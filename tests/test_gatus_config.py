import copy
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'monitoring'))


@unittest.skipIf(yaml is None, 'PyYAML not installed')
class GatusConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import check_gatus_config as cgc
        cls.cgc = cgc
        cls.cfg = yaml.safe_load(cgc.DEFAULT_CONFIG.read_text())

    def cfg_copy(self):
        return copy.deepcopy(self.cfg)

    def test_example_config_is_clean(self):
        self.assertEqual(self.cgc.check_config(self.cfg), [])
        self.assertEqual(self.cgc.check_job_keys(self.cfg, 'Jobs', [self.cgc.REPO]), [])

    def test_go_durations(self):
        self.assertEqual(self.cgc.go_duration('192h'), 192 * 3600)
        self.assertEqual(self.cgc.go_duration('1h30m'), 5400)
        for bad in ('8d', '', '1w', 'h'):
            with self.assertRaises(ValueError, msg=bad):
                self.cgc.go_duration(bad)

    def test_days_in_a_heartbeat_are_caught(self):
        cfg = self.cfg_copy()
        cfg['external-endpoints'][1]['heartbeat']['interval'] = '8d'
        errors = self.cgc.check_config(cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn('not a Go duration', errors[0])

    def test_missing_heartbeat_is_caught(self):
        cfg = self.cfg_copy()
        del cfg['external-endpoints'][1]['heartbeat']
        self.assertIn('no heartbeat.interval', self.cgc.check_config(cfg)[0])

    def test_alert_without_provider_is_caught(self):
        cfg = self.cfg_copy()
        cfg['endpoints'][0]['alerts'] = [{'type': 'pagerduty'}]
        self.assertIn("alert type 'pagerduty' has no provider", self.cgc.check_config(cfg)[0])

    def test_typo_in_top_level_key(self):
        cfg = self.cfg_copy()
        cfg['endpoint'] = cfg.pop('endpoints')
        self.assertIn("unknown top-level key 'endpoint'", self.cgc.check_config(cfg)[0])

    def test_gatus_key(self):
        self.assertEqual(self.cgc.gatus_key('Jobs', 'backup-app-a'), 'jobs_backup-app-a')
        self.assertEqual(self.cgc.gatus_key('Public services', 'app-a review queue'),
                         'public-services_app-a-review-queue')

    def test_job_reporting_to_a_missing_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cron = Path(tmp) / 'cron.d-extra'
            cron.write_text('# run-and-report.sh in-a-comment is ignored\n'
                            '0 4 * * * ops run-and-report.sh nightly-report report.sh\n')
            errors = self.cgc.check_job_keys(self.cfg, 'Jobs', [Path(tmp)])
        self.assertEqual(len(errors), 1)
        self.assertIn("reports to 'nightly-report'", errors[0])
        self.assertIn('404', errors[0])


if __name__ == '__main__':
    unittest.main()
