"""Bounded public API adapters. Endpoint errors never become zero observations."""
from datetime import datetime
import json
import math
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MINT = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
DEX = 'https://api.dexscreener.com'
GECKO = 'https://api.geckoterminal.com/api/v2'


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


class Client:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport or self._http
        self.last = {}
        self.receipts = {}

    @staticmethod
    def _http(url):
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'GemRadar/1'})
        with urlopen(request, timeout=15) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError('Oversized provider response')
            return json.loads(body)

    def get(self, provider, url, ttl=60):
        key = 'cache:' + url
        cached = self.store.status(key).get(key)
        if cached and time.time() - cached['updated'] < ttl:
            self.receipts[url] = cached['updated']
            return cached['data']
        health = self.store.status('provider:' + provider).get('provider:' + provider, {})
        if health.get('retry_at', 0) > time.time():
            raise RuntimeError('Provider cooldown')
        # Gecko public allowance is shared across all endpoints.
        delay = (2.2 if provider == 'gecko' else .35) - (time.monotonic() - self.last.get(provider, 0))
        if delay > 0:
            time.sleep(delay)
        for attempt in range(3):
            self.last[provider] = time.monotonic()
            try:
                value = self.transport(url)
                self.store.set_status(key, {'data': value})
                self.receipts[url] = time.time()
                self.store.set_status('provider:' + provider, {'healthy': True, 'last_success': time.time()})
                return value
            except HTTPError as error:
                if error.code == 429:
                    try:
                        delay = max(60, min(3600, float(error.headers.get('Retry-After', 60))))
                    except (ValueError, TypeError):
                        delay = 60
                    self.store.set_status('provider:' + provider, {'healthy': False, 'error': 'HTTP 429', 'retry_at': time.time() + delay})
                    raise RuntimeError('Provider rate limited') from None
                if error.code < 500:
                    break
            except (OSError, ValueError):
                pass
            if attempt < 2:
                time.sleep(2 ** attempt)
        self.store.set_status('provider:' + provider, {'healthy': False, 'error': 'Request failed', 'retry_at': time.time() + 60})
        raise RuntimeError('Provider unavailable')


def dex_pairs(payload, mint):
    if not isinstance(payload, list):
        raise ValueError('Invalid DexScreener pairs')
    result = []
    for p in payload:
        if p.get('chainId') != 'solana' or p.get('baseToken', {}).get('address') != mint:
            continue
        tx = p.get('txns', {}).get('m5', {})
        info = p.get('info') or {}
        result.append({'provider': 'dex', 'source_url': DEX + '/token-pairs/v1/solana/' + mint,
                       'mint': mint, 'pool': p.get('pairAddress'), 'dex': p.get('dexId'),
                       'name': p['baseToken'].get('name'), 'symbol': p['baseToken'].get('symbol'),
                       'price': number(p.get('priceUsd')), 'market_cap': number(p.get('marketCap')),
                       'fdv': number(p.get('fdv')), 'liquidity': number((p.get('liquidity') or {}).get('usd')),
                       'volume': {k: number(v) for k, v in p.get('volume', {}).items()},
                       'buys': number(tx.get('buys')), 'sells': number(tx.get('sells')),
                       'price_change': p.get('priceChange') or {}, 'pool_created': timestamp(p.get('pairCreatedAt')),
                       'websites': [x.get('url') for x in info.get('websites', []) if x.get('url')],
                       'socials': info.get('socials', []), 'raw': p})
    return result


def gecko_pools(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('Invalid GeckoTerminal pools')
    result = []
    for p in payload['data']:
        a, rel = p.get('attributes', {}), p.get('relationships', {})
        mint = rel.get('base_token', {}).get('data', {}).get('id', '').removeprefix('solana_')
        if not MINT.fullmatch(mint):
            continue
        tx = a.get('transactions', {}).get('m5', {})
        result.append({'provider': 'gecko', 'source_url': GECKO + '/networks/solana/pools/' + str(a.get('address', '')),
                       'mint': mint, 'pool': a.get('address'), 'dex': rel.get('dex', {}).get('data', {}).get('id'),
                       'name': a.get('name'), 'symbol': None, 'price': number(a.get('base_token_price_usd')),
                       'market_cap': number(a.get('market_cap_usd')), 'fdv': number(a.get('fdv_usd')),
                       'liquidity': number(a.get('reserve_in_usd')),
                       'volume': {k: number(v) for k, v in a.get('volume_usd', {}).items()},
                       'buys': number(tx.get('buys')), 'sells': number(tx.get('sells')),
                       'unique_buyers': number(tx.get('buyers')), 'unique_sellers': number(tx.get('sellers')),
                       'price_change': a.get('price_change_percentage') or {},
                       'pool_created': timestamp(a.get('pool_created_at')), 'websites': [], 'socials': [], 'raw': p})
    return result


class Providers:
    def __init__(self, client):
        self.client = client

    def discover(self):
        # Independent feeds fail independently; no fabricated fallback data.
        discovered, observations, errors = {}, [], []
        try:
            rows = self.client.get('dex', DEX + '/token-profiles/latest/v1')
            if not isinstance(rows, list):
                raise ValueError('Invalid profile response')
            for row in rows:
                mint = row.get('tokenAddress', '')
                if row.get('chainId') == 'solana' and MINT.fullmatch(mint):
                    discovered[mint] = {'source': 'dex_profile', 'name': mint, 'source_url': DEX + '/token-profiles/latest/v1'}
        except (RuntimeError, ValueError, TypeError):
            errors.append('DexScreener discovery unavailable')
        try:
            rows = gecko_pools(self.client.get('gecko', GECKO + '/networks/solana/new_pools'))
            for row in rows:
                row['fetched_at'] = self.client.receipts[GECKO + '/networks/solana/new_pools']
                discovered.setdefault(row['mint'], {'source': 'gecko_new_pool', 'name': row['name'], 'source_url': row['source_url']})
            observations.extend(rows)
        except (RuntimeError, ValueError, TypeError):
            errors.append('GeckoTerminal discovery unavailable')
        return discovered, observations, errors

    def markets(self, mint):
        if not MINT.fullmatch(mint):
            raise ValueError('Invalid mint')
        result, errors = [], []
        try:
            url = DEX + '/token-pairs/v1/solana/' + mint
            rows = dex_pairs(self.client.get('dex', url), mint)
            result += [dict(r, fetched_at=self.client.receipts[url]) for r in rows]
        except (RuntimeError, ValueError, TypeError):
            errors.append('DexScreener markets unavailable')
        try:
            url = GECKO + '/networks/solana/tokens/' + mint + '/pools'
            rows = gecko_pools(self.client.get('gecko', url))
            result += [dict(r, fetched_at=self.client.receipts[url]) for r in rows if r['mint'] == mint]
        except (RuntimeError, ValueError, TypeError):
            errors.append('GeckoTerminal markets unavailable')
        return result, errors

    def risk(self, mint):
        url = 'https://api.rugcheck.xyz/v1/tokens/' + mint + '/report'
        try:
            raw = self.client.get('rugcheck', url, ttl=1800)
            if not isinstance(raw, dict) or not isinstance(raw.get('token'), dict):
                raise ValueError('Invalid RugCheck report')
            return {'source_url': url, 'raw': raw, 'observed_at': self.client.receipts[url]}
        except (RuntimeError, ValueError, TypeError):
            return {'source_url': url, 'raw': None, 'observed_at': time.time(), 'error': 'RugCheck unavailable'}
