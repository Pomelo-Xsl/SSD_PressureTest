import unittest

from report_enrichment import (
    assess_telemetry_data_quality,
    build_report_evidence,
    stage_result_overview,
    summarize_alert_lifecycle,
    summarize_asset_history,
)


def task_template(**changes):
    task = {
        'id': 'task-evidence-01',
        'device': 'Enterprise NVMe SSD',
        'serial': 'SN-EVIDENCE-01',
        'path': '/dev/nvme1n1',
        'plan': '24 小时稳定性验证',
        'status': '已完成',
        'active_stage': '恢复观察',
        'stages': [
            {'ordinal': 1, 'name': '预热', 'purpose': '建立基线', 'duration_seconds': 600, 'duration_minutes': 10},
            {'ordinal': 2, 'name': '稳定负载', 'purpose': '验证持续 I/O', 'duration_seconds': 4200, 'duration_minutes': 70},
            {'ordinal': 3, 'name': '恢复观察', 'purpose': '观察负载撤除后的恢复', 'duration_seconds': 1200, 'duration_minutes': 20},
        ],
        'stage_results': [
            {'ordinal': 1, 'stage': '预热', 'completed': True, 'elapsed_seconds': 600, 'metrics': {'throughput_mbps': 2100, 'p99_ms': 3.5, 'job_count': 1}},
            {'ordinal': 2, 'stage': '稳定负载', 'completed': True, 'elapsed_seconds': 4200, 'metrics': {'throughput_mbps': 1980, 'p99_ms': 5.1, 'job_count': 1}},
            {'ordinal': 3, 'stage': '恢复观察', 'completed': True, 'elapsed_seconds': 1200, 'metrics': {'throughput_mbps': 2050, 'p99_ms': 4.0, 'job_count': 1}},
        ],
        'samples': [
            {'time': '2026-07-29 09:00:00', 'stage_name': '预热', 'temperature': 40, 'p99': 3.1, 'throughput': 2100, 'health': 99},
            {'time': '2026-07-29 09:10:00', 'stage_name': '稳定负载', 'temperature': 43, 'p99': 4.2, 'throughput': 2050, 'health': 99},
            {'time': '2026-07-29 09:20:00', 'stage_name': '恢复观察', 'temperature': 41, 'p99': 3.8, 'throughput': 2070, 'health': 99},
        ],
        'events': [
            {'time': '2026-07-29 09:05:00', 'severity': '警告', 'text': 'P99 延迟短时上升'},
            {'time': '2026-07-29 09:15:00', 'severity': '严重', 'text': '温度达到阈值'},
        ],
        'acknowledged_alerts': ['task-evidence-01:0'],
    }
    task.update(changes)
    return task


class ReportEnrichmentTests(unittest.TestCase):

    def test_stage_overview_keeps_plan_and_stage_metrics(self):
        overview = stage_result_overview(task_template())
        self.assertEqual(overview['planned_count'], 3)
        self.assertEqual(overview['completed_count'], 3)
        self.assertEqual(overview['stage_record_coverage_pct'], 100.0)
        self.assertEqual(overview['stages'][1]['metrics']['throughput_mbps'], 1980)
        self.assertEqual(overview['assessment'], '全部计划阶段已完成')

    def test_data_quality_reports_missing_and_duplicate_samples(self):
        task = task_template(samples=[
            {'time': '2026-07-29 09:00:00', 'stage_name': '预热', 'temperature': 40, 'p99': 3, 'throughput': 2000, 'health': 99},
            {'time': '2026-07-29 09:00:00', 'stage_name': '', 'temperature': '--', 'p99': 'invalid', 'throughput': 1980, 'health': None},
        ])
        quality = assess_telemetry_data_quality(task)
        self.assertEqual(quality['duplicate_timestamp_count'], 1)
        self.assertEqual(quality['fields']['temperature']['missing_count'], 1)
        self.assertEqual(quality['fields']['p99']['invalid_count'], 1)
        self.assertEqual(quality['stage_attribution_coverage_pct'], 50.0)
        self.assertEqual(quality['level'], '受限')

    def test_asset_history_summarizes_changes_and_health_decline(self):
        history = [
            {'captured_at': '2026-07-27 09:00:00', 'asset_id': 'asset-01', 'firmware': '1.0', 'interface': 'NVMe', 'capacity': '3.84 TB', 'health': 100, 'temperature': 39},
            {'captured_at': '2026-07-29 09:00:00', 'asset_id': 'asset-01', 'firmware': '1.1', 'interface': 'NVMe', 'capacity': '3.84 TB', 'health': 99.4, 'temperature': 48},
        ]
        summary = summarize_asset_history(task_template(), history)
        self.assertTrue(summary['available'])
        self.assertEqual(summary['snapshot_count'], 2)
        self.assertEqual(summary['health']['trend'], '下降')
        self.assertEqual(summary['health']['delta'], -0.6)
        self.assertEqual(summary['temperature']['maximum'], 48.0)
        self.assertEqual(summary['changes'][0]['field'], 'firmware')

    def test_alert_lifecycle_tracks_acknowledged_and_open_critical_alerts(self):
        lifecycle = summarize_alert_lifecycle(task_template())
        self.assertEqual(lifecycle['total'], 2)
        self.assertEqual(lifecycle['acknowledged'], 1)
        self.assertEqual(lifecycle['open_critical'], 1)
        self.assertIn('未确认严重告警', lifecycle['assessment'])

    def test_external_alert_records_can_close_lifecycle(self):
        task = task_template(events=[])
        alerts = [
            {'id': 'external-01', 'task_id': task['id'], 'severity': 'critical', 'opened_at': '2026-07-29 09:00:00', 'acknowledged_at': '2026-07-29 09:01:00', 'closed_at': '2026-07-29 09:02:00', 'message': '温度已恢复'},
            {'id': 'other-task', 'task_id': 'other', 'severity': 'warning', 'time': '2026-07-29 09:00:00'},
        ]
        lifecycle = summarize_alert_lifecycle(task, alerts)
        self.assertEqual(lifecycle['source'], '传入告警记录')
        self.assertEqual(lifecycle['closed'], 1)
        self.assertEqual(lifecycle['closure_rate_pct'], 100.0)
        self.assertEqual(lifecycle['assessment'], '告警均已关闭，形成完整处置闭环')

    def test_combined_evidence_marks_incomplete_data_and_open_critical_alert(self):
        task = task_template(samples=[], stage_results=[])
        analysis = {'algorithm': 'SSD Stability Score v2.0', 'score': 72, 'conclusion': '预警', 'risk_level': '中风险', 'limitations': ['样本不足']}
        evidence = build_report_evidence(task, analysis)
        self.assertFalse(evidence['report_ready'])
        self.assertEqual(evidence['analysis']['conclusion'], '预警')
        self.assertEqual(evidence['data_quality']['level'], '不足')
        self.assertTrue(evidence['recommendations'])


if __name__ == '__main__':
    unittest.main()
