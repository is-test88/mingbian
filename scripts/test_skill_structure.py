#!/usr/bin/env python3
"""本仓库的结构与合成示例回归；不执行模型行为评测。"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

import mingbian_session as session
import validate_skill

ROOT = Path(__file__).resolve().parents[1]


class SkillStructureTests(unittest.TestCase):
    def test_release_structure_is_valid(self):
        result = validate_skill.validate(ROOT)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['checks']['trigger_inputs_prepared_not_run'], 19)
        self.assertEqual(result['checks']['behavior_inputs_prepared_not_run'], 26)

    def test_missing_reference_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'mingbian'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'references/examples.md').unlink()
            result = validate_skill.validate(root)
            self.assertFalse(result['ok'])
            self.assertTrue(any('references/examples.md' in error for error in result['errors']))

    def test_oversized_description_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'mingbian'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('__pycache__'))
            path = root / 'SKILL.md'
            path.write_text(path.read_text(encoding='utf-8').replace('description: >-', 'description: >-\n  ' + '长' * 1025), encoding='utf-8')
            result = validate_skill.validate(root)
            self.assertFalse(result['ok'])
            self.assertTrue(any('1024' in error for error in result['errors']))

    def test_version_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'mingbian'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('__pycache__'))
            path = root / 'SKILL.md'
            path.write_text(path.read_text(encoding='utf-8').replace('version: "3.2.0"', 'version: "0.0.0"'), encoding='utf-8')
            result = validate_skill.validate(root)
            self.assertFalse(result['ok'])
            self.assertTrue(any('主文件版本' in error for error in result['errors']))

    def test_documented_example_roundtrip(self):
        """Use the real parser/command handlers with temporary storage and shipped fixtures."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / 'sessions'
            parser = session.build_parser()
            def run(*args):
                return session.run(parser.parse_args(['--store-dir', str(store), *args]))
            tree = str(ROOT / 'examples/tree.json')
            self.assertEqual(run('validate-tree', '--tree', tree)['branch_count'], 3)
            state = run('init', '--session', 'example', '--tree', tree,
                        '--plan', str(ROOT / 'examples/sample-plan.md'), '--stage', 'pilot')
            self.assertEqual(state['revision'], 1)
            state = run('apply', '--session', 'example', '--changes', str(ROOT / 'examples/changes.json'))
            self.assertEqual(state['revision'], 2)
            self.assertTrue(state['graph_resolved'])
            self.assertFalse(state['can_confirm'])
            state = run('review', '--session', 'example', '--expected-revision', '2',
                        '--record', str(ROOT / 'examples/mingbian-record.md'),
                        '--checks', str(ROOT / 'examples/review-checks.json'))
            self.assertTrue(state['can_confirm'])
            output = Path(tmp) / 'record.md'
            run('export', '--session', 'example', '--output', str(output))
            self.assertIn('试点就绪', output.read_text(encoding='utf-8'))
            state = run('confirm', '--session', 'example', '--expected-revision', '3',
                        '--confirmation', '纯合成自动化测试：确认演示快照，绝非真实用户授权')
            self.assertEqual(state['state'], 'closed')
            self.assertFalse(state['external_execution_authorized'])


if __name__ == '__main__':
    unittest.main()
