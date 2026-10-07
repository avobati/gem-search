"""Read-only finalized mint checks. Largest accounts are not wallet identities."""
import json
import os
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from radar.providers import MINT

TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'


class Onchain:
    def __init__(self, store, transport=None):
        self.store = store
        self.url = os.getenv('RADAR_RPC_URL', 'https://api.mainnet-beta.solana.com')
        if urlsplit(self.url).scheme != 'https':
            raise ValueError('Radar RPC requires HTTPS')
        self.transport = transport or self._http

    def _http(self, method, params):
        body = json.dumps({'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params}).encode()
        with urlopen(Request(self.url, data=body, headers={'Content-Type':'application/json'}), timeout=12) as response:
            data = response.read(1_000_001)
            if len(data) > 1_000_000:
                raise ValueError('Oversized RPC response')
            return json.loads(data)

    def call(self, method, params):
        result = self.transport(method, params)
        if not isinstance(result, dict) or result.get('error') or not isinstance(result.get('result'), dict):
            raise ValueError('RPC response unavailable')
        return result['result']

    def inspect(self, mint):
        if not MINT.fullmatch(mint):
            raise ValueError('Invalid mint')
        key = 'onchain:' + mint
        previous = self.store.status(key).get(key)
        if previous and time.time()-previous['updated'] < 1800:
            return previous
        health = self.store.status('provider:rpc').get('provider:rpc', {})
        if health.get('retry_at', 0) > time.time():
            return {'observed_at':0, 'error':'RPC cooldown', 'mint':mint}
        evidence = {'mint':mint, 'commitment':'finalized', 'source_url':'https://solscan.io/token/' + mint,
                    'method':'Finalized mint and largest token accounts; calls may have different slots'}
        try:
            account = self.call('getAccountInfo', [mint, {'encoding':'jsonParsed', 'commitment':'finalized'}])
            value = account.get('value') or {}
            parsed = (value.get('data') or {}).get('parsed') or {}
            info = parsed.get('info') or {}
            if value.get('owner') not in (TOKEN, TOKEN_2022) or parsed.get('type') != 'mint':
                raise ValueError('Unrecognized token mint')
            if info.get('isInitialized') is False:
                raise ValueError('Uninitialized mint')
            evidence.update(program=value['owner'], token_2022=value['owner']==TOKEN_2022,
                            mint_slot=(account.get('context') or {}).get('slot'),
                            token={k:info[k] for k in ('mintAuthority','freezeAuthority') if k in info},
                            extensions=info.get('extensions', []), supply=info.get('supply'), decimals=info.get('decimals'))
            try:
                largest = self.call('getTokenLargestAccounts', [mint, {'commitment':'finalized'}])
                supply = int(info['supply'])
                if supply <= 0:
                    raise ValueError('Nonpositive supply')
                holders = []
                for holder in largest.get('value', [])[:20]:
                    amount = int(holder['amount'])
                    if amount < 0 or amount > supply:
                        raise ValueError('Invalid token amount')
                    holders.append({'address':holder['address'], 'amount':str(amount), 'pct':amount/supply*100})
                evidence.update(top_accounts=holders, accounts_slot=(largest.get('context') or {}).get('slot'),
                                top_10_pct=sum(h['pct'] for h in holders[:10]) if holders else None)
            except (OSError, ValueError, KeyError, TypeError):
                evidence['accounts_error'] = 'Largest accounts unavailable'
            evidence['observed_at'] = time.time()
            self.store.set_status('provider:rpc', {'healthy':True, 'last_success':time.time()})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            evidence.update(observed_at=0, error='Mint RPC unavailable')
            self.store.set_status('provider:rpc', {'healthy':False, 'error':'RPC unavailable', 'retry_at':time.time()+300})
        self.store.set_status(key, evidence)
        return evidence
