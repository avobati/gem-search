import http.client
from http.server import ThreadingHTTPServer
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
from radar.auth import Sessions
from radar.server import handler_for
from radar.store import Store


class SessionTests(unittest.TestCase):
    def test_one_use_challenge_and_session_expiry(self):
        sessions=Sessions()
        with patch('radar.auth.time.time',return_value=1000):
            nonce=sessions.challenge()
            token,status=sessions.login(nonce,'name:password','name:password')
            self.assertEqual(status,'ok')
            self.assertTrue(sessions.valid(token))
            self.assertIsNone(sessions.login(nonce,'name:password','name:password')[0])
        with patch('radar.auth.time.time',return_value=30000):
            self.assertFalse(sessions.valid(token))

    def test_failed_password_and_throttle(self):
        sessions=Sessions()
        for _ in range(30):
            self.assertIsNone(sessions.login(sessions.challenge(),'wrong','expected')[0])
        self.assertEqual(sessions.login(sessions.challenge(),'expected','expected')[1],'limited')

    def test_hosted_session_survives_restart_and_password_change_revokes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(str(Path(temp)/'sessions.sqlite'))
            first=Sessions(store,'name:private-password-test')
            token,status=first.login(first.challenge(),'name:private-password-test','name:private-password-test')
            self.assertEqual(status,'ok')
            second=Sessions(store,'name:private-password-test')
            self.assertTrue(second.valid(token))
            self.assertFalse(Sessions(store,'name:new-password').valid(token))
            self.assertNotIn(token,str(store.status()))
            second.logout(token)
            self.assertFalse(first.valid(token))

    def test_browser_login_origin_cookie_logout_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(str(Path(temp)/'db.sqlite'))
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(store,'avobati:private-password-test'))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                con=http.client.HTTPConnection(*server.server_address)
                con.request('GET','/')
                response=con.getresponse()
                self.assertEqual(response.status,302)
                response.read()
                con.request('GET','/login')
                response=con.getresponse()
                nonce_cookie=response.getheader('Set-Cookie')
                page=response.read().decode()
                nonce=re.search(r'name="nonce" value="([^"]+)"',page).group(1)
                self.assertIn('HttpOnly',nonce_cookie)
                self.assertIn('SameSite=Strict',nonce_cookie)
                body=urlencode({'username':'avobati','password':'private-password-test','nonce':nonce})
                bad_body=urlencode({'username':'avobati','password':'wrong-password','nonce':nonce})
                local_origin=f'http://127.0.0.1:{server.server_port}'
                con.request('POST','/login',bad_body,{'Cookie':nonce_cookie.split(';')[0],'Content-Type':'application/x-www-form-urlencoded','Origin':local_origin})
                response=con.getresponse()
                self.assertEqual(response.status,303)
                self.assertEqual(response.getheader('Location'),'/login?error=1')
                response.read()
                con.request('GET','/login?error=1')
                response=con.getresponse()
                nonce_cookie=response.getheader('Set-Cookie')
                page=response.read().decode()
                self.assertIn('Sign-in failed',page)
                nonce=re.search(r'name="nonce" value="([^"]+)"',page).group(1)
                body=urlencode({'username':'avobati','password':'private-password-test','nonce':nonce})
                headers={'Cookie':nonce_cookie.split(';')[0],'Content-Type':'application/x-www-form-urlencoded','Origin':'https://evil.example'}
                con.request('POST','/login',body,headers)
                response=con.getresponse()
                self.assertEqual(response.status,403)
                response.read()
                headers['Origin']=f'http://127.0.0.1:{server.server_port}'
                con.request('POST','/login',body,headers)
                response=con.getresponse()
                self.assertEqual(response.status,303)
                session=response.getheader('Set-Cookie').split(';')[0]
                self.assertNotIn('private-password-test',session)
                response.read()
                con.request('GET','/',headers={'Cookie':session})
                response=con.getresponse()
                self.assertEqual(response.status,200)
                response.read()
                con.request('POST','/api/launches','{}',{'Cookie':session})
                response=con.getresponse()
                self.assertEqual(response.status,405)
                response.read()
                con.request('POST','/logout','',{'Cookie':session,'Origin':headers['Origin']})
                response=con.getresponse()
                self.assertEqual(response.status,303)
                response.read()
                con.request('GET','/api/reports',headers={'Cookie':session})
                response=con.getresponse()
                self.assertEqual(response.status,401)
                response.read()
                con.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
