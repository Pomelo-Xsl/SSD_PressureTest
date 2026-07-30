import unittest

from alerts import active_maintenance_windows, build_alert_fingerprint, create_policy_version, evaluate_alert_policy, mark_notification_result, normalize_alert_policy, policy_summary, prepare_notification_outbox


def sample_policy():
    return {
        'id': 'enterprise-nvme',
        'name': '企业 NVMe 压测告警策略',
        'version': 2,
        'channels': ['web', 'audit', 'web'],
        'dedup_seconds': 300,
        'suppression_seconds': 900,
        'rules': [
            {
                'id': 'temperature-critical',
                'name': '控制器温度超限',
                'metric': 'temperature',
                'operator': '>=',
                'threshold': 70,
                'severity': '严重',
                'channels': ['webhook'],
                'message': '{device} 温度 {actual}°C，已达到 {threshold}°C',
            },
            {
                'id': 'latency-streak',
                'name': 'P99 延迟连续偏高',
                'metric': 'latency.p99',
                'operator': '>=',
                'threshold': 20,
                'severity': '警告',
                'trigger_after': 2,
                'trigger_window_seconds': 600,
            },
        ],
        'maintenance_windows': [
            {
                'id': 'night-window',
                'name': '夜间维护',
                'weekdays': [0],
                'start': '23:00',
                'end': '02:00',
                'severities': ['警告'],
            },
        ],
    }


class AlertRuleTests(unittest.TestCase):

    def setUp(self):
        self.policy = sample_policy()
        self.scope = {'asset_id': 'asset-ssd-a', 'device': 'Enterprise SSD', 'path': '/dev/nvme1n1', 'task_id': 'task-a'}

    def test_normalize_policy_deduplicates_channels_and_produces_summary(self):
        policy = normalize_alert_policy(self.policy)
        self.assertEqual(policy['channels'], ['web', 'audit'])
        self.assertEqual(policy['rules'][0]['channels'], ['webhook'])
        summary = policy_summary(policy)
        self.assertEqual(summary['rule_count'], 2)
        self.assertEqual(summary['severity_counts']['严重'], 1)

    def test_policy_version_keeps_history_and_increments_version(self):
        changed = create_policy_version(self.policy, {'name': '企业 NVMe 压测告警策略 V3'}, '2026-07-29 10:00:00', '提高温度规则可见性')
        self.assertEqual(changed['version'], 3)
        self.assertEqual(changed['name'], '企业 NVMe 压测告警策略 V3')
        self.assertEqual(changed['versions'][0]['version'], 2)
        self.assertEqual(changed['versions'][0]['reason'], '提高温度规则可见性')

    def test_metric_rule_creates_alert_and_notification_outbox(self):
        result = evaluate_alert_policy(self.policy, {'temperature': 72, 'latency': {'p99': 8}}, self.scope, now='2026-07-29 10:00:00')
        self.assertEqual(len(result['matches']), 1)
        alert = result['matches'][0]
        self.assertEqual(alert['rule_id'], 'temperature-critical')
        self.assertEqual(alert['severity'], '严重')
        self.assertIn('Enterprise SSD 温度 72.0°C', alert['message'])
        self.assertEqual(len(result['notifications']), 1)
        self.assertEqual(result['notifications'][0]['channel'], 'webhook')
        self.assertEqual(result['notifications'][0]['status'], '待发送')

    def test_duplicate_open_alert_is_suppressed(self):
        first = evaluate_alert_policy(self.policy, {'temperature': 72}, self.scope, now='2026-07-29 10:00:00')
        history = list(first['matches'])
        history[0]['last_seen_at'] = '2026-07-29 10:01:00'
        repeated = evaluate_alert_policy(self.policy, {'temperature': 73}, self.scope, history, '2026-07-29 10:02:00')
        self.assertEqual(len(repeated['matches']), 0)
        self.assertEqual(len(repeated['suppressed']), 1)
        self.assertIn('去重', repeated['suppressed'][0]['suppression_reason'])

    def test_closed_alert_uses_suppression_cooldown(self):
        first = evaluate_alert_policy(self.policy, {'temperature': 72}, self.scope, now='2026-07-29 10:00:00')
        history = list(first['matches'])
        history[0]['status'] = '已关闭'
        history[0]['closed_at'] = '2026-07-29 10:03:00'
        repeated = evaluate_alert_policy(self.policy, {'temperature': 73}, self.scope, history, '2026-07-29 10:05:00')
        self.assertEqual(len(repeated['matches']), 0)
        self.assertIn('抑制', repeated['suppressed'][0]['suppression_reason'])

    def test_overnight_maintenance_window_suppresses_warning_only(self):
        windows = active_maintenance_windows(self.policy, '2026-07-28 00:30:00', '警告')
        self.assertEqual(len(windows), 1)
        history = [{
            'id': 'latency-observation',
            'rule_id': 'latency-streak',
            'fingerprint': build_alert_fingerprint('enterprise-nvme', 'latency-streak', self.scope),
            'status': '已关闭',
            'occurred_at': '2026-07-28 00:25:00',
            'closed_at': '2026-07-27 00:00:00',
        }]
        second = evaluate_alert_policy(self.policy, {'latency': {'p99': 25}}, self.scope, history, '2026-07-28 00:30:00')
        self.assertEqual(len(second['suppressed']), 1)
        self.assertIn('维护窗口', second['suppressed'][0]['suppression_reason'])

    def test_streak_rule_waits_for_required_occurrences(self):
        first = evaluate_alert_policy(self.policy, {'latency': {'p99': 25}}, self.scope, now='2026-07-29 10:00:00')
        self.assertEqual(len(first['matches']), 0)
        skipped = [item for item in first['skipped'] if item.get('rule_id') == 'latency-streak']
        self.assertEqual(skipped[0]['reason'], '连续触发次数不足')
        self.assertEqual(skipped[0]['occurrences'], 1)
        policy = normalize_alert_policy(self.policy)
        synthetic_history = [{
            'id': 'old',
            'rule_id': 'latency-streak',
            'fingerprint': build_alert_fingerprint(policy['id'], 'latency-streak', self.scope),
            'status': '已关闭',
            'closed_at': '2026-07-29 09:00:00',
            'occurred_at': '2026-07-29 10:00:00',
        }]
        second = evaluate_alert_policy(policy, {'temperature': 0, 'latency': {'p99': 25}}, self.scope, synthetic_history, '2026-07-29 10:00:03')
        self.assertEqual(len(second['matches']), 1)
        self.assertEqual(second['matches'][0]['rule_id'], 'latency-streak')

    def test_prepare_and_mark_notification_result(self):
        alerts = [{
            'id': 'alert-001',
            'fingerprint': 'fp-001',
            'status': '打开',
            'channels': ['web', 'audit', 'web'],
            'severity': '警告',
            'rule_name': '延迟异常',
            'message': 'P99 延迟偏高',
            'scope': {'task_id': 'task-1'},
            'occurred_at': '2026-07-29 11:00:00',
        }]
        records = prepare_notification_outbox(alerts, '2026-07-29 11:01:00')
        self.assertEqual(len(records), 2)
        delivered = mark_notification_result(records[0], True, '2026-07-29 11:02:00')
        failed = mark_notification_result(records[1], False, '2026-07-29 11:02:00', '连接被拒绝')
        self.assertEqual(delivered['status'], '已发送')
        self.assertEqual(failed['status'], '发送失败')
        self.assertEqual(failed['error_text'], '连接被拒绝')


if __name__ == '__main__':
    unittest.main()
