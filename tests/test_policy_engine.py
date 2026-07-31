import unittest

from test_workflow import active_stage, planned_stages, estimate_test_envelope, strategy_config, test_config


class PolicyEngineTests(unittest.TestCase):

    def setUp(self):
        self.config = {'duration': 8, 'block_size': '4K', 'read_ratio': 20, 'queue_depth': 128, 'num_jobs': 2, 'rate_limit': 1000}

    def test_stages_cover_full_duration(self):
        stages = planned_stages('plan-spike', self.config)
        self.assertEqual(sum(stage['duration_seconds'] for stage in stages), 8 * 3600)
        self.assertEqual(active_stage(stages, 0)['name'], '预热')
        self.assertEqual(active_stage(stages, 999999)['name'], '恢复观察')

    def test_envelope_estimates_limited_write_volume(self):
        envelope = estimate_test_envelope(self.config)
        self.assertTrue(envelope['rate_limited'])
        self.assertGreater(envelope['estimated_write_gib_when_limited'], 0)
        self.assertGreater(envelope['minimum_sample_count'], 3)

    def test_http_config_and_strategy_share_the_same_validation(self):
        plan = {'id': 'custom', 'name': '抽检', 'duration': 8, 'block_size': '4K', 'read_ratio': 20, 'queue_depth': 64, 'threshold_temp': 70}
        checked = test_config(plan, {'queue_depth': '128', 'extra_options': 'thinktime=10'})
        strategy = strategy_config(dict(plan, queue_depth='128'))
        self.assertEqual(checked['queue_depth'], strategy['queue_depth'])
        self.assertEqual(checked['extra_options'], {'thinktime': 10})

    def test_fio_runtime_cannot_be_overridden_through_extra_options(self):
        plan = {'duration': 8, 'block_size': '4K', 'read_ratio': 20, 'queue_depth': 64, 'threshold_temp': 70}
        with self.assertRaisesRegex(ValueError, '未开放'):
            test_config(plan, {'extra_options': 'runtime=1'})


if __name__ == '__main__':
    unittest.main()
