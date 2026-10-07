"""Deterministic, versioned scoring. Unknown evidence cannot earn points."""
import hashlib
import json
import math
import re
from radar.providers import number

VERSION = 'gem-v1.1-onchain'
WEIGHTS = {'momentum': 25, 'liquidity': 15, 'wallets': 15, 'distribution': 10,
           'narrative': 10, 'project': 10, 'structure': 10, 'risk': 5}
WINDOWS = {'5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '3h': 10800,
           '6h': 21600, '12h': 43200, '24h': 86400}
HORIZONS = {'1h': 3600, '3h': 10800, '6h': 21600, '12h': 43200,
            '24h': 86400, '48h': 172800, '3d': 259200, '7d': 604800}


def ratio(current, previous):
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous


def primary(rows):
    # Never sum duplicate liquidity reported by aggregators.
    valid = [r for r in rows if r.get('price') is not None and r['price'] > 0]
    return max(valid, key=lambda r: (r.get('liquidity') or 0, r['available_at'], r.get('provider') == 'dex'), default=None)


def features(history, cutoff):
    history = [r for r in history if r['available_at'] <= cutoff]
    if not history:
        return {'missing': ['market'], 'stale': True, 'windows': {}}
    latest_time = max(r['available_at'] for r in history)
    latest = primary([r for r in history if latest_time - r['available_at'] <= 60])
    if not latest:
        return {'missing': ['price'], 'stale': True, 'windows': {}}
    f = {k: latest.get(k) for k in ('price', 'liquidity', 'market_cap', 'fdv', 'buys', 'sells', 'unique_buyers', 'pool_created', 'name', 'symbol', 'dex', 'pool')}
    f.update(volume_1h=(latest.get('volume') or {}).get('h1'), observed_at=latest['available_at'],
             stale=cutoff-latest['available_at'] > 600, source=latest.get('provider'), windows={},
             smart_wallet_score=None, holder_growth=None, missing=[])
    pools = {r.get('pool') for r in history if cutoff-r['available_at'] < 600 and r.get('pool')}
    f['dex_count'] = len({r.get('dex') for r in history if cutoff-r['available_at'] < 600 and r.get('dex')})
    f['pool_count'] = len(pools)
    f['buy_sell'] = ratio(f['buys'], f['sells'])
    f['transactions'] = f['buys']+f['sells'] if f['buys'] is not None and f['sells'] is not None else None
    # Match same market/provider to avoid artificial velocity on provider switching.
    same = sorted([r for r in history if r.get('pool') == latest.get('pool') and r.get('provider') == latest.get('provider')], key=lambda r: r['available_at'])
    def at(target, tolerance):
        older = [r for r in same if r['available_at'] <= target and target-r['available_at'] <= tolerance]
        return older[-1] if older else None
    for label, seconds in WINDOWS.items():
        previous, earlier = at(cutoff-seconds, seconds*.25), at(cutoff-seconds*2, seconds*.25)
        window = {}
        for key in ('price', 'liquidity', 'market_cap', 'unique_buyers', 'buys', 'sells'):
            current = latest.get(key)
            old = previous.get(key) if previous else None
            factor = ratio(current, old)
            window[key + '_growth'] = (factor-1)*100 if factor is not None else None
            before = ratio(old, earlier.get(key)) if earlier else None
            window[key + '_acceleration'] = factor-before if factor is not None and before is not None else None
        current_v = (latest.get('volume') or {}).get('h1')
        old_v = (previous.get('volume') or {}).get('h1') if previous else None
        earlier_v = (earlier.get('volume') or {}).get('h1') if earlier else None
        velocity, old_velocity = ratio(current_v, old_v), ratio(old_v, earlier_v)
        window['volume_velocity'] = velocity
        window['volume_acceleration'] = ratio(velocity, old_velocity)
        # h1 is overlapping rolling volume, not disjoint interval trade volume.
        window['method'] = 'same-provider same-pool sampled change; volume uses rolling h1'
        f['windows'][label] = window
    f['volume_velocity'] = f['windows']['15m']['volume_velocity']
    f['volume_acceleration'] = f['windows']['15m']['volume_acceleration']
    f['buyer_acceleration'] = f['windows']['15m']['unique_buyers_acceleration']
    f['liquidity_acceleration'] = f['windows']['15m']['liquidity_acceleration']
    for key in ('price', 'liquidity', 'market_cap', 'buys', 'sells', 'unique_buyers', 'smart_wallet_score', 'holder_growth'):
        if f.get(key) is None:
            f['missing'].append(key)
    # Compare contemporaneous markets; disagreement does not get silently averaged.
    prices = [r['price'] for r in history if cutoff-r['available_at'] <= 120 and r.get('price') and (r.get('liquidity') or 0) >= 10000]
    f['price_disagreement'] = max(prices)/min(prices)-1 if len(prices)>1 else None
    return f


