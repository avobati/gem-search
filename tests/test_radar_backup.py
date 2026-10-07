import json
from pathlib import Path
import tempfile
import unittest
from radar.backup import export,validate,restore
from radar.store import Store


class BackupTests(unittest.TestCase):
    def test_roundtrip_preserves_ids_cutoffs_first_seen_and_immutability(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Store(str(Path(temp)/'source.sqlite'))
            source.discover('mint',{'source':'test'},1000)
            source.append('decisions','mint',{'run_id':'cohort','cutoff':1100,'universe_size':1},1100,identity='decision')
            source.set_status('cache:private-endpoint',{'data':'excluded'})
            backup=Path(temp)/'backup.jsonl'
            counts=export(source,backup)
            self.assertEqual(validate(backup),counts)
            target=Store(str(Path(temp)/'target.sqlite'))
            self.assertEqual(restore(target,backup),counts)
            self.assertEqual(target.tokens(),source.tokens())
            self.assertEqual(target.history('decisions',cutoff=1200),source.history('decisions',cutoff=1200))
            self.assertEqual(target.status('ranking')['ranking']['run_id'],'cohort')
            self.assertNotIn('cache:private-endpoint',target.status())
            with self.assertRaises(Exception):
                with target.connect() as db:
                    db.execute("UPDATE decisions SET payload='{}'")
            with self.assertRaises(ValueError):
                restore(target,backup)
            with self.assertRaises(FileExistsError):
                export(source,backup)

    def test_corrupt_or_incomplete_backup_cannot_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Store(str(Path(temp)/'source.sqlite'))
            source.discover('mint',{'source':'test'},1000)
            backup=Path(temp)/'backup.jsonl'
            export(source,backup)
            backup.write_text(backup.read_text().replace('1000','2000'),encoding='utf-8')
            target=Store(str(Path(temp)/'target.sqlite'))
            with self.assertRaises(ValueError):
                restore(target,backup)
            self.assertEqual(target.tokens(),[])
            backup.write_text('{"manifest":{"counts":{},"sha256":"bad"}}\n',encoding='utf-8')
            with self.assertRaises(ValueError):
                validate(backup)
