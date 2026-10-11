"""Explicitly opt-in CLI smoke tests. They use the caller's Claude usage/credentials.

CLAUDE_CREW_LIVE_TESTS=1 python -m unittest discover -s tests -p test_live_cli.py -v
"""

import json
import os
import subprocess
import sys
import unittest

from test_runner import RELAY, RelayHarness


@unittest.skipUnless(os.environ.get('CLAUDE_CREW_LIVE_TESTS') == '1', 'live Claude calls are opt-in')
class LiveCliTests(RelayHarness):
    def setUp(self):
        super().setUp()
        self.env.pop('CLAUDE_CREW_BIN')
        self.env.pop('FAKE_CLAUDE_LOG')
        if os.environ.get('CLAUDE_CREW_BIN'):
            self.env['CLAUDE_CREW_BIN'] = os.environ['CLAUDE_CREW_BIN']
        code, preflight = self.relay('preflight')
        self.assertEqual(code, 0, preflight)
        self.model = os.environ.get('CLAUDE_CREW_LIVE_MODEL', 'haiku')

    def live(self, *args, brief):
        proc = subprocess.run([sys.executable, str(RELAY), 'run', *args, '--brief', '-',
                               '--max-turns', '12', '--max-budget-usd', '1', '--timeout', '120'],
                              input=brief, capture_output=True, text=True, cwd=self.repo, env=self.env, timeout=160)
        record = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, record)
        self.assertEqual(record['status'], 'completed', record)
        self.assertIsNotNone(record['claimed'])
        return record

    def test_review_ignores_project_hook_and_keeps_files_unchanged(self):
        folder = self.repo / '.claude'
        folder.mkdir()
        (folder / 'settings.json').write_text(json.dumps({
            'permissions': {'allow': ['Bash(*)']},
            'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'touch hook-ran.txt'}]}]},
        }))
        record = self.live('--mode', 'review', '--model', self.model,
                           brief='Read README.md and return a short review using the required report schema. Do not edit anything.')
        self.assertFalse((self.repo / 'hook-ran.txt').exists())
        self.assertEqual((self.repo / 'README.md').read_text(), 'hello\n')
        self.assertIs(record['observed']['read_only_violation'], False)

    def test_implementation_and_resume_in_isolated_worktree(self):
        first = self.live('--mode', 'implement', '--worktree', 'live-smoke', '--model', self.model,
                          brief='Create crew-smoke.txt containing exactly one line: first. Return the required report. Do not commit or run shell commands.')
        from pathlib import Path
        target = Path(first['cwd']) / 'crew-smoke.txt'
        self.assertEqual(target.read_text().strip(), 'first')
        second = self.live('--resume', first['run_id'],
                           brief='Replace crew-smoke.txt with exactly one line: second. Return the required report. Do not commit or run shell commands.')
        self.assertEqual(target.read_text().strip(), 'second')
        self.assertEqual(first['claude']['session_id'], second['claude']['session_id'])
        self.assertFalse((self.repo / 'crew-smoke.txt').exists())
        self.assertEqual(second['observed']['commits'], [])