def risk_assessment(f, evidence, cutoff):
    raw = evidence.get('raw') if evidence else None
    stale = not evidence or not 0 <= cutoff-evidence.get('observed_at', 0) <= 3600
    token = dict(raw.get('token') or {}) if raw and not stale else {}
    chain = (evidence or {}).get('onchain') or {}
    chain_fresh = bool(chain.get('observed_at')) and 0 <= cutoff-chain['observed_at'] <= 3600
    if chain_fresh:
        for field in ('mintAuthority','freezeAuthority'):
            if field in chain.get('token', {}):
                # Any active authority wins over an incompatible null report.
                if chain['token'][field] is not None or field not in token:
                    token[field] = chain['token'][field]
    reasons, unknowns, score = [], [], 0
    for field, points in [('mintAuthority', 25), ('freezeAuthority', 35)]:
        if field not in token:
            unknowns.append(field)
        elif token[field] is not None:
            score += points; reasons.append(field + ' remains active')
    top = None
    if raw and not stale and isinstance(raw.get('topHolders'), list) and raw['topHolders']:
        values = [number(h.get('pct')) for h in raw['topHolders'][:10]]
        if all(v is not None for v in values):
            top = sum(values)
    if chain_fresh and number(chain.get('top_10_pct')) is not None:
        chain_top = number(chain['top_10_pct'])
        if top is not None and abs(top-chain_top) > 10:
            reasons.append('Concentration sources differ by more than ten percentage points')
        top = max(top if top is not None else 0, chain_top)
    # Token accounts can include pools; owner attribution remains unverified.
    if top is not None and top >= 70:
        score += 35; reasons.append(f'Top ten reported accounts hold {top:.1f}% (pool attribution unverified)')
    elif top is not None and top >= 40:
        score += 20; reasons.append(f'Top ten reported accounts hold {top:.1f}%')
    if top is None:
        unknowns.append('top_10_concentration')
    liquidity = f.get('liquidity')
    if liquidity is None:
        unknowns.append('liquidity')
    elif liquidity < 10000:
        score += 40; reasons.append(f'Liquidity below $10,000: ${liquidity:,.0f}')
    elif liquidity < 50000:
        score += 15; reasons.append('Thin liquidity below $50,000')
    if raw and raw.get('rugged') is True:
        score = 100; reasons.append('Provider reports rugged token')
    severe = False
    for item in (raw.get('risks') or []) if raw and not stale else []:
        if str(item.get('level', '')).lower() == 'danger':
            severe = True; score += 20; reasons.append('Provider danger: ' + str(item.get('name', 'Unspecified'))[:150])
    if f.get('price_disagreement') is not None and f['price_disagreement'] > .3:
        score += 20; reasons.append('Liquid markets disagree on price by more than 30%')
    chain_reject = False
    if chain_fresh:
        # Token-2022 extensions can affect transfers; an unreviewed extension
        # remains a hold even when an aggregator reports low risk.
        if chain.get('token_2022'):
            unknowns.append('Token-2022 transfer semantics')
            extensions = {str(x.get('extension','')).lower() for x in chain.get('extensions', []) if isinstance(x, dict)}
            if extensions & {'nontransferable','transferhook','permanentdelegate','confidentialtransfermint'}:
                chain_reject = True
                score += 60
                reasons.append('Token-2022 transfer restriction or privileged extension')
    unknowns += ['creator_cluster', 'bundled_wallets', 'LP_lock', 'sell_restrictions', 'creator_history']
    score = min(100, score)
    level = 'EXTREME' if score >= 90 else 'HIGH' if score >= 60 else 'MEDIUM' if score >= 30 else 'LOW'
    complete_gate = all(x not in unknowns for x in ('mintAuthority', 'freezeAuthority', 'top_10_concentration', 'liquidity', 'Token-2022 transfer semantics'))
    hard_reject = bool(raw and not stale and raw.get('rugged')) or chain_reject or severe or token.get('freezeAuthority') is not None or score >= 60 or (liquidity is not None and liquidity < 10000)
    return {'score': score, 'level': level if complete_gate else 'UNKNOWN', 'observed_level': level,
            'reasons': reasons, 'unknowns': unknowns, 'top_10_pct': top, 'stale': stale,
            'eligible': complete_gate and not hard_reject and not f.get('stale', True),
            'rejected': hard_reject, 'source_url': evidence.get('source_url') if evidence else None,
            'top_holders': (raw.get('topHolders') or [])[:10] if raw else [],
            'mint_authority': token.get('mintAuthority'), 'freeze_authority': token.get('freezeAuthority'),
            'onchain': chain if chain_fresh else None}


