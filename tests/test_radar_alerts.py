import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from radar.alerts import notify
from radar.store import Store


class AlertTests(unittest.TestCase):
    def test_disabled_and_uncertain_delivery_do_not_spam(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(str(Path(root)/'test.sqlite'))
            row={'mint':'test','features':{'name':'Test','liquidity':200000},
                 'risk':{'score':10},'score':{'candidate':True,'total':90,'coverage':90},
                 'explanation':{'why':['Observed signal'],'risks':['Unknown sellability']}}
            calls=[]
            def failure(body):
                calls.append(body);raise TimeoutError()
            with patch.dict(os.environ,{'TELEGRAM_ALERTS_ENABLED':'0'}):
                self.assertEqual(notify(store,[row],failure),0)
                self.assertEqual(calls,[])
            with patch.dict(os.environ,{'TELEGRAM_ALERTS_ENABLED':'1','TELEGRAM_BOT_TOKEN':'test','TELEGRAM_CHAT_ID':'test'}):
                self.assertEqual(notify(store,[row],failure),0)
                self.assertEqual(notify(store,[row],failure),0)
                self.assertEqual(len(calls),1)
                self.assertEqual(store.status()['alert:test']['state'],'unknown')
