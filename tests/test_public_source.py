"""The release check must examine staged content without reading private files."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


CHECK_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'check_public_source.py'
spec = importlib.util.spec_from_file_location('public_source_check', CHECK_PATH)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PublicSourceCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name)
        self.git('init', '--quiet')

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.root, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def check(self):
        with patch.object(checker, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return checker.main()

    def test_later_clean_working_tree_cannot_hide_staged_credential(self):
        source = self.root / 'example.py'
        source.write_text('gh' + 'p_' + 'A' * 40, encoding='utf-8')
        self.git('add', 'example.py')
        source.write_text('# cleaned after staging\n', encoding='utf-8')
        self.assertEqual(self.check(), 1)

    def test_unstaged_changes_do_not_replace_the_selected_blob(self):
        source = self.root / 'example.py'
        source.write_text('# clean source\n', encoding='utf-8')
        self.git('add', 'example.py')
        source.write_text('gh' + 'p_' + 'A' * 40, encoding='utf-8')
        self.assertEqual(self.check(), 0)

    def test_rejected_private_path_is_never_opened_or_read_as_blob(self):
        private = self.root / 'data' / 'profile.txt'
        private.parent.mkdir()
        private.write_text('generated fixture only', encoding='utf-8')
        self.git('add', 'data/profile.txt')
        real_run = subprocess.run
        with patch.object(checker.subprocess, 'run', wraps=real_run) as calls:
            self.assertEqual(self.check(), 1)
        self.assertEqual(len(calls.call_args_list), 1)
        self.assertEqual(calls.call_args.args[0][1:3], ['ls-files', '--stage'])


if __name__ == '__main__':
    unittest.main()
