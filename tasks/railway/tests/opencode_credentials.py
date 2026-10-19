"""Credential selection regressions with fake secrets and a disposable database."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from run_opencode import provider_credential


class CredentialSelection(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / 'opencode.db'
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('CREATE TABLE credential(integration_id TEXT,time_updated INTEGER,value TEXT)')
            connection.executemany('INSERT INTO credential VALUES(?,?,?)', [
                ('target', 1, json.dumps({'type':'api','key':'old-fake'})),
                ('target', 2, json.dumps({'type':'key','key':'new-fake'})),
                ('unrelated', 3, json.dumps({'type':'key','key':'other-fake'})),
                ('oauth', 4, json.dumps({'type':'oauth','access':'oauth-fake'}))])
            connection.commit()

    def test_only_selected_provider_and_latest_key(self):
        self.assertEqual(provider_credential(None,self.database,'target'),{'type':'api','key':'new-fake'})

    def test_database_not_modified(self):
        before=self.database.read_bytes()
        provider_credential(None,self.database,'target')
        self.assertEqual(before,self.database.read_bytes())

    def test_unknown_provider_does_not_fall_back(self):
        with self.assertRaises(RuntimeError):
            provider_credential(None,self.database,'absent')

    def test_oauth_is_not_silently_used_as_api_key(self):
        with self.assertRaises(RuntimeError):
            provider_credential(None,self.database,'oauth')

    def test_legacy_auth_file(self):
        path=self.root/'auth.json'
        path.write_text(json.dumps({'target':{'type':'api','key':'legacy-fake'}}),encoding='utf-8-sig')
        self.assertEqual(provider_credential(path,None,'target'),{'type':'api','key':'legacy-fake'})


if __name__=='__main__':
    unittest.main()
