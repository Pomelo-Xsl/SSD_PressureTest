import json
import unittest

from fio_executor import build_fio_command, parse_fio_json, stage_progress


class FioExecutorTests(unittest.TestCase):

    def test_build_command_keeps_required_safety_and_load_options(self):
        task = {'id': 'task-1', 'path': '/dev/nvme1n1', 'io_pattern': 'randrw', 'block_size': '4K', 'queue_depth': 64, 'num_jobs': 2, 'ramp_time': 30, 'read_ratio': 20, 'rate_limit': 500, 'verify': 'crc32c', 'extra_options': {'thinktime': 10}}
        command = build_fio_command(task, 3600)
        self.assertIn('--direct=1', command)
        self.assertIn('--runtime=3600', command)
        self.assertIn('--rwmixread=20', command)
        self.assertIn('--verify=crc32c', command)

    def test_parse_fio_json_aggregates_all_jobs(self):
        output = json.dumps({'jobs': [{'read': {'bw_bytes': 1000000, 'iops': 100, 'clat_ns': {'percentile': {'99.000000': 2000000}}}, 'write': {'bw_bytes': 2000000, 'iops': 200, 'clat_ns': {'percentile': {'99.000000': 3000000}}}}]})
        metrics = parse_fio_json(output)
        self.assertEqual(metrics['throughput_mbps'], 3.0)
        self.assertEqual(metrics['p99_ms'], 3.0)
        self.assertEqual(metrics['write_iops'], 200)

    def test_stage_progress_tracks_current_stage(self):
        stages = [{'duration_seconds': 100}, {'duration_seconds': 100}]
        progress = stage_progress(stages, 150)
        self.assertEqual(progress['stage_index'], 1)
        self.assertEqual(progress['stage_progress'], 50.0)


if __name__ == '__main__':
    unittest.main()
