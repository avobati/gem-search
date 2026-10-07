"""Portable transactional storage; observations and decisions are append-only."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

TABLES = ('observations', 'research', 'decisions', 'returns', 'reports', 'events')


class Session:
    def __init__(self, connection, postgres=False):
        self.connection, self.postgres = connection, postgres

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace('?', '%s') if self.postgres else sql, args)


class Store:
    def __init__(self, url=None):
        self.url = url or os.getenv('RADAR_DATABASE_URL', 'data/radar.sqlite')
        self.postgres = self.url.startswith(('postgres://', 'postgresql://'))
        if not self.postgres:
            Path(self.url).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if self.postgres:
                db.execute('SELECT pg_advisory_xact_lock(73492019)')
            db.execute('CREATE TABLE IF NOT EXISTS tokens (mint TEXT PRIMARY KEY, first_seen DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
            for table in TABLES:
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, mint TEXT NOT NULL, available_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
                db.execute(f'CREATE INDEX IF NOT EXISTS {table}_mint_time ON {table}(mint,available_at)')
            db.execute('CREATE TABLE IF NOT EXISTS operational (key TEXT PRIMARY KEY, updated DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
            if self.postgres:
                db.execute("""CREATE OR REPLACE FUNCTION radar_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
                            BEGIN RAISE EXCEPTION 'Radar evidence is append-only'; END; $$""")
                for table in ('tokens',)+TABLES:
                    # Separate update/delete triggers support idempotent schema init.
                    exists = db.execute('SELECT 1 FROM pg_trigger WHERE tgname=?', ('radar_immutable_'+table,)).fetchone()
                    if not exists:
                        db.execute(f'CREATE TRIGGER radar_immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION radar_append_only()')
            else:
                for table in ('tokens',)+TABLES:
                    for action in ('UPDATE','DELETE'):
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Radar evidence is append-only'); END")

    @contextmanager
    def connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            connection = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        else:
            connection = sqlite3.connect(self.url, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
        try:
            with connection:
                yield Session(connection, self.postgres)
        finally:
            connection.close()

    def append(self, table, mint, payload, stamp=None, identity=None, db=None):
        if table not in TABLES:
            raise ValueError('Unknown entity')
        stamp = time.time() if stamp is None else stamp
        identity = identity or uuid.uuid4().hex
        if db is None:
            with self.connect() as session:
                return self.append(table, mint, payload, stamp, identity, session)
        return db.execute(f'INSERT INTO {table} VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING',
                          (identity, mint, stamp, json.dumps(payload, allow_nan=False))).rowcount

    def discover(self, mint, identity, stamp=None):
        stamp = time.time() if stamp is None else stamp
        with self.connect() as db:
            added = db.execute('INSERT INTO tokens VALUES (?,?,?) ON CONFLICT(mint) DO NOTHING',
                               (mint, stamp, json.dumps(identity))).rowcount
            if added:
                self.append('events', mint, {'type': 'first_discovered', 'identity': identity}, stamp, db=db)
        return bool(added)

    def tokens(self):
        with self.connect() as db:
            return [dict(json.loads(r['payload']), mint=r['mint'], first_seen=r['first_seen'])
                    for r in db.execute('SELECT * FROM tokens ORDER BY first_seen DESC')]

    def history(self, table, mint=None, cutoff=None, limit=10000, fields=None):
        if table not in TABLES:
            raise ValueError('Unknown entity')
        clauses, args = ['available_at<=?'], [time.time() if cutoff is None else cutoff]
        if mint is not None:
            clauses.append('mint=?'); args.append(mint)
        args.append(limit)
        selection = '*'
        if fields:
            if any(not f.replace('_','').isalnum() for f in fields):
                raise ValueError('Invalid projected field')
            parts = ','.join(f"'{f}',payload::jsonb->'{f}'" if self.postgres else f"'{f}',json_extract(payload,'$.{f}')" for f in fields)
            function = 'json_build_object' if self.postgres else 'json_object'
            selection = f'id,mint,available_at,{function}({parts}) AS payload'
        with self.connect() as db:
            rows = db.execute(f'SELECT {selection} FROM {table} WHERE ' + ' AND '.join(clauses) +
                              ' ORDER BY available_at DESC,id DESC LIMIT ?', args)
            return [dict(r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload']), id=r['id'], mint=r['mint'], available_at=r['available_at']) for r in rows]

    def set_status(self, key, value, stamp=None):
        with self.connect() as db:
            db.execute('INSERT INTO operational VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET updated=excluded.updated,payload=excluded.payload',
                       (key, time.time() if stamp is None else stamp, json.dumps(value)))

    def status(self, key=None):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM operational WHERE key=?', (key,)) if key is not None else db.execute('SELECT * FROM operational')
            return {r['key']: dict(json.loads(r['payload']), updated=r['updated']) for r in rows}

    @contextmanager
    def worker_lock(self):
        # Lock spans the cycle; OS/connection releases it on crash.
        if self.postgres:
            with self.connect() as db:
                acquired = db.execute('SELECT pg_try_advisory_lock(73492018) AS acquired').fetchone()['acquired']
                try:
                    yield acquired
                finally:
                    if acquired:
                        db.execute('SELECT pg_advisory_unlock(73492018)')
        else:
            from radar.lock import file_lock
            with file_lock(self.url + '.worker.lock') as acquired:
                yield acquired
