from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_publication as publication


class PublicationTests(unittest.TestCase):
    def test_private_paths_and_bound_templates_are_rejected(self):
        self.assertIn('private-data-path', publication.check_text('accounts/test/profile.json', b'{}'))
        self.assertIn('private-data-path', publication.check_text('settings.local.json', b'{}'))
        self.assertIn('bound-example-identity', publication.check_text(
            'examples/account.json', b'{"expected_identity":{"kind":"user_id","value":"synthetic"}}'))
        self.assertIn('nonempty-example-profile', publication.check_text(
            'examples/profile.json', b'{"name":"Synthetic Person"}'))

    def test_private_values_and_home_paths_are_reported_without_values(self):
        home = ('C:' + chr(92) + 'Users' + chr(92) + 'synthetic-owner' + chr(92) + 'file').encode()
        self.assertEqual(publication.check_text('README.md', home), ['personal-home-path'])
        self.assertEqual(publication.check_text('scripts/a.py', b'private-fixture-value', ['private-fixture-value']),
                         ['local-private-value'])

    def test_source_export_excludes_git_and_local_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'
            root.mkdir()
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            (root / '.gitignore').write_text('accounts/\nconnections/\n.account-runtime/\n', encoding='utf-8')
            (root / 'README.md').write_text('Synthetic public source', encoding='utf-8')
            for name in ('accounts', 'connections', '.account-runtime'):
                (root / name).mkdir()
                (root / name / 'private.json').write_text('{"private":true}', encoding='utf-8')
            export = Path(folder) / 'export'
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check', '--export', str(export)]), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(publication.main(), 0)
            self.assertEqual(set(p.name for p in export.iterdir()), {'.gitignore', 'README.md'})
            self.assertEqual(json.loads(out.getvalue())['findings'], [])
            # A forced tracked private file must fail rather than silently export.
            subprocess.run(['git', '-C', str(root), 'add', '-f', 'accounts/private.json'], check=True, capture_output=True)
            refused = Path(folder) / 'refused'
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check', '--export', str(refused)]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(publication.main(), 1)
            self.assertFalse(refused.exists())

    def test_history_keeps_private_path_after_same_blob_is_renamed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.run(['git', '-C', folder, '-c', 'user.name=Synthetic Fixture',
                                       '-c', 'user.email=fixture@example.invalid', *args],
                                      check=True, capture_output=True)
            git('init')
            private = root / 'accounts/private-record.json'
            private.parent.mkdir()
            private.write_text('{}', encoding='utf-8')
            git('add', '.')
            git('commit', '-m', 'Synthetic private fixture')
            private.rename(root / 'public.json')
            git('add', '-A')
            git('commit', '-m', 'Rename identical content')
            with patch.object(publication, 'ROOT', root):
                history = publication.history_files()
            names = {name for _, name in history}
            self.assertIn('accounts/private-record.json', names)
            self.assertIn('public.json', names)
            self.assertTrue(any('private-data-path' in publication.check_text(name, content)
                                for (_, name), content in history.items()))


if __name__ == '__main__':
    unittest.main()
