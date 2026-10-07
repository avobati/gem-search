"""Reuse Gem Search crawler, capture evidence and narrative taxonomy."""
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from automation import TOPICS
from radar.engine import social_features


def captured_posts(cutoff):
    path = Path(os.getenv('GEM_CAPTURE_DATABASE', 'data/gem-search.sqlite')).resolve()
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        try:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM spider_posts WHERE created<=? AND created>?', (cutoff,cutoff-86400))]
        except sqlite3.OperationalError:
            return []


def investigate(store, mint, market, cutoff, crawler=None, reviewer=None):
    social = social_features(captured_posts(cutoff), mint, market.get('symbol'), market.get('name'), cutoff)
    narratives = [topic for topic, regex in TOPICS.items() if any(re.search(regex, p['text'], re.I) for p in social['posts'])]
    previous = store.history('research', mint, cutoff, limit=1)
    pages, errors, crawled = [], [], False
    if previous and cutoff-previous[0]['available_at'] < 1800:
        pages = previous[0].get('pages', [])
        errors = previous[0].get('errors', [])
        crawled = previous[0].get('crawled', False)
    else:
        if crawler is None:
            from app import crawl
            crawler = crawl
        # One official provider-linked website; crawler validates DNS/redirects.
        for url in market.get('websites', [])[:1]:
            result = crawler(url)
            pages += result['pages']; errors += result['errors']; crawled = True
    result = {'pages': pages, 'errors': errors, 'crawled': crawled, 'social': social, 'narratives': narratives,
              'claims': 'Retrieved pages demonstrate content only; product, code activity and team claims are unverified',
              'grok': {'status': 'disabled', 'votes': []}}
    if reviewer and social['authors'] >= 4:
        market_page = {'url':market['source_url'], 'text':json.dumps({k:v for k,v in market.items() if k not in ('raw','security_evidence')})[:2500]}
        security = market.get('security_evidence') or {}
        raw = security.get('raw') or {}
        risk_page = {'url':security.get('source_url',market['source_url']),
                     'text':json.dumps({k:raw.get(k) for k in ('token','risks','rugged','topHolders')})[:2500]}
        result['grok'] = reviewer.review({'name': market.get('name') or mint, 'token_context': True,
                                         'signals': social, 'posts': social['posts'], 'evidence': {'pages': [market_page,risk_page]+pages}})
    return result
