import json
import sqlite3
from pathlib import Path


class OperationsStore:

    def __init__(self, database_file):
        self.database_file = Path(database_file)

    def connect(self):
        connection = sqlite3.connect(self.database_file, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS test_results (task_id TEXT PRIMARY KEY, device TEXT NOT NULL, serial TEXT, device_path TEXT, plan TEXT, mode TEXT, task_status TEXT NOT NULL, conclusion TEXT, score INTEGER, risk_level TEXT, started_at TEXT, ended_at TEXT, progress REAL, result_json TEXT NOT NULL, analysis_json TEXT NOT NULL, report_html TEXT NOT NULL, archived_at TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_test_results_ended_at ON test_results(ended_at DESC)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_test_results_device ON test_results(device)')
            connection.execute('CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, action TEXT NOT NULL, task_id TEXT, device_path TEXT, severity TEXT NOT NULL, detail_json TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_audit_events_time ON audit_events(event_time DESC)')
            connection.execute('CREATE TABLE IF NOT EXISTS test_batches (batch_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, total_count INTEGER NOT NULL, completed_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS test_batch_items (batch_id TEXT NOT NULL, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL, PRIMARY KEY(batch_id, task_id))')
            connection.execute('CREATE TABLE IF NOT EXISTS device_assets (asset_id TEXT PRIMARY KEY, serial TEXT, model TEXT, last_path TEXT, interface TEXT, capacity TEXT, labels_json TEXT NOT NULL, note TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, risk_level TEXT, risk_score INTEGER)')
            connection.execute('CREATE TABLE IF NOT EXISTS device_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL, captured_at TEXT NOT NULL, firmware TEXT, health REAL, temperature REAL, payload_json TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_device_snapshots_asset_time ON device_snapshots(asset_id, captured_at DESC)')
            connection.execute('CREATE TABLE IF NOT EXISTS metric_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, sampled_at TEXT, temperature REAL, p99_ms REAL, throughput_mbps REAL, health REAL, stage_name TEXT, payload_json TEXT NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_metric_samples_task_time ON metric_samples(task_id, sampled_at)')
            connection.execute('CREATE TABLE IF NOT EXISTS alert_records (alert_id TEXT PRIMARY KEY, task_id TEXT, asset_id TEXT, severity TEXT NOT NULL, rule_id TEXT, status TEXT NOT NULL, opened_at TEXT NOT NULL, acknowledged_at TEXT, closed_at TEXT, message TEXT NOT NULL, evidence_json TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS notification_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, channel TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, sent_at TEXT, error_text TEXT)')

    def archive_result(self, task, analysis, report_html, archived_at):
        result_json = json.dumps(task, ensure_ascii=False)
        analysis_json = json.dumps(analysis, ensure_ascii=False)
        values = (task.get('id'), task.get('device'), task.get('serial'), task.get('path'), task.get('plan'), task.get('mode'), task.get('status'), analysis.get('conclusion'), analysis.get('score'), analysis.get('risk_level'), task.get('started_at'), task.get('ended_at'), task.get('progress'), result_json, analysis_json, report_html, archived_at)
        statement = 'INSERT INTO test_results (task_id, device, serial, device_path, plan, mode, task_status, conclusion, score, risk_level, started_at, ended_at, progress, result_json, analysis_json, report_html, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET device=excluded.device, serial=excluded.serial, device_path=excluded.device_path, plan=excluded.plan, mode=excluded.mode, task_status=excluded.task_status, conclusion=excluded.conclusion, score=excluded.score, risk_level=excluded.risk_level, started_at=excluded.started_at, ended_at=excluded.ended_at, progress=excluded.progress, result_json=excluded.result_json, analysis_json=excluded.analysis_json, report_html=excluded.report_html, archived_at=excluded.archived_at'
        with self.connect() as connection:
            connection.execute(statement, values)

    def record_audit(self, event_time, action, severity, detail, task_id=None, device_path=None):
        with self.connect() as connection:
            connection.execute('INSERT INTO audit_events (event_time, action, task_id, device_path, severity, detail_json) VALUES (?, ?, ?, ?, ?, ?)', (event_time, action, task_id, device_path, severity, json.dumps(detail, ensure_ascii=False)))

    def list_results(self, limit=200):
        with self.connect() as connection:
            rows = connection.execute('SELECT task_id, device, serial, device_path, plan, mode, task_status, conclusion, score, risk_level, started_at, ended_at, progress, archived_at FROM test_results ORDER BY archived_at DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]

    def result_records(self, limit=1000):
        with self.connect() as connection:
            rows = connection.execute('SELECT task_id, device, serial, device_path, plan, mode, task_status, conclusion, score, risk_level, started_at, ended_at, progress, result_json, analysis_json, archived_at FROM test_results ORDER BY archived_at DESC LIMIT ?', (limit,)).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item['result_json'] = json.loads(item['result_json'])
            item['analysis_json'] = json.loads(item['analysis_json'])
            records.append(item)
        return records

    def report_snapshot(self, task_id):
        with self.connect() as connection:
            row = connection.execute('SELECT report_html FROM test_results WHERE task_id = ?', (task_id,)).fetchone()
        return row['report_html'] if row else None

    def recent_audit_events(self, limit=100):
        with self.connect() as connection:
            rows = connection.execute('SELECT event_time, action, task_id, device_path, severity, detail_json FROM audit_events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item['detail'] = json.loads(item.pop('detail_json'))
            events.append(item)
        return events

    def create_batch(self, batch_id, name, created_at, metadata, items):
        with self.connect() as connection:
            connection.execute('INSERT INTO test_batches (batch_id, name, status, created_at, total_count, metadata_json) VALUES (?, ?, ?, ?, ?, ?)', (batch_id, name, '已创建', created_at, len(items), json.dumps(metadata, ensure_ascii=False)))
            for ordinal, task_id in enumerate(items, 1):
                connection.execute('INSERT INTO test_batch_items (batch_id, task_id, ordinal) VALUES (?, ?, ?)', (batch_id, task_id, ordinal))

    def refresh_batch(self, batch_id, task_states, finished_at):
        completed = sum(1 for status in task_states if status == '已完成')
        failed = sum(1 for status in task_states if status in ('失败', '已停止', '已中断'))
        active = any(status in ('运行中', '停止中', '排队中') for status in task_states)
        if active:
            status = '执行中'
        elif any(item == '失败' for item in task_states):
            status = '已完成（含失败）'
        elif any(item in ('已停止', '已中断') for item in task_states):
            status = '已完成（含中断）'
        else:
            status = '已完成'
        with self.connect() as connection:
            connection.execute('UPDATE test_batches SET status=?, completed_count=?, failed_count=?, finished_at=? WHERE batch_id=?', (status, completed, failed, finished_at if not active else None, batch_id))

    def list_batches(self, limit=100):
        with self.connect() as connection:
            rows = connection.execute('SELECT batch_id, name, status, created_at, finished_at, total_count, completed_count, failed_count, metadata_json FROM test_batches ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
            items = connection.execute('SELECT batch_id, task_id, ordinal FROM test_batch_items ORDER BY batch_id, ordinal').fetchall()
        grouped = {}
        for item in items:
            grouped.setdefault(item['batch_id'], []).append({'task_id': item['task_id'], 'ordinal': item['ordinal']})
        batches = []
        for row in rows:
            batch = dict(row)
            batch['metadata'] = json.loads(batch.pop('metadata_json'))
            batch['items'] = grouped.get(batch['batch_id'], [])
            batches.append(batch)
        return batches

    def batch_task_ids(self, batch_id):
        with self.connect() as connection:
            rows = connection.execute('SELECT task_id FROM test_batch_items WHERE batch_id=? ORDER BY ordinal', (batch_id,)).fetchall()
        return [row['task_id'] for row in rows]

    def upsert_asset(self, asset, captured_at):
        values = (asset['asset_id'], asset.get('serial'), asset.get('name'), asset.get('path'), asset.get('interface'), asset.get('capacity'), json.dumps(asset.get('labels') or [], ensure_ascii=False), asset.get('note'), captured_at, captured_at, asset.get('risk', {}).get('level'), asset.get('risk', {}).get('score'))
        with self.connect() as connection:
            connection.execute('INSERT INTO device_assets (asset_id, serial, model, last_path, interface, capacity, labels_json, note, first_seen_at, last_seen_at, risk_level, risk_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET serial=excluded.serial, model=excluded.model, last_path=excluded.last_path, interface=excluded.interface, capacity=excluded.capacity, last_seen_at=excluded.last_seen_at, risk_level=excluded.risk_level, risk_score=excluded.risk_score', values)

    def append_asset_snapshot(self, asset, captured_at):
        payload = json.dumps(asset, ensure_ascii=False)
        with self.connect() as connection:
            connection.execute('INSERT INTO device_snapshots (asset_id, captured_at, firmware, health, temperature, payload_json) VALUES (?, ?, ?, ?, ?, ?)', (asset['asset_id'], captured_at, asset.get('firmware'), asset.get('health'), asset.get('temperature'), payload))

    def list_assets(self, limit=500):
        with self.connect() as connection:
            rows = connection.execute('SELECT asset_id, serial, model, last_path, interface, capacity, labels_json, note, first_seen_at, last_seen_at, risk_level, risk_score FROM device_assets ORDER BY last_seen_at DESC LIMIT ?', (limit,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['labels'] = json.loads(item.pop('labels_json'))
            items.append(item)
        return items

    def asset_snapshots(self, asset_id, limit=200):
        with self.connect() as connection:
            rows = connection.execute('SELECT captured_at, firmware, health, temperature, payload_json FROM device_snapshots WHERE asset_id=? ORDER BY captured_at DESC LIMIT ?', (asset_id, limit)).fetchall()
        snapshots = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json'))
            snapshots.append(item)
        return snapshots

    def append_metric_sample(self, task_id, sample, stage_name=None):
        payload = json.dumps(sample, ensure_ascii=False)
        values = (task_id, sample.get('time'), sample.get('temperature'), sample.get('p99'), sample.get('throughput'), sample.get('health'), stage_name, payload)
        with self.connect() as connection:
            connection.execute('INSERT INTO metric_samples (task_id, sampled_at, temperature, p99_ms, throughput_mbps, health, stage_name, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', values)

    def task_metric_samples(self, task_id, limit=5000):
        with self.connect() as connection:
            rows = connection.execute('SELECT sampled_at, temperature, p99_ms, throughput_mbps, health, stage_name, payload_json FROM metric_samples WHERE task_id=? ORDER BY id ASC LIMIT ?', (task_id, limit)).fetchall()
        samples = []
        for row in rows:
            item = dict(row)
            payload = json.loads(item.pop('payload_json'))
            payload.setdefault('time', item['sampled_at'])
            payload.setdefault('temperature', item['temperature'])
            payload.setdefault('p99', item['p99_ms'])
            payload.setdefault('throughput', item['throughput_mbps'])
            payload.setdefault('health', item['health'])
            payload['stage_name'] = item['stage_name']
            samples.append(payload)
        return samples

    def upsert_alert_record(self, alert):
        opened_at = alert.get('time') or alert.get('occurred_at')
        message = alert.get('text') or alert.get('message') or ''
        values = (alert['id'], alert.get('task_id'), alert.get('asset_id'), alert['severity'], alert.get('rule_id'), '已确认' if alert.get('acknowledged') else '打开', opened_at, alert.get('acknowledged_at') if alert.get('acknowledged') else None, None, message, json.dumps(alert, ensure_ascii=False))
        with self.connect() as connection:
            connection.execute('INSERT INTO alert_records (alert_id, task_id, asset_id, severity, rule_id, status, opened_at, acknowledged_at, closed_at, message, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(alert_id) DO UPDATE SET status=excluded.status, acknowledged_at=excluded.acknowledged_at, message=excluded.message, evidence_json=excluded.evidence_json', values)

    def acknowledge_alert_record(self, alert_id, acknowledged_at):
        with self.connect() as connection:
            connection.execute('UPDATE alert_records SET status=?, acknowledged_at=? WHERE alert_id=?', ('已确认', acknowledged_at, alert_id))

    def delete_task_records(self, task_id):
        with self.connect() as connection:
            removed_notifications = connection.execute('DELETE FROM notification_outbox WHERE alert_id IN (SELECT alert_id FROM alert_records WHERE task_id=?)', (task_id,)).rowcount
            removed_alerts = connection.execute('DELETE FROM alert_records WHERE task_id=?', (task_id,)).rowcount
            removed_samples = connection.execute('DELETE FROM metric_samples WHERE task_id=?', (task_id,)).rowcount
            removed_batch_items = connection.execute('DELETE FROM test_batch_items WHERE task_id=?', (task_id,)).rowcount
            removed_results = connection.execute('DELETE FROM test_results WHERE task_id=?', (task_id,)).rowcount
        return {'results': removed_results, 'samples': removed_samples, 'alerts': removed_alerts, 'notifications': removed_notifications, 'batch_items': removed_batch_items}

    def enqueue_notifications(self, notifications):
        if not notifications:
            return 0
        inserted = 0
        with self.connect() as connection:
            for notification in notifications:
                exists = connection.execute('SELECT 1 FROM notification_outbox WHERE alert_id=? AND channel=? LIMIT 1', (notification.get('alert_id'), notification.get('channel'))).fetchone()
                if not exists:
                    connection.execute('INSERT INTO notification_outbox (alert_id, channel, status, created_at, payload_json) VALUES (?, ?, ?, ?, ?)', (notification.get('alert_id'), notification.get('channel'), notification.get('status', '待发送'), notification.get('created_at'), json.dumps(notification, ensure_ascii=False)))
                    inserted += 1
        return inserted

    def list_notifications(self, limit=500):
        with self.connect() as connection:
            rows = connection.execute('SELECT id, alert_id, channel, status, created_at, payload_json, sent_at, error_text FROM notification_outbox ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        notifications = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json'))
            notifications.append(item)
        return notifications

    def mark_notification(self, notification_id, status, processed_at, error_text=''):
        if status not in ('已发送', '发送失败'):
            raise ValueError('通知状态不支持')
        with self.connect() as connection:
            cursor = connection.execute('UPDATE notification_outbox SET status=?, sent_at=?, error_text=? WHERE id=?', (status, processed_at, '' if status == '已发送' else str(error_text)[:500], notification_id))
        return cursor.rowcount == 1

    def list_alert_records(self, status=None, limit=500, task_id=None):
        statement = 'SELECT alert_id, task_id, asset_id, severity, rule_id, status, opened_at, acknowledged_at, closed_at, message, evidence_json FROM alert_records'
        values = []
        clauses = []
        if status:
            clauses.append('status=?')
            values.append(status)
        if task_id:
            clauses.append('task_id=?')
            values.append(task_id)
        if clauses:
            statement += ' WHERE ' + ' AND '.join(clauses)
        statement += ' ORDER BY opened_at DESC LIMIT ?'
        values.append(limit)
        with self.connect() as connection:
            rows = connection.execute(statement, values).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item['evidence'] = json.loads(item.pop('evidence_json'))
            records.append(item)
        return records
