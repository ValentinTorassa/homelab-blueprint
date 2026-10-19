"""run-and-report.sh + report-job.sh, with a fake curl that records the push."""
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

JOBS = Path(__file__).resolve().parents[1] / 'monitoring' / 'jobs'

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$FAKE_CURL_ARGS"
printf '%s' "${FAKE_CURL_CODE:-200}"
"""


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        curl = self.root / 'curl'
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.env_file = self.root / 'gatus-jobs.env'
        self.env_file.write_text('GATUS_BASE_URL=https://status.example.internal\n'
                                 'GATUS_TOKEN_BACKUP_APP_A=t0k3n\n')
        self.args_file = self.root / 'curl-args'
        self.log = self.root / 'job-reports.log'
        self.env = dict(os.environ, CURL_BIN=str(curl), JOBS_ENV_FILE=str(self.env_file),
                        JOBS_LOG_FILE=str(self.log), FAKE_CURL_ARGS=str(self.args_file))
        self.env.pop('REPORT_AS', None)

    def run_wrapped(self, *cmd, code='200'):
        env = dict(self.env, FAKE_CURL_CODE=code)
        return subprocess.run([str(JOBS / 'run-and-report.sh'), 'backup-app-a', *cmd],
                              capture_output=True, text=True, env=env)

    def pushed(self):
        args = self.args_file.read_text().splitlines()
        params = dict(urllib.parse.parse_qsl('&'.join(args[i + 1] for i, a in enumerate(args)
                                                       if a == '--data-urlencode')))
        return args, params

    def test_success_pushes_success_and_keeps_stdout(self):
        proc = self.run_wrapped('bash', '-c', 'echo progress')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, 'progress\n')
        args, params = self.pushed()
        self.assertIn('https://status.example.internal/api/v1/endpoints/jobs_backup-app-a/external', args)
        self.assertIn('Authorization: Bearer t0k3n', args)
        self.assertEqual(params['success'], 'true')

    def test_failure_keeps_the_exit_code_and_sends_the_stderr_tail(self):
        proc = self.run_wrapped('bash', '-c', 'echo noise >&2; echo "disk full" >&2; exit 3')
        self.assertEqual(proc.returncode, 3)
        self.assertIn('disk full', proc.stderr)              # still in the job's own log
        _, params = self.pushed()
        self.assertEqual(params['success'], 'false')
        self.assertTrue(params['error'].startswith('exit 3:'))
        self.assertIn('disk full', params['error'])

    def test_unknown_endpoint_is_logged_as_404_not_as_unreachable(self):
        proc = self.run_wrapped('true', code='404')
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ERROR 404: Gatus does not know 'jobs_backup-app-a'", self.log.read_text())

    def test_monitor_down_never_breaks_the_job(self):
        proc = self.run_wrapped('true', code='000')
        self.assertEqual(proc.returncode, 0)
        self.assertIn('Gatus unreachable (http=000)', self.log.read_text())

    def test_missing_config_is_logged_and_the_job_still_succeeds(self):
        self.env['JOBS_ENV_FILE'] = str(self.root / 'nope.env')
        proc = self.run_wrapped('true')
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.args_file.exists())
        self.assertIn('MISSING', self.log.read_text())

    def test_reporter_rejects_bad_keys(self):
        proc = subprocess.run([str(JOBS / 'report-job.sh'), '../etc', 'ok'],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
