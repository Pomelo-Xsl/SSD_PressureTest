import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime_ops import backup_database, database_health, list_backups, prune_backups


class DatabaseMaintenanceTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ssd-db-test-'))
        self.database = self.root / 'results.db'
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)')
            connection.execute("INSERT INTO records(value) VALUES ('ok')")

    def tearDown(self):
        for file in self.root.rglob('*'):
            if file.is_file():
                file.unlink()
        for folder in sorted([item for item in self.root.rglob('*') if item.is_dir()], reverse=True):
            folder.rmdir()
        self.root.rmdir()

    def test_health_and_backup_keep_contents(self):
        health = database_health(self.database)
        self.assertEqual(health['integrity'], 'ok')
        backup = backup_database(self.database, self.root / 'backups', '20260729_120000')
        self.assertTrue(backup.exists())
        self.assertEqual(sqlite3.connect(backup).execute('SELECT value FROM records').fetchone()[0], 'ok')

    def test_prune_keeps_requested_number(self):
        folder = self.root / 'backups'
        for index in range(3):
            backup_database(self.database, folder, '20260729_12000{0}'.format(index))
        self.assertEqual(len(list_backups(folder)), 3)
        self.assertEqual(len(prune_backups(folder, 1)), 2)
        self.assertEqual(len(list_backups(folder)), 1)


if __name__ == '__main__':
    unittest.main()
