import unittest

from device_history import health_timeline, compare_device_snapshots, device_snapshot_record, parse_capacity_bytes, device_history_overview


class DeviceHistoryTests(unittest.TestCase):

    def test_capacity_parser_accepts_decimal_and_binary_units(self):
        self.assertEqual(parse_capacity_bytes('3.84 TB'), 3840000000000)
        self.assertEqual(parse_capacity_bytes('1 GiB'), 1073741824)
        self.assertIsNone(parse_capacity_bytes('unknown'))

    def test_normalize_snapshot_reads_database_payload_and_aliases(self):
        item = device_snapshot_record({'captured_at': '2026-07-29 09:00:00', 'payload': {'asset_id': 'asset-01', 'model': 'Enterprise SSD', 'serial': 'SN-001', 'path': '/dev/nvme1n1', 'transport': 'nvme', 'fwrev': '1.2.3', 'capacity': '3.84 TB', 'health': '98', 'temperature': '42 C'}})
        self.assertEqual(item['asset_id'], 'asset-01')
        self.assertEqual(item['interface'], 'NVMe')
        self.assertEqual(item['firmware'], '1.2.3')
        self.assertEqual(item['capacity_bytes'], 3840000000000)
        self.assertEqual(item['health'], 98.0)
        self.assertEqual(item['temperature'], 42.0)

    def test_compare_snapshots_reports_firmware_interface_and_capacity_change(self):
        previous = {'asset_id': 'asset-01', 'captured_at': '2026-07-28 09:00:00', 'serial': 'SN-001', 'firmware': '1.0', 'interface': 'NVMe', 'capacity': '3.84 TB', 'health': 99, 'temperature': 40}
        current = {'asset_id': 'asset-01', 'captured_at': '2026-07-29 09:00:00', 'serial': 'SN-001', 'firmware': '1.1', 'interface': 'SAS', 'capacity': '7.68 TB', 'health': 98, 'temperature': 48}
        result = compare_device_snapshots(previous, current)
        self.assertTrue(result['changed'])
        self.assertTrue(result['firmware']['changed'])
        self.assertTrue(result['interface']['changed'])
        self.assertTrue(result['capacity']['changed'])
        self.assertEqual(result['health']['difference'], -1.0)
        self.assertEqual({item['field'] for item in result['changes']}, {'firmware', 'interface', 'capacity'})

    def test_compare_does_not_mix_unrelated_assets(self):
        result = compare_device_snapshots({'asset_id': 'old', 'firmware': '1.0', 'capacity': '1 TB'}, {'asset_id': 'new', 'firmware': '2.0', 'capacity': '2 TB'})
        self.assertFalse(result['same_asset'])
        self.assertFalse(result['changed'])
        self.assertFalse(result['firmware']['changed'])

    def test_health_trend_sorts_history_and_detects_decline(self):
        history = [
            {'asset_id': 'asset-01', 'captured_at': '2026-07-29 10:00:00', 'health': 98.8, 'temperature': 52},
            {'asset_id': 'asset-01', 'captured_at': '2026-07-27 10:00:00', 'health': 100, 'temperature': 42},
            {'asset_id': 'asset-01', 'captured_at': '2026-07-28 10:00:00', 'health': 99.4, 'temperature': 48},
        ]
        trend = health_timeline(history)
        self.assertEqual(trend['points'][0]['captured_at'], '2026-07-27 10:00:00')
        self.assertEqual(trend['health']['trend'], '下降')
        self.assertEqual(trend['health']['difference'], -1.2)
        self.assertEqual(trend['temperature']['maximum'], 52.0)

    def test_summary_tracks_hardware_versions_and_latest_change(self):
        history = [
            {'asset_id': 'asset-01', 'captured_at': '2026-07-27 10:00:00', 'firmware': '1.0', 'interface': 'NVMe', 'capacity': '3.84 TB', 'health': 100},
            {'asset_id': 'asset-01', 'captured_at': '2026-07-28 10:00:00', 'firmware': '1.1', 'interface': 'NVMe', 'capacity': '3.84 TB', 'health': 99.9},
        ]
        summary = device_history_overview(history)
        self.assertTrue(summary['firmware_changed'])
        self.assertFalse(summary['interface_changed'])
        self.assertIsNotNone(summary['latest_change'])
        self.assertTrue(summary['latest_change']['firmware']['changed'])


if __name__ == '__main__':
    unittest.main()
