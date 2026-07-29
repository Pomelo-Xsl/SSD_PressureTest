import unittest

from task_scheduler import next_runnable_task, queue_position, summarize_queue


class TaskSchedulerTests(unittest.TestCase):

    def test_priority_precedes_fifo_for_queued_tasks(self):
        tasks = [{'id': 'low', 'status': '排队中', 'priority': '低', 'queue_sequence': 1}, {'id': 'high', 'status': '排队中', 'priority': '高', 'queue_sequence': 3}, {'id': 'normal', 'status': '排队中', 'priority': '普通', 'queue_sequence': 2}]
        self.assertEqual(next_runnable_task(tasks)['id'], 'high')
        self.assertEqual(queue_position(tasks, 'normal'), 2)

    def test_active_task_blocks_next_start(self):
        tasks = [{'id': 'running', 'status': '运行中', 'priority': '普通'}, {'id': 'queued', 'status': '排队中', 'priority': '紧急'}]
        self.assertIsNone(next_runnable_task(tasks))
        summary = summarize_queue(tasks)
        self.assertEqual(summary['active_count'], 1)
        self.assertEqual(summary['queued_count'], 1)


if __name__ == '__main__':
    unittest.main()
