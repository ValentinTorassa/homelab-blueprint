import copy
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backups'))
import check_backup_consistency as cbc  # noqa: E402

EXAMPLES = cbc.EXAMPLES


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.cron = cbc.cron_families(EXAMPLES / 'cron.d-backups')
        self.offsite = cbc.offsite_lists(EXAMPLES / 'offsite.conf')
        self.restore = cbc.restore_test_families()
        self.inventory = json.loads((EXAMPLES / 'inventory.json').read_text())

    def check(self, **overrides):
        args = dict(cron=self.cron, offsite=self.offsite, restore=self.restore,
                    inventory=self.inventory)
        args.update(overrides)
        return cbc.check(**args)

    def test_examples_are_coherent(self):
        self.assertEqual(self.check(), [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cbc.main([]), 0)

    def test_parses_cron_time_and_keep(self):
        self.assertEqual(self.cron['mailer']['time'], '02:40')
        self.assertEqual(self.cron['mailer']['keep'], 14)

    def test_family_missing_from_restore_test(self):
        errors = self.check(restore=self.restore - {'blog'})
        self.assertEqual(errors, ['blog: missing from the restore test'])

    def test_documented_keep_differs_from_cron(self):
        inv = copy.deepcopy(self.inventory)
        inv['families']['app-a']['keep'] = 30
        where = self.cron['app-a']['where']
        self.assertIn(f'app-a: inventory says KEEP 30, {where} keeps 7', self.check(inventory=inv))

    def test_offsite_cadence_differs(self):
        offsite = dict(self.offsite, blog='daily')
        self.assertEqual(self.check(offsite=offsite),
                         ['blog: inventory says offsite weekly, the offsite lists say daily'])

    def test_deployed_app_without_backup(self):
        inv = copy.deepcopy(self.inventory)
        inv['apps'].append('app-c')
        self.assertEqual(self.check(inventory=inv),
                         ['app-c: deployed app with state but no backup family, gap or reason'])

    def test_closed_gap_must_be_removed(self):
        inv = copy.deepcopy(self.inventory)
        inv['known_gaps']['blog'] = 'old excuse'
        self.assertIn("blog: listed as a known gap but family 'blog' exists; remove the stale gap",
                      self.check(inventory=inv))

    def test_state_elsewhere_must_itself_be_covered(self):
        inv = copy.deepcopy(self.inventory)
        inv['state_elsewhere']['app-b'] = 'app-z'
        self.assertIn("app-b: its state lives in 'app-z', which is not backed up", self.check(inventory=inv))


class StaleNoteTests(unittest.TestCase):
    def test_dated_claims_expire_but_history_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / 'dr.md').write_text('Restore drill: last verified 2026-01-01.\n'
                                        'Offsite: last verified 2026-02-20.\n')
            (docs / 'runs').mkdir()
            (docs / 'runs' / 'drill.md').write_text('last verified 2025-01-01\n')
            errors = cbc.stale_notes(docs, 30, today=dt.date(2026, 3, 1))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith('dr.md:1: "last verified 2026-01-01" is 59 days old'))


if __name__ == '__main__':
    unittest.main()
