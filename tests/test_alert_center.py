import unittest

from alerts import acknowledge_alert, alert_summary, build_alerts


class AlertCenterTests(unittest.TestCase):

    def setUp(self):
        self.task = {'id': 'task-a', 'device': 'Enterprise SSD', 'path': '/dev/nvme1n1', 'plan': '耐久老化', 'events': [{'time': '2026-07-29 10:00:00', 'severity': '信息', 'text': '启动'}, {'time': '2026-07-29 10:02:00', 'severity': '警告', 'text': '延迟偏高'}, {'time': '2026-07-29 10:03:00', 'severity': '严重', 'text': '温度超限'}]}

    def test_alerts_include_only_warning_and_critical_events(self):
        alerts = build_alerts([self.task])
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]['severity'], '严重')
        summary = alert_summary(alerts)
        self.assertEqual(summary['critical'], 1)
        self.assertEqual(summary['unacknowledged'], 2)

    def test_acknowledge_marks_task_alert(self):
        result = acknowledge_alert(self.task, 'task-a:1')
        self.assertTrue(result['acknowledged'])
        self.assertTrue(build_alerts([self.task])[1]['acknowledged'])

    def test_event_identifier_survives_event_reordering(self):
        self.task['events'][1]['id'] = 'task-a:event-latency'
        alerts = build_alerts([self.task])
        result = acknowledge_alert(self.task, 'task-a:event-latency')
        self.assertEqual(result['alert_id'], 'task-a:event-latency')
        latency_alert = next(alert for alert in alerts if alert['id'] == 'task-a:event-latency')
        self.assertFalse(latency_alert['acknowledged'])
        current = next(alert for alert in build_alerts([self.task]) if alert['id'] == 'task-a:event-latency')
        self.assertTrue(current['acknowledged'])


if __name__ == '__main__':
    unittest.main()
