import copy
import unittest
from unittest.mock import patch

import app


class TaskRequest:

    def __init__(self, payload):
        self.path = '/api/tasks'
        self.payload = payload
        self.response = None
        self.status = None

    def body(self):
        return self.payload

    def send_json(self, body, status=200):
        self.response = body
        self.status = status
        return body


class TaskCreationTests(unittest.TestCase):

    def setUp(self):
        self.original_state = copy.deepcopy(app.STATE)
        app.STATE['plans'] = copy.deepcopy(app.DEFAULT_PLANS)
        app.STATE['tasks'] = []
        app.STATE['alert_policies'] = []
        self.device = {
            'id': 'demo-nvme0',
            'path': '/dev/nvme0n1',
            'name': 'Demo Enterprise SSD',
            'serial': 'DEMO-001',
            'asset_id': 'asset-demo-001',
            'interface': 'NVMe',
            'testable': True,
            'test_reasons': [],
        }

    def tearDown(self):
        app.STATE.clear()
        app.STATE.update(self.original_state)

    def create_demo_task(self):
        return TaskRequest({'device_id': 'demo-nvme0', 'plan_id': 'plan-burnin', 'mode': 'demo'})

    def test_idle_demo_task_starts_immediately(self):
        request = self.create_demo_task()
        started = []

        def mark_started(task):
            started.append(task['id'])
            task['status'] = '运行中'
            task['queued'] = False
            task['started_at'] = '2026-07-29 16:00:00'

        with patch.object(app, 'running_on_linux', return_value=False), \
             patch.object(app, 'build_demo_ssd_inventory', return_value=[self.device]), \
             patch.object(app, 'enrich_device', side_effect=lambda device: device), \
             patch.object(app, 'start_task_execution', side_effect=mark_started), \
             patch.object(app, 'persist_test_workspace_state'):
            app.Handler.do_POST(request)

        self.assertEqual(request.status, 201)
        self.assertEqual(request.response['status'], '运行中')
        self.assertFalse(request.response['queued'])
        self.assertEqual(request.response['started_at'], '2026-07-29 16:00:00')
        self.assertEqual(started, [request.response['id']])

    def test_active_task_keeps_new_demo_task_queued(self):
        app.STATE['tasks'] = [{'id': 'running-task', 'status': '运行中'}]
        request = self.create_demo_task()

        with patch.object(app, 'running_on_linux', return_value=False), \
             patch.object(app, 'build_demo_ssd_inventory', return_value=[self.device]), \
             patch.object(app, 'enrich_device', side_effect=lambda device: device), \
             patch.object(app, 'start_task_execution') as start_execution, \
             patch.object(app, 'persist_test_workspace_state'):
            app.Handler.do_POST(request)

        self.assertEqual(request.status, 201)
        self.assertEqual(request.response['status'], '排队中')
        self.assertTrue(request.response['queued'])
        self.assertIsNone(request.response['started_at'])
        start_execution.assert_not_called()


if __name__ == '__main__':
    unittest.main()
