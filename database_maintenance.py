import sqlite3
from datetime import datetime
from pathlib import Path


def database_health(database_file):
    path = Path(database_file)
    if not path.exists():
        return {'exists': False, 'size_bytes': 0, 'integrity': '未创建', 'tables': []}
    with sqlite3.connect(path) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {'exists': True, 'size_bytes': path.stat().st_size, 'integrity': integrity, 'tables': tables}


def backup_database(database_file, backup_folder, timestamp):
    source = Path(database_file)
    destination_folder = Path(backup_folder)
    if not source.exists():
        raise FileNotFoundError('结果数据库尚未创建')
    destination_folder.mkdir(parents=True, exist_ok=True)
    target = destination_folder / 'results_{0}.db'.format(timestamp)
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)
    return target


def list_backups(backup_folder):
    folder = Path(backup_folder)
    if not folder.exists():
        return []
    items = []
    for file in sorted(folder.glob('results_*.db'), key=lambda item: item.stat().st_mtime, reverse=True):
        items.append({'name': file.name, 'size_bytes': file.stat().st_size, 'modified_at': datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    return items


def prune_backups(backup_folder, keep_count):
    backups = list_backups(backup_folder)
    removed = []
    for item in backups[max(0, int(keep_count)):]:
        path = Path(backup_folder) / item['name']
        path.unlink(missing_ok=True)
        removed.append(item['name'])
    return removed
