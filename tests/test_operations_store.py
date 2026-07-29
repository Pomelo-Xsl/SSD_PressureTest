import tempfile
import unittest
from pathlib import Path

from operations_store import OperationsStore
from telemetry_rules import evaluate_sample, telemetry_summary


class OperationsStoreTests(unittest.TestCase):

    def setUp(self):
        self.database_file = Path(tempfile.gettempdir()) / 'ssd_pressuretest_operations_test.db'
        self.database_file.unlink(missing_ok=True)
        self.store = OperationsStore(self.database_file)
        self.store.initialize()

    def tearDown(self):
        self.database_file.unlink(missing_ok=True)

    def test_archive_result_replaces_same_task_snapshot(self):
        task = {'id': 'task-a', 'device': 'Enterprise NVMe', 'serial': 'SN-1', 'path': '/dev/nvme1n1', 'plan': '稳定性验证', 'mode': '安全演示', 'status': '已完成', 'started_at': '2026-07-29 10:00:00', 'ended_at': '2026-07-29 10:10:00', 'progress': 100}
        analysis = {'conclusion': '通过', 'score': 91, 'risk_level': '低风险'}
        self.store.archive_result(task, analysis, '<html>first</html>', '2026-07-29 10:10:01')
        analysis['score'] = 88
        self.store.archive_result(task, analysis, '<html>second</html>', '2026-07-29 10:10:02')
        rows = self.store.list_results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['score'], 88)
        self.assertEqual(self.store.report_snapshot('task-a'), '<html>second</html>')

    def test_audit_event_round_trip(self):
        self.store.record_audit('2026-07-29 10:00:00', 'task_event', '警告', {'text': '温度接近阈值'}, 'task-a', '/dev/nvme1n1')
        event = self.store.recent_audit_events()[0]
        self.assertEqual(event['action'], 'task_event')
        self.assertEqual(event['detail']['text'], '温度接近阈值')

    def test_batch_tracks_completion_and_failures(self):
        self.store.create_batch('batch-a', '验收批次', '2026-07-29 10:00:00', {'priority': '普通'}, ['task-1', 'task-2'])
        self.store.refresh_batch('batch-a', ['已完成', '失败'], '2026-07-29 11:00:00')
        batch = self.store.list_batches()[0]
        self.assertEqual(batch['completed_count'], 1)
        self.assertEqual(batch['failed_count'], 1)
        self.assertEqual(batch['status'], '已完成（含失败）')

    def test_metric_samples_and_alert_lifecycle_round_trip(self):
        self.store.append_metric_sample('task-a', {'time': '2026-07-29 10:00:00', 'temperature': 67.2, 'p99': 21.5, 'throughput': 1900, 'health': 99}, '稳定负载')
        samples = self.store.task_metric_samples('task-a')
        self.assertEqual(samples[0]['stage_name'], '稳定负载')
        self.assertEqual(samples[0]['temperature'], 67.2)
        alert = {'id': 'alert-a', 'task_id': 'task-a', 'asset_id': 'asset-a', 'severity': '警告', 'rule_id': 'latency', 'time': '2026-07-29 10:01:00', 'text': 'P99 延迟偏高'}
        self.store.upsert_alert_record(alert)
        self.store.acknowledge_alert_record('alert-a', '2026-07-29 10:02:00')
        records = self.store.list_alert_records(task_id='task-a')
        self.assertEqual(records[0]['status'], '已确认')
        self.assertEqual(records[0]['acknowledged_at'], '2026-07-29 10:02:00')

    def test_notification_outbox_deduplicates_by_alert_and_channel(self):
        notices = [{'alert_id': 'alert-a', 'channel': 'web', 'status': '待发送', 'created_at': '2026-07-29 10:00:00', 'payload': {'message': '温度告警'}}]
        self.assertEqual(self.store.enqueue_notifications(notices), 1)
        self.assertEqual(self.store.enqueue_notifications(notices), 0)
        rows = self.store.list_notifications()
        self.assertEqual(len(rows), 1)
        self.assertTrue(self.store.mark_notification(rows[0]['id'], '已发送', '2026-07-29 10:01:00'))
        self.assertEqual(self.store.list_notifications()[0]['status'], '已发送')


class TelemetryRuleTests(unittest.TestCase):

    def test_temperature_and_latency_thresholds_create_events(self):
        task = {'threshold_temp': 70, 'samples': []}
        events = evaluate_sample(task, {'temperature': 71, 'p99': 52, 'throughput': 2000})
        self.assertEqual([event[0] for event in events], ['严重', '严重'])

    def test_summary_ignores_missing_values(self):
        summary = telemetry_summary([{'temperature': 42, 'p99': '--', 'throughput': 2000, 'health': 99}, {'temperature': 44, 'p99': 4.2, 'throughput': 2200, 'health': '--'}])
        self.assertEqual(summary['temperature']['avg'], 43.0)
        self.assertEqual(summary['p99']['count'], 1)


if __name__ == '__main__':
    unittest.main()
