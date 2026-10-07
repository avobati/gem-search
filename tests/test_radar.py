import base64
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from radar.store import Store
from radar.providers import dex_pairs, gecko_pools, number, Client, Providers
from radar.engine import features, risk_assessment, score_token, social_features
from radar.validation import label_returns, report, walk_forward_periods, evaluate_walk_forward
from radar.server import handler_for

MINT='So11111111111111111111111111111111111111112'


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(str(Path(self.temp.name)/'radar.sqlite'))

    def tearDown(self):
        self.temp.cleanup()

    def observation(self, stamp, volume=100, price=1, **kwargs):
        return dict({'mint':MINT,'available_at':stamp,'provider':'dex','pool':'pool','dex':'raydium',
                     'price':price,'liquidity':200000,'market_cap':1000000,'volume':{'h1':volume},'buys':10,'sells':5},**kwargs)

    def test_first_seen_is_immutable_across_restart_and_rename(self):
        self.store.discover(MINT,{'name':'Old'},100)
        Store(self.store.url).discover(MINT,{'name':'New'},200)
        token=self.store.tokens()[0]
        self.assertEqual(token['first_seen'],100)
        self.assertEqual(len(self.store.history('events',MINT)),1)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute('UPDATE tokens SET first_seen=0')

    def test_no_future_information_and_acceleration(self):
        rows=[self.observation(1000,8000),self.observation(1900,20000),self.observation(2800,70000)]
        f=features(rows,2800)
        self.assertAlmostEqual(f['volume_velocity'],3.5)
        self.assertAlmostEqual(f['volume_acceleration'],1.4)
        future=self.observation(2900,100000000,price=500)
        self.assertEqual(f,features(rows+[future],2800))

    def test_missing_values_are_unknown_and_not_zero(self):
        for v in [None,'bad',float('inf'),float('nan'),True,-1]:
            self.assertIsNone(number(v))
        self.assertEqual(number('0'),0)
        f=features([self.observation(1000,volume=None,liquidity=None)],1000)
        self.assertIn('liquidity',f['missing'])
        self.assertIsNone(f['volume_acceleration'])
        risk=risk_assessment(f,None,1000)
        self.assertEqual(risk['level'],'UNKNOWN')
        self.assertFalse(risk['eligible'])
        self.assertFalse(score_token(f,risk,{})['candidate'])

    def test_rug_and_freeze_authority_cannot_pass(self):
        f=features([self.observation(1000)],1000)
        for token in [{'mintAuthority':None,'freezeAuthority':'active'}, {'mintAuthority':None,'freezeAuthority':None}]:
            evidence={'observed_at':1000,'raw':{'token':token,'topHolders':[{'pct':1}]*10,'rugged':True}}
            risk=risk_assessment(f,evidence,1000)
            self.assertTrue(risk['rejected'])
            self.assertEqual(risk['score'],100)
            self.assertFalse(risk['eligible'])

    def test_liquidity_removal_rejects(self):
        f=features([self.observation(1000,liquidity=100)],1000)
        risk=risk_assessment(f,{'observed_at':1000,'raw':{'token':{'mintAuthority':None,'freezeAuthority':None},'topHolders':[{'pct':1}]}},1000)
        self.assertTrue(risk['rejected'])

    def test_provider_switch_does_not_invent_acceleration(self):
        rows=[self.observation(1000),self.observation(1900,volume=500,provider='gecko'),self.observation(2800,volume=3000)]
        f=features(rows,2800)
        self.assertIsNone(f['volume_acceleration'])

    def test_duplicate_pools_not_summed_and_price_disagreement(self):
        f=features([self.observation(1000),self.observation(1000,provider='gecko',price=2)],1000)
        self.assertEqual(f['liquidity'],200000)
        self.assertEqual(f['pool_count'],1)
        self.assertEqual(f['price_disagreement'],1)

    def test_author_spam_and_future_capture_excluded(self):
        posts=[{'text':MINT+' buy now','author':str(i),'timestamp':1000,'captured_at':1000} for i in range(20)]
        posts.append({'text':MINT+' good','author':'future','timestamp':900,'captured_at':1100})
        s=social_features(posts,MINT,'TEST','Test',1000)
        self.assertEqual(s['authors'],20)
        self.assertAlmostEqual(s['duplicate_ratio'],.95)
        self.assertEqual(s['mentions'],20)

    def test_forward_return_idempotence_missing_and_drawdown(self):
        d={'run_id':'run','cutoff':1000,'features':{'price':1,'pool':'pool','source':'dex','stale':False},
           'ranks':{'gem':1,'random':1,'volume':1,'liquidity':1,'momentum':1,'gainer':1},'score':{'candidate':True}}
        self.store.append('decisions',MINT,d,1000,identity='decision')
        for stamp,price in [(2000,2),(3000,.5),(4600,1.5)]:
            self.store.append('observations',MINT,self.observation(stamp,price=price),stamp)
        label_returns(self.store,5000);label_returns(self.store,5000)
        labels=self.store.history('returns',cutoff=5000)
        self.assertEqual(len(labels),1)
        self.assertAlmostEqual(labels[0]['return_pct'],50)
        self.assertAlmostEqual(labels[0]['max_drawdown_pct'],-75)
        label_returns(self.store,13000)
        missing=[r for r in self.store.history('returns',cutoff=13000) if r['horizon']=='3h'][0]
        self.assertEqual(missing['status'],'missing')
        self.assertIsNone(missing['return_pct'])

    def test_empty_report_has_no_fabricated_metrics(self):
        r=report(self.store)
        self.assertEqual(r['backtest']['status'],'not_available')
        self.assertTrue(all(x['hit_rate'] is None for x in r['metrics']))

    def test_walk_forward_embargo(self):
        periods=walk_forward_periods(0,100*86400)
        self.assertTrue(periods)
        for p in periods:
            self.assertGreaterEqual(p['validation'][0]-p['train'][1],7*86400)
            self.assertGreaterEqual(p['test'][0]-p['validation'][1],7*86400)

    def test_walk_forward_does_not_use_labels_available_after_selection(self):
        from radar.engine import WEIGHTS
        decision={'run_id':'run','cutoff':1000,'features':{'price':1},'risk':{},'research':{}}
        self.store.append('decisions',MINT,decision,1000,identity='d')
        self.store.append('returns',MINT,{'decision_id':'d','horizon':'24h','status':'observed','target_at':1100,'return_pct':500},1500)
        periods=[{'train':[900,1200],'validation':[1300,1400],'test':[1600,1700]}]
        with patch('radar.engine.score_token',side_effect=AssertionError('Future label must be excluded')):
            result=evaluate_walk_forward(self.store,periods,[WEIGHTS],minimum=1)
        self.assertEqual(result['folds'][0]['status'],'insufficient_independent_mature_tokens')
        self.assertFalse(result['production_weights_changed'])

    def test_provider_malformed_and_missing_cap(self):
        with self.assertRaises(ValueError):
            gecko_pools({})
        with self.assertRaises(ValueError):
            dex_pairs({},MINT)
        p={'chainId':'solana','baseToken':{'address':MINT},'priceUsd':'1','fdv':100,'pairAddress':'pool'}
        parsed=dex_pairs([p,p],MINT)
        self.assertIsNone(parsed[0]['market_cap'])
        self.assertEqual(parsed[0]['fdv'],100)

    def test_provider_fallback_during_dex_outage(self):
        def transport(url):
            if 'dexscreener' in url:
                raise OSError('Offline')
            return {'data':[{'attributes':{'address':'pool','name':'Test','base_token_price_usd':'1'},
                             'relationships':{'base_token':{'data':{'id':'solana_'+MINT}}}}]}
        with patch('radar.providers.time.sleep'):
            discovered,rows,errors=Providers(Client(self.store,transport)).discover()
        self.assertIn(MINT,discovered)
        self.assertEqual(len(rows),1)
        self.assertTrue(errors)

    def test_readonly_authenticated_api_and_secret_boundary(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.store,'test:long-secret-password-here'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            con=http.client.HTTPConnection('127.0.0.1',server.server_port)
            con.request('GET','/api/radar');r=con.getresponse();self.assertEqual(r.status,401);r.read()
            auth='Basic '+base64.b64encode(b'test:long-secret-password-here').decode()
            con.request('GET','/api/radar',headers={'Authorization':auth});r=con.getresponse();body=r.read();self.assertEqual(r.status,200)
            self.assertNotIn(b'password',body)
            con.request('POST','/api/launches',body='{}',headers={'Authorization':auth});r=con.getresponse();self.assertEqual(r.status,405);r.read()
            con.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
