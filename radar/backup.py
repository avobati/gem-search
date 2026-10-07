"""Consistent evidence export and atomic restore into an empty radar database."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from radar.store import Store, TABLES

ENTITIES = ('tokens',)+TABLES


def export(store, destination):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256()
    counts={}
    # Exclusive creation prevents replacing an existing backup by mistake.
    with destination.open('x',encoding='utf-8',newline='\n') as output, store.connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY' if store.postgres else 'BEGIN')
        def write(record):
            line=json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n'
            output.write(line)
            digest.update(line.encode())
        write({'format':'gem-radar-evidence-v1','exported_at':time.time()})
        for table in ENTITIES:
            counts[table]=0
            cursor=db.execute(f'SELECT * FROM {table} ORDER BY mint')
            while batch:=cursor.fetchmany(250):
                for row in batch:
                    write({'table':table,'row':dict(row)})
                    counts[table]+=1
        output.write(json.dumps({'manifest':{'sha256':digest.hexdigest(),'counts':counts}},sort_keys=True)+'\n')
    return counts


def validate(source):
    digest=hashlib.sha256()
    counts={table:0 for table in ENTITIES}
    manifest=None
    header=False
    with Path(source).open(encoding='utf-8',newline='') as data:
        for index,line in enumerate(data):
            record=json.loads(line)
            if manifest is not None:
                raise ValueError('Data follows the backup manifest')
            if 'manifest' in record:
                manifest=record['manifest']
                continue
            digest.update(line.encode())
            if index==0:
                if record.get('format')!='gem-radar-evidence-v1':
                    raise ValueError('Unsupported backup format')
                header=True
                continue
            table=record.get('table')
            if table not in counts or not isinstance(record.get('row'),dict):
                raise ValueError('Invalid backup row')
            counts[table]+=1
    if not header or not manifest or manifest.get('sha256')!=digest.hexdigest() or manifest.get('counts')!=counts:
        raise ValueError('Backup checksum/count mismatch')
    return counts


def restore(store, source):
    counts=validate(source)
    with store.connect() as db:
        db.execute('LOCK TABLE '+','.join(ENTITIES)+' IN ACCESS EXCLUSIVE MODE' if store.postgres else 'BEGIN IMMEDIATE')
        for table in ENTITIES:
            if db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                raise ValueError('Restore requires an empty evidence database')
        with Path(source).open(encoding='utf-8') as data:
            next(data)
            for line in data:
                record=json.loads(line)
                if 'manifest' in record:
                    break
                table,row=record['table'],record['row']
                if table not in ENTITIES:
                    raise ValueError('Invalid backup entity')
                if table=='tokens':
                    db.execute('INSERT INTO tokens VALUES (?,?,?)',(row['mint'],row['first_seen'],row['payload']))
                else:
                    db.execute(f'INSERT INTO {table} VALUES (?,?,?,?)',(row['id'],row['mint'],row['available_at'],row['payload']))
        # Operational caches/secrets are excluded. Restore only the latest cohort
        # pointer, so stored decisions are inspectable before collection resumes.
        row=db.execute('SELECT payload FROM decisions ORDER BY available_at DESC LIMIT 1').fetchone()
        if row:
            decision=json.loads(row['payload'])
            status={k:decision.get(k) for k in ('run_id','cutoff','universe_size')}
            db.execute('INSERT INTO operational VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET updated=excluded.updated,payload=excluded.payload',
                       ('ranking',time.time(),json.dumps(status)))
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('export','validate','restore'))
    parser.add_argument('path')
    parser.add_argument('--database',help='SQLite path or PostgreSQL DSN; prefer RADAR_DATABASE_URL for secret DSNs')
    args=parser.parse_args()
    result=validate(args.path) if args.action=='validate' else globals()[args.action](Store(args.database),args.path)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    main()
