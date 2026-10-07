"""Hosted read-only radar. Never exposes the legacy launch or pairing API."""
import argparse
import base64
from html import escape
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from radar.auth import Sessions, cookies
from automation import load_env
from radar.store import Store
from radar.validation import report

STATIC = Path(__file__).resolve().parents[1] / 'static' / 'radar'


def current_rows(store):
    ranking = store.status('ranking').get('ranking', {})
    run_id = ranking.get('run_id')
    if not run_id:
        return []
    rows = store.history('decisions', limit=100000, run_id=run_id, since=ranking.get('cutoff'),
                         fields=('features','risk','score','explanation','ranks','first_seen','cutoff','run_id','universe_size'))
    return sorted([{k:v for k,v in r.items() if k not in ('risk_evidence','research')} for r in rows], key=lambda r:r['ranks']['gem'])


def public_health(store):
    result = {}
    for key in ('worker','ranking','provider:dex','provider:gecko','provider:rugcheck'):
        result.update(store.status(key))
    return result


def handler_for(store, credentials):
    sessions = Sessions()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, payload, mime='application/json; charset=utf-8', headers=None):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header('Referrer-Policy', 'no-referrer')
            for key,value in (headers or {}).items():
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/login':
                if not credentials:
                    return self.send(302,b'',headers={'Location':'/'})
                nonce = sessions.challenge()
                if not nonce:
                    return self.send(429, {'error':'Please try again shortly'})
                query=parse_qs(urlsplit(self.path).query)
                message='Sign-in failed. Check your dashboard password and try again.' if 'error' in query else 'Your sign-in expired. Please try again.' if 'expired' in query else ''
                page = (STATIC/'login.html').read_text().replace('{{nonce}}',nonce).replace('{{username}}',escape(credentials.split(':',1)[0],quote=True)).replace('{{message}}',message)
                return self.send(200,page.encode(),'text/html; charset=utf-8',
                                 {'Set-Cookie':self.cookie('radar_nonce',nonce,600)})
            if path == '/radar.css':
                return self.send(200,(STATIC/'radar.css').read_bytes(),'text/css; charset=utf-8')
            if path == '/healthz':
                try:
                    store.status('worker')
                    return self.send(200, {'status':'ok'})
                except Exception:
                    return self.send(503, {'status':'database_unavailable'})
            expected = 'Basic ' + base64.b64encode(credentials.encode()).decode()
            authenticated = sessions.valid(cookies(self.headers.get('Cookie')).get('radar_session'))
            if credentials and not authenticated and not secrets.compare_digest(self.headers.get('Authorization', ''), expected):
                if path == '/':
                    return self.send(302,b'',headers={'Location':'/login'})
                return self.send(401, {'error':'Authentication required'})
            try:
                if path == '/api/radar':
                    rows = current_rows(store)
                    tokens = store.tokens()
                    metrics = report(store)
                    forward = next((r for r in metrics['metrics'] if r['method']=='gem' and r['horizon']=='24h' and r['threshold']==100), {})
                    return self.send(200, {'tokens':rows, 'kpis': {'scanned':len(tokens), 'rejected':sum(r['risk']['rejected'] for r in rows),
                                        'candidates':sum(r['score']['candidate'] for r in rows),
                                        'high_conviction':sum(r['score']['candidate'] and r['score']['total']>=80 for r in rows),
                                        'median_return':forward.get('median_return_pct'), 'hit_rate':forward.get('hit_rate')},
                                          'health': public_health(store),
                                          'generated_at':time.time(), 'scope':'Observed universe; uncalibrated research scores'})
                if path == '/api/reports':
                    return self.send(200, report(store))
                if path.startswith('/api/token/'):
                    mint = path.removeprefix('/api/token/')
                    from radar.providers import MINT
                    if not MINT.fullmatch(mint):
                        return self.send(400, {'error':'Invalid mint'})
                    token = next((t for t in store.tokens() if t['mint']==mint), None)
                    if not token:
                        return self.send(404, {'error':'Token not found'})
                    observations = store.history('observations',mint,limit=2000)
                    prices = sorted([r for r in observations if r.get('price')], key=lambda r:r['available_at'])
                    return self.send(200, {'token':token, 'discovery_price':prices[0]['price'] if prices else None,
                                          'discovery_price_observed_at':prices[0]['available_at'] if prices else None,
                                          'decisions':store.history('decisions',mint,limit=500),
                                          'observations':observations,
                                          'timeline':store.history('events',mint,limit=500),
                                          'returns':store.history('returns',mint,limit=500),
                                          'research':store.history('research',mint,limit=100)})
                if path == '/api/health':
                    state = public_health(store)
                    worker = state.get('worker', {})
                    ready = worker.get('state')!='failed' and time.time()-(worker.get('last_completed') or 0)<900 and any(v.get('healthy') and time.time()-v['updated']<900 for k,v in state.items() if k.startswith('provider:'))
                    return self.send(200 if ready else 503, {'ready':ready, 'worker':worker})
                files = {'/':('index.html','text/html; charset=utf-8'), '/radar.js':('radar.js','text/javascript; charset=utf-8'), '/radar.css':('radar.css','text/css; charset=utf-8')}
                if path in files:
                    name, mime = files[path]
                    return self.send(200, (STATIC/name).read_bytes(), mime)
                return self.send(404, {'error':'Not found'})
            except Exception:
                return self.send(503, {'error':'Data temporarily unavailable'})

        def cookie(self, name, value, age):
            local = urlsplit('http://'+self.headers.get('Host','')).hostname in ('localhost','127.0.0.1')
            return f'{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('' if local else '; Secure')

        def same_origin(self):
            origin = urlsplit(self.headers.get('Origin',''))
            local = origin.hostname in ('localhost','127.0.0.1')
            return (origin.scheme=='https' or (local and origin.scheme=='http')) and origin.netloc==self.headers.get('Host')

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ('/login','/logout'):
                return self.send(405, {'error':'Read-only research service'})
            if not self.same_origin():
                return self.send(403, {'error':'Origin validation failed'})
            jar = cookies(self.headers.get('Cookie'))
            if path == '/logout':
                sessions.logout(jar.get('radar_session'))
                return self.send(303,b'',headers={'Location':'/login','Set-Cookie':self.cookie('radar_session','',0)})
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 4096 or self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':
                    return self.send(400, {'error':'Invalid login request'})
                fields = parse_qs(self.rfile.read(size).decode('utf-8'),max_num_fields=4)
                nonce = fields.get('nonce',[''])[0]
                if not secrets.compare_digest(nonce.encode(),jar.get('radar_nonce','').encode()):
                    return self.send(303,b'',headers={'Location':'/login?expired=1'})
                supplied = fields.get('username',[''])[0]+':'+fields.get('password',[''])[0]
                token, status = sessions.login(nonce,supplied,credentials)
                if not token:
                    if status=='limited':
                        return self.send(429,{'error':'Too many sign-in attempts; try again in five minutes'})
                    return self.send(303,b'',headers={'Location':'/login?error=1'})
                return self.send(303,b'',headers={'Location':'/','Set-Cookie':self.cookie('radar_session',token,8*3600)})
            except (ValueError,UnicodeError):
                return self.send(400, {'error':'Invalid login request'})
    return Handler


def main():
    load_env(Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT','8790')))
    parser.add_argument('--with-worker', action='store_true')
    args = parser.parse_args()
    username, password = os.getenv('RADAR_USERNAME',''), os.getenv('RADAR_PASSWORD','')
    credentials = username+':'+password if username and password else ''
    if args.host not in ('127.0.0.1','localhost') and (not credentials or len(password)<20):
        raise SystemExit('Public binding requires RADAR_USERNAME and a RADAR_PASSWORD of at least 20 characters; terminate TLS at the host proxy')
    store = Store()
    if args.with_worker:
        from radar.worker import Worker
        worker = Worker(store)
        def loop():
            while True:
                try:
                    worker.cycle()
                except Exception as exc:
                    store.set_status('worker', {'state':'failed','error':type(exc).__name__,'failed_at':time.time()})
                time.sleep(30)
        threading.Thread(target=loop,daemon=True).start()
    server = ThreadingHTTPServer((args.host,args.port),handler_for(store,credentials))
    server.daemon_threads = True
    print(f'Gem Radar listening on {args.host}:{args.port}',flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
