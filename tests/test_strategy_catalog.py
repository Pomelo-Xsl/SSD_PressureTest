import unittest

from test_workflow import enabled_strategies, normalize_strategy, strategy_snapshot


class StrategyCatalogTests(unittest.TestCase):

    def setUp(self):
        self.strategy = {'id': 'custom-1', 'name': '自定义验收', 'duration': 12, 'block_size': '4K', 'read_ratio': 30, 'queue_depth': 32, 'threshold_temp': 70}

    def test_normalize_adds_version_and_enabled_status(self):
        item = normalize_strategy(self.strategy, {'4K'})
        self.assertTrue(item['enabled'])
        self.assertEqual(item['version'], 1)

    def test_enabled_strategies_hides_disabled_items(self):
        plans = [normalize_strategy(self.strategy, {'4K'}), dict(self.strategy, id='disabled', enabled=False)]
        self.assertEqual(len(enabled_strategies(plans)), 1)

    def test_snapshot_does_not_expose_mutable_fields(self):
        snapshot = strategy_snapshot(normalize_strategy(self.strategy, {'4K'}))
        self.assertEqual(snapshot['name'], '自定义验收')
        self.assertNotIn('enabled', snapshot)


if __name__ == '__main__':
    unittest.main()
