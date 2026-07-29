import unittest

from time_series_store import InMemoryTimeSeriesStore, calculate_missing_intervals, data_quality_summary, downsample_time_series, flatten_metric_sample, merge_stage_labels, normalize_metric_sample


class TimeSeriesNormalizationTests(unittest.TestCase):

    def test_normalize_accepts_time_aliases_nested_metrics_and_labels(self):
        record = normalize_metric_sample({
            'time': '2026-07-29 08:00:00+08:00',
            'temperature': '42.5 C',
            'metrics': {'p99': '7.2', 'throughput': '2,400 MB/s'},
            'stage_name': '预热',
            'labels': {'host': 'rack-a'},
        })
        self.assertEqual(record['timestamp'], '2026-07-29T00:00:00Z')
        self.assertEqual(record['metrics'], {'p99': 7.2, 'throughput': 2400.0, 'temperature': 42.5})
        self.assertEqual(record['stage'], '预热')
        self.assertEqual(record['labels']['host'], 'rack-a')

    def test_normalize_requires_timestamp_and_flatten_keeps_metrics(self):
        with self.assertRaises(ValueError):
            normalize_metric_sample({'temperature': 40})
        flattened = flatten_metric_sample({'timestamp': 0, 'temperature': 40, 'stage': '负载'})
        self.assertEqual(flattened['timestamp'], '1970-01-01T00:00:00Z')
        self.assertEqual(flattened['temperature'], 40.0)
        self.assertEqual(flattened['stage'], '负载')


class TimeSeriesStoreTests(unittest.TestCase):

    def test_store_orders_merges_and_projects_samples(self):
        store = InMemoryTimeSeriesStore()
        store.write_samples('task-a', [
            {'time': '2026-07-29T00:02:00Z', 'temperature': 44, 'stage': '负载'},
            {'time': '2026-07-29T00:01:00Z', 'temperature': 42, 'stage': '预热'},
        ])
        result = store.write_samples('task-a', [{'time': '2026-07-29T00:01:00Z', 'p99': 7.5, 'labels': {'host': 'a'}}])
        self.assertEqual(result['merged'], 1)
        records = store.query_samples('task-a', metric_names='p99')
        self.assertEqual([item['timestamp'] for item in records], ['2026-07-29T00:01:00Z', '2026-07-29T00:02:00Z'])
        self.assertEqual(records[0]['metrics'], {'p99': 7.5})
        self.assertEqual(records[0]['labels']['host'], 'a')
        self.assertEqual(store.series_summary('task-a')['stages'], ['负载', '预热'])

    def test_store_filters_bounds_stages_and_retention(self):
        store = InMemoryTimeSeriesStore(max_samples_per_series=3)
        samples = []
        for minute in range(4):
            samples.append({'time': '2026-07-29T00:0{0}:00Z'.format(minute), 'temperature': 40 + minute, 'stage': '预热' if minute < 2 else '负载'})
        result = store.write_samples('task-b', samples)
        self.assertEqual(result['evicted'], 1)
        rows = store.query_samples('task-b', start='2026-07-29T00:02:00Z', stages='负载', descending=True)
        self.assertEqual([item['metrics']['temperature'] for item in rows], [43.0, 42.0])
        self.assertEqual(store.clear_series('task-b'), 3)
        self.assertEqual(store.count_samples('task-b'), 0)


class TimeSeriesProcessingTests(unittest.TestCase):

    def _samples(self, count=20):
        records = []
        for index in range(count):
            value = 10 if index != 11 else 100
            records.append({'time': '2026-07-29T00:{0:02d}:00Z'.format(index), 'throughput': value, 'p99': index})
        return records

    def test_minmax_downsampling_preserves_bounds_and_peak(self):
        output = downsample_time_series(self._samples(), 8, metric='throughput')
        self.assertEqual(len(output), 8)
        self.assertEqual(output[0]['timestamp'], '2026-07-29T00:00:00Z')
        self.assertEqual(output[-1]['timestamp'], '2026-07-29T00:19:00Z')
        self.assertIn(100.0, [item['metrics']['throughput'] for item in output])

    def test_uniform_downsampling_and_invalid_method(self):
        output = downsample_time_series(self._samples(10), 3, method='uniform')
        self.assertEqual([item['metrics']['p99'] for item in output], [0.0, 4.0, 9.0])
        with self.assertRaises(ValueError):
            downsample_time_series(self._samples(), 3, method='other')

    def test_missing_intervals_and_data_quality(self):
        samples = [
            {'time': '2026-07-29T00:00:00Z', 'temperature': 40},
            {'time': '2026-07-29T00:01:00Z', 'temperature': 41},
            {'time': '2026-07-29T00:04:00Z', 'temperature': 42},
            {'time': 'bad-time', 'temperature': 43},
        ]
        gaps = calculate_missing_intervals(samples, 60, ignore_invalid=True)
        self.assertEqual(gaps['missing_sample_count'], 2)
        self.assertEqual(gaps['gap_count'], 1)
        quality = data_quality_summary(samples, 60)
        self.assertEqual(quality['invalid_sample_count'], 1)
        self.assertEqual(quality['completeness_pct'], 60.0)
        self.assertEqual(quality['quality'], '需关注')

    def test_merge_stage_labels_supports_sequential_policy_stages(self):
        samples = [
            {'time': '2026-07-29T00:00:00Z', 'temperature': 40},
            {'time': '2026-07-29T00:01:00Z', 'temperature': 41},
            {'time': '2026-07-29T00:02:00Z', 'temperature': 42},
            {'time': '2026-07-29T00:03:00Z', 'temperature': 43},
        ]
        output = merge_stage_labels(samples, [
            {'name': '预热', 'duration_seconds': 60},
            {'name': '负载', 'duration_seconds': 120},
            {'name': '恢复', 'duration_seconds': 60},
        ])
        self.assertEqual([item['stage'] for item in output], ['预热', '负载', '负载', '恢复'])
        self.assertEqual(output[1]['labels']['stage_ordinal'], '2')

    def test_existing_stage_is_preserved_unless_overwrite_enabled(self):
        source = [{'time': '2026-07-29T00:00:00Z', 'temperature': 40, 'stage': '人工标记'}]
        stages = [{'name': '预热', 'duration_seconds': 60}]
        self.assertEqual(merge_stage_labels(source, stages)[0]['stage'], '人工标记')
        self.assertEqual(merge_stage_labels(source, stages, overwrite=True)[0]['stage'], '预热')


if __name__ == '__main__':
    unittest.main()
