import unittest

from device_assets import enrich_device, summarize_inventory


class DeviceAssetTests(unittest.TestCase):

    def test_testable_device_has_low_risk_asset(self):
        device = enrich_device({'name': 'Enterprise SSD', 'serial': 'SN-1', 'path': '/dev/nvme1n1', 'interface': 'NVMe', 'testable': True, 'test_reasons': []})
        self.assertEqual(device['risk']['level'], '低')
        self.assertEqual(len(device['asset_id']), 16)

    def test_partitioned_device_is_high_risk(self):
        device = enrich_device({'name': 'System SSD', 'serial': 'SN-2', 'path': '/dev/nvme0n1', 'interface': 'NVMe', 'testable': False, 'test_reasons': ['磁盘含有分区，禁止裸盘测试']})
        self.assertEqual(device['risk']['level'], '高')
        summary = summarize_inventory([device])
        self.assertEqual(summary['high_risk'], 1)


if __name__ == '__main__':
    unittest.main()
