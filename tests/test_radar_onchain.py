import tempfile
import time
import unittest
from pathlib import Path
from radar.onchain import Onchain, TOKEN, TOKEN_2022
from radar.store import Store
from radar.engine import risk_assessment

MINT = 'So11111111111111111111111111111111111111112'


class OnchainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name)/'test.sqlite'))
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == 'getAccountInfo':
            return {'result':{'context':{'slot':100}, 'value':{'owner':TOKEN,
                    'data':{'parsed':{'type':'mint','info':{'supply':'1000','decimals':6,
                    'mintAuthority':None,'freezeAuthority':None}}}}}}
        return {'result':{'context':{'slot':102}, 'value':[{'address':'account', 'amount':'80'}]}}

    def test_finalized_raw_integer_concentration_cached(self):
        chain = Onchain(self.store, self.rpc)
        result = chain.inspect(MINT)
        self.assertEqual(result['top_10_pct'], 8)
        self.assertEqual(result['mint_slot'],100)
        self.assertEqual(result['accounts_slot'],102)
        self.assertEqual(chain.inspect(MINT)['observed_at'],result['observed_at'])
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0][1][1]['commitment'],'finalized')

    def test_rpc_failure_is_unknown_and_cools_down(self):
        calls = []
        def fail(*args):
            calls.append(args)
            return {'error':{'code':-32005}}
        chain = Onchain(self.store, fail)
        self.assertEqual(chain.inspect(MINT)['observed_at'],0)
        chain.inspect('1'*32)
        self.assertEqual(len(calls),1)

    def test_projection_retains_nested_types_and_omits_raw_evidence(self):
        self.store.append('decisions',MINT,{'score':{'candidate':True},'ranks':{'gem':1},'risk_evidence':{'secret':'large'}},1000)
        row = self.store.history('decisions',cutoff=1000,fields=('score','ranks'))[0]
        self.assertTrue(row['score']['candidate'])
        self.assertEqual(row['ranks']['gem'],1)
        self.assertNotIn('risk_evidence',row)
        with self.assertRaises(ValueError):
            self.store.history('decisions',fields=("score');drop",))

    def test_current_cohort_query_excludes_previous_and_future_rows(self):
        for stamp,run in ((1000,'old'),(1100,'current'),(1200,'future')):
            self.store.append('decisions',MINT,{'run_id':run},stamp)
        rows = self.store.history('decisions',cutoff=1150,since=1100,run_id='current')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['run_id'],'current')

    def test_largest_account_failure_preserves_authorities(self):
        def partial(method, params):
            return self.rpc(method,params) if method=='getAccountInfo' else {'error':{}}
        result = Onchain(self.store, partial).inspect(MINT)
        self.assertEqual(result['token']['freezeAuthority'],None)
        self.assertIn('accounts_error',result)
        self.assertNotIn('top_10_pct',result)

    def evidence(self, chain):
        return {'observed_at':1000, 'raw':{'token':{'mintAuthority':None,'freezeAuthority':None},
                    'topHolders':[{'pct':1}]*10}, 'onchain':chain}

    def test_active_independent_freeze_overrides_provider_null(self):
        chain = {'observed_at':1000,'token':{'freezeAuthority':'active'}}
        risk = risk_assessment({'liquidity':100000,'stale':False},self.evidence(chain),1000)
        self.assertTrue(risk['rejected'])
        self.assertEqual(risk['freeze_authority'],'active')

    def test_unreviewed_token2022_holds_and_transfer_hook_rejects(self):
        chain = {'observed_at':1000,'token_2022':True,'extensions':[]}
        risk = risk_assessment({'liquidity':100000,'stale':False},self.evidence(chain),1000)
        self.assertFalse(risk['eligible'])
        self.assertFalse(risk['rejected'])
        chain['extensions'] = [{'extension':'transferHook'}]
        self.assertTrue(risk_assessment({'liquidity':100000,'stale':False},self.evidence(chain),1000)['rejected'])

    def test_future_and_stale_authorities_cannot_enter_decision(self):
        chain = {'observed_at':1001,'token':{'freezeAuthority':'active'}}
        risk = risk_assessment({'liquidity':100000,'stale':False},self.evidence(chain),1000)
        self.assertFalse(risk['rejected'])
        evidence = self.evidence(chain)
        evidence['observed_at'] = 1001
        risk = risk_assessment({'liquidity':100000,'stale':False},evidence,1000)
        self.assertFalse(risk['eligible'])
        self.assertIn('freezeAuthority',risk['unknowns'])