def social_features(posts, mint, symbol, name, cutoff):
    # Address attribution is strong; ticker/name-only matches remain ambiguous.
    exact, ambiguous = [], []
    for post in posts:
        available = post.get('captured_at', post.get('available_at', float('inf')))
        created = post.get('timestamp', 0)
        if available > cutoff or created > cutoff or cutoff-created > 86400:
            continue
        text = post.get('text', '')
        if re.search(r'(?<![1-9A-HJ-NP-Za-km-z])' + re.escape(mint) + r'(?![1-9A-HJ-NP-Za-km-z])', text):
            exact.append(post)
        elif symbol and len(symbol) >= 3 and re.search(r'\$' + re.escape(symbol) + r'\b', text, re.I):
            ambiguous.append(post)
    normalized = {re.sub(r'https?://\S+|[^\w\s]', '', p['text'].lower()).strip() for p in exact}
    authors = {p.get('author', '').lower() for p in exact if p.get('author')}
    duplicates = 1-len(normalized)/len(exact) if exact else None
    recent = sum(cutoff-p['timestamp'] < 3600 for p in exact)
    previous = sum(3600 <= cutoff-p['timestamp'] < 7200 for p in exact)
    return {'mentions': len(exact), 'authors': len(authors), 'duplicate_ratio': duplicates,
            'mention_velocity': ratio(recent, previous), 'ambiguous_mentions': len(ambiguous),
            'posts': exact[:20], 'scope': 'observed captures only; account credibility unverified'}


def score_token(f, risk, research, weights=None):
    weights = dict(weights or WEIGHTS)
    if set(weights) != set(WEIGHTS) or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values()) or sum(weights.values()) != 100:
        raise ValueError('Weights must cover all components and sum to 100')
    components = {k: None for k in weights}
    velocity, acceleration = f.get('volume_velocity'), f.get('volume_acceleration')
    if velocity is not None or acceleration is not None:
        components['momentum'] = min(1, max(0, ((velocity or 1)-1)/4)*.6 + max(0, ((acceleration or 1)-1)/3)*.4)
    liquidity = f.get('liquidity')
    if liquidity is not None:
        components['liquidity'] = min(1, liquidity/250000)
    if risk.get('top_10_pct') is not None:
        components['distribution'] = max(0, 1-risk['top_10_pct']/100)
    social = research.get('social', {})
    if social.get('mentions'):
        components['narrative'] = min(1, social.get('authors', 0)/20)*(1-social.get('duplicate_ratio', 1))
    pages = research.get('pages', [])
    if research.get('crawled'):
        components['project'] = min(1, sum(len(p.get('text', '')) >= 500 for p in pages)/3)
    if f.get('dex_count'):
        components['structure'] = min(1, f['dex_count']/3)*(.3 if (f.get('price_disagreement') or 0)>.3 else 1)
    if risk['level'] != 'UNKNOWN':
        components['risk'] = (100-risk['score'])/100
    earned = {k: round((v or 0)*weights[k], 2) for k,v in components.items()}
    coverage = sum(weights[k] for k,v in components.items() if v is not None)
    total = round(sum(earned.values()), 2)
    # Separate horizon recipes; these are NOT calibrated probabilities.
    horizons = {h: round(min(100, max(0, total + (earned['momentum']-earned['project'])*factor)), 2)
                for h, factor in [('6h', .3), ('12h', .15), ('24h', 0), ('3d', -.25), ('7d', -.4)]}
    return {'version': VERSION, 'weights': weights, 'components': earned, 'unknown_components': [k for k,v in components.items() if v is None],
            'total': total, 'coverage': coverage, 'horizons': horizons, 'kind': 'uncalibrated research score',
            'confidence': 'LOW' if coverage < 60 or risk['level'] == 'UNKNOWN' else 'MEDIUM',
            'candidate': risk['eligible'] and total >= 50}


def explain(f, risk, score, research):
    why = []
    for key, label in [('volume_velocity', 'Rolling hourly volume velocity'), ('volume_acceleration', 'Volume velocity acceleration')]:
        if f.get(key) is not None:
            why.append(f'{label}: {f[key]:.2f}×')
    if f.get('liquidity') is not None:
        why.append(f'Observed liquidity: ${f["liquidity"]:,.0f}')
    if research.get('social', {}).get('authors'):
        why.append(f'{research["social"]["authors"]} distinct captured authors mention this mint')
    return {'why': why or ['Insufficient longitudinal evidence to identify acceleration'],
            'risks': risk['reasons'] + ['Unverified: ' + ', '.join(risk['unknowns']),
                                      'Scores have not established predictive value in out-of-sample testing']}


def baseline_order(rows, method, run_id):
    def key(row):
        f = row['features']
        if method == 'random':
            return hashlib.sha256((run_id+row['mint']).encode()).hexdigest()
        if method == 'gem':
            return row['score']['total']
        if method == 'volume':
            return f.get('volume_1h') if f.get('volume_1h') is not None else -1
        if method == 'liquidity':
            return f.get('liquidity') if f.get('liquidity') is not None else -1
        if method == 'momentum':
            return f.get('volume_velocity') if f.get('volume_velocity') is not None else -1
        if method == 'gainer':
            return f.get('windows', {}).get('1h', {}).get('price_growth') if f.get('windows', {}).get('1h', {}).get('price_growth') is not None else -1
        raise ValueError('Unknown baseline')
    return sorted(rows, key=key, reverse=True)
