"""Hosted read-only radar. Never exposes the legacy launch or pairing API."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from automation import load_env
from radar.store import Store
from radar.validation import report

STATIC = Path(__file__).resolve().parents[1] / 'static' / 'radar'


def current_rows(store):
    ranking = store.status('ranking').get('ranking', {})
    run_id = ranking.get('run_id')
    rows = [r for r in store.history('decisions', limit=100000) if r.get('run_id') == run_id]
    return sorted([{k:v for k,v in r.items() if k not in ('risk_evidence','research')} for r in rows], key=lambda r:r['ranks']['gem'])


def public_health(store):
    result = {}
    for key in ('worker','ranking','provider:dex','provider:gecko','provider:rugcheck'):
        result.update(store.status(key))
    return result


def handler_for(store, credentials):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, payload, mime='application/json; charset=utf-8'):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header('Referrer-Policy', 'no-referrer')
            if status == 401:
                self.send_header('WWW-Authenticate', 'Basic realm="Gem Radar", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                try:
                    store.status('worker')
                    return self.send(200, {'status':'ok'})
                except Exception:
                    return self.send(503, {'status':'database_unavailable'})
            expected = 'Basic ' + base64.b64encode(credentials.encode()).decode()
            if credentials and not secrets.compare_digest(self.headers.get('Authorization', ''), expected):
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

        def do_POST(self):
            self.send(405, {'error':'Read-only research service'})
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
