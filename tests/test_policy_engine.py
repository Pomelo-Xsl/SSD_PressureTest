import unittest

from policy_engine import active_stage, build_test_stages, estimate_test_envelope


class PolicyEngineTests(unittest.TestCase):

    def setUp(self):
        self.config = {'duration': 8, 'block_size': '4K', 'read_ratio': 20, 'queue_depth': 128, 'num_jobs': 2, 'rate_limit': 1000}

    def test_stages_cover_full_duration(self):
        stages = build_test_stages('plan-spike', self.config)
        self.assertEqual(sum(stage['duration_seconds'] for stage in stages), 8 * 3600)
        self.assertEqual(active_stage(stages, 0)['name'], '预热')
        self.assertEqual(active_stage(stages, 999999)['name'], '恢复观察')

    def test_envelope_estimates_limited_write_volume(self):
        envelope = estimate_test_envelope(self.config)
        self.assertTrue(envelope['rate_limited'])
        self.assertGreater(envelope['estimated_write_gib_when_limited'], 0)
        self.assertGreater(envelope['minimum_sample_count'], 3)


if __name__ == '__main__':
    unittest.main()
