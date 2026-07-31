import unittest
from datetime import datetime

from common import (
    bytes_digest,
    datetime_string,
    metric_series,
    number_in_text,
    number_value,
    scope_identity,
    text_digest,
    text_value,
    wall_datetime,
)


class CommonValueTests(unittest.TestCase):

    def test_number_helpers_keep_missing_and_boolean_values_out(self):
        self.assertIsNone(number_value('--'))
        self.assertIsNone(number_value(True))
        self.assertEqual(number_value('42.5'), 42.5)
        self.assertEqual(number_in_text('温度 58.2 C'), 58.2)

    def test_metric_series_skips_invalid_rows(self):
        samples = [{'temperature': '42'}, None, {'temperature': '--'}, {'temperature': 43.5}]
        self.assertEqual(metric_series(samples, 'temperature'), [42.0, 43.5])

    def test_text_and_time_helpers_apply_shared_defaults(self):
        self.assertEqual(text_value('  NVMe  '), 'NVMe')
        self.assertEqual(text_value('--', '未知'), '未知')
        self.assertEqual(datetime_string('2026-07-31T09:30:12'), '2026-07-31 09:30:12')
        self.assertEqual(wall_datetime(datetime(2026, 7, 31, 9, 30, 12, 999)).microsecond, 0)

    def test_scope_and_digest_are_stable(self):
        self.assertEqual(scope_identity({'serial': 'SN-001'}), 'serial:SN-001')
        self.assertEqual(text_digest('a', 'b', length=8), text_digest('a', 'b', length=8))
        self.assertEqual(len(bytes_digest(b'evidence')), 64)


if __name__ == '__main__':
    unittest.main()
