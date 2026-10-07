"""Persistent age-aware discovery, research, frozen cohorts and forward labels."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
import signal
import threading
import time
from automation import load_env
from pathlib import Path
from radar.engine import features, risk_assessment, score_token, explain, primary, baseline_order, HORIZONS
from radar.providers import Client, Providers
from radar.research import investigate
from radar.store import Store


def cadence(age):
    return 60 if age < 3600 else 180 if age < 21600 else 600 if age < 86400 else 1800


class Worker:
    def __init__(self, store, providers=None, researcher=None):
        self.store = store
        self.providers = providers or Providers(Client(store))
        self.researcher = researcher or investigate
        self.reviewer = None
        if os.getenv('GROK_ENABLED') == '1':
            import app
            from grok import GrokReview
            app.init()
            self.reviewer = GrokReview(app.connect)

    def cycle(self):
        with self.store.worker_lock() as acquired:
            if not acquired:
                return False
            self.store.set_status('worker', {'state': 'running', 'last_started': time.time()})
            discovered, observations, errors = self.providers.discover()
            for mint, identity in discovered.items():
                self.store.discover(mint, identity)
            for row in observations:
                self.record(row)
            tokens = self.store.tokens()
            # Bounded cycle, prioritize never-enriched and overdue youngest tokens.
            due = []
            now = time.time()
            for token in tokens:
                last = self.store.history('observations', token['mint'], limit=1)
                if not last or now-last[0]['available_at'] >= cadence(now-token['first_seen']):
                    due.append(dict(token, last_market=last[0]['available_at'] if last else 0))
            # Never-enriched mints first, then relative overdue time; old tokens
            # cannot starve forever when the new-pool feed stays busy.
            due.sort(key=lambda t: (now-t['last_market'])/cadence(now-t['first_seen']), reverse=True)
            for token in due[:int(os.getenv('RADAR_MARKETS_PER_CYCLE', '15'))]:
                mint = token['mint']
                rows, failures = self.providers.markets(mint)
                for row in rows:
                    self.record(row)
                if failures:
                    self.store.append('events', mint, {'type': 'provider_gap', 'errors': failures})
            self.rank()
            from radar.validation import label_returns, report
            label_returns(self.store)
            day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            last_report = self.store.history('reports', 'daily', limit=1)
            if not last_report or last_report[0].get('day') != day:
                self.store.append('reports', 'daily', dict(report(self.store), day=day))
            self.store.set_status('worker', {'state': 'idle', 'last_completed': time.time(), 'discovered': len(discovered), 'errors': errors})
            return True

    def record(self, row):
        # Same payload from the response cache cannot create fresh evidence.
        digest = hashlib.sha256(repr(row).encode()).hexdigest()
        last = self.store.history('observations', row['mint'], limit=10)
        if any(r.get('digest') == digest for r in last):
            return
        self.store.append('observations', row['mint'], dict(row, digest=digest), stamp=row.get('fetched_at'))
        if not last:
            self.store.append('events', row['mint'], {'type':'first_market_observed', 'price':row.get('price'),
                              'source':row.get('provider'), 'pool':row.get('pool')}, stamp=row.get('fetched_at'))

    def rank(self):
        state = self.store.status('ranking').get('ranking', {})
        if time.time()-state.get('updated', 0) < 300:
            return
        # All enrichment happens before a shared decision cutoff.
        rows = []
        risk_budget, research_budget = 15, 10
        for token in self.store.tokens():
            mint = token['mint']
            history = self.store.history('observations', mint, limit=5000)
            market = primary(history[:30])
            if market is None:
                continue
            risk_cache = self.store.status('risk:' + mint).get('risk:' + mint)
            if not risk_cache or time.time()-risk_cache['updated'] > 1800:
                if risk_budget:
                    evidence = self.providers.risk(mint)
                    self.store.set_status('risk:' + mint, evidence)
                    risk_budget -= 1
                else:
                    evidence = risk_cache or {'raw':None, 'observed_at':0, 'error':'Risk budget deferred'}
            else:
                evidence = risk_cache
            previous = self.store.history('research', mint, limit=1)
            if previous and time.time()-previous[0]['available_at'] < 1800:
                research = previous[0]
            else:
                if research_budget:
                    research = self.researcher(self.store, mint, dict(market, security_evidence=evidence), time.time(), reviewer=self.reviewer)
                    self.store.append('research', mint, research)
                    research_budget -= 1
                else:
                    research = previous[0] if previous else {'pages':[], 'social':{}, 'crawled':False, 'grok':{'status':'deferred','votes':[]}}
            rows.append((token, evidence, research))
        cutoff = time.time()
        decisions = []
        for token, evidence, research in rows:
            history = self.store.history('observations', token['mint'], cutoff, limit=5000)
            f = features(history, cutoff)
            risk = risk_assessment(f, evidence, cutoff)
            score = score_token(f, risk, research)
            decisions.append({'mint': token['mint'], 'first_seen': token['first_seen'], 'features': f, 'risk': risk,
                              'score': score, 'research': research, 'risk_evidence': evidence,
                              'explanation': explain(f, risk, score, research)})
        run_id = str(int(cutoff))
        ranks = {}
        for method in ('gem', 'random', 'volume', 'liquidity', 'momentum', 'gainer'):
            ranks[method] = {r['mint']: i+1 for i,r in enumerate(baseline_order(decisions, method, run_id))}
        with self.store.connect() as db:
            for row in decisions:
                row.update(run_id=run_id, cutoff=cutoff, ranks={k:v[row['mint']] for k,v in ranks.items()}, universe_size=len(decisions))
                self.store.append('decisions', row['mint'], row, cutoff, identity=run_id+':'+row['mint'], db=db)
                self.store.append('events', row['mint'], {'type':'ranked', 'gem_score':row['score']['total'],
                                  'rank':row['ranks']['gem'], 'candidate':row['score']['candidate']}, cutoff, db=db)
        self.store.set_status('ranking', {'run_id': run_id, 'universe_size': len(decisions), 'cutoff': cutoff})
        from radar.alerts import notify
        notify(self.store, decisions)


def main():
    load_env(Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    worker = Worker(Store())
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            worker.cycle()
        except Exception as exc:
            worker.store.set_status('worker', {'state': 'failed', 'error': type(exc).__name__, 'failed_at': time.time()})
            if args.once:
                raise
        if args.once:
            return
        stop.wait(30)


if __name__ == '__main__':
    main()
