import unittest

from reliability_metrics import health_decline, latency_slo, temperature_dwell, throughput_jitter


class ReliabilityMetricTests(unittest.TestCase):

    def setUp(self):
        self.samples = [{'temperature': 62, 'p99': 4, 'throughput': 2000, 'health': 100}, {'temperature': 68, 'p99': 24, 'throughput': 1600, 'health': 99.8}, {'temperature': 71, 'p99': 8, 'throughput': 1800, 'health': 99.7}]

    def test_temperature_dwell_counts_near_and_over_limit_samples(self):
        result = temperature_dwell(self.samples, 70)
        self.assertEqual(result['near_limit_count'], 2)
        self.assertEqual(result['over_limit_count'], 1)

    def test_latency_slo_reports_violations(self):
        result = latency_slo(self.samples, 20)
        self.assertEqual(result['violations'], 1)
        self.assertLess(result['pass_rate_pct'], 100)

    def test_jitter_and_health_decline(self):
        self.assertGreater(throughput_jitter(self.samples)['jitter_pct'], 0)
        self.assertEqual(health_decline(self.samples)['decline_pct'], 0.3)


if __name__ == '__main__':
    unittest.main()
