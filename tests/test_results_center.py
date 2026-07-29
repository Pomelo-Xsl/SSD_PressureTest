import unittest

from results_center import build_filter_facets, build_history_trend, compare_results, normalize_result, query_results


def result(task_id, device, score, risk, started_at, status='已完成', conclusion='通过'):
    return {
        'task_id': task_id,
        'device': device,
        'serial': 'SN-{0}'.format(task_id),
        'device_path': '/dev/nvme{0}n1'.format(task_id[-1]),
        'plan': '稳定性验证',
        'mode': '真实 fio 裸盘',
        'task_status': status,
        'conclusion': conclusion,
        'score': score,
        'risk_level': risk,
        'started_at': started_at,
        'ended_at': started_at,
        'progress': 100,
        'analysis': {
            'metrics': {
                'temperature': {'max': 60 + int(task_id[-1])},
                'latency': {'p95': 2 + int(task_id[-1])},
                'throughput': {'avg': 2000 - int(task_id[-1]) * 100},
                'health': {'min': 99.9 - int(task_id[-1]) * 0.1},
            }
        },
    }


class ResultCenterTests(unittest.TestCase):

    def setUp(self):
        self.results = [
            result('task-1', 'Enterprise A', 92, '低风险', '2026-07-29 08:00:00'),
            result('task-2', 'Enterprise A', 78, '中风险', '2026-07-29 09:00:00', conclusion='预警'),
            result('task-3', 'Enterprise B', 61, '高风险', '2026-07-29 10:00:00', conclusion='不通过'),
        ]

    def test_normalize_uses_analysis_metrics_and_nested_task_data(self):
        nested = {
            'result_json': {
                'id': 'nested-1',
                'device': 'Nested SSD',
                'path': '/dev/nvme9n1',
                'status': '已完成',
                'samples': [
                    {'temperature': 52, 'p99': 4, 'throughput': 1200, 'health': 99.9},
                    {'temperature': 57, 'p99': 6, 'throughput': 1400, 'health': 99.8},
                ],
            },
            'analysis_json': {'score': 88, 'risk_level': '低风险', 'conclusion': '通过'},
        }
        item = normalize_result(nested)
        self.assertEqual(item['task_id'], 'nested-1')
        self.assertEqual(item['temperature_max'], 57.0)
        self.assertEqual(item['throughput_avg'], 1300.0)
        self.assertEqual(item['score'], 88.0)

    def test_query_filters_searches_sorts_and_paginates(self):
        page = query_results(self.results, {'device': 'Enterprise A', 'score_min': 80}, page=1, page_size=1, sort_by='score')
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['task_id'], 'task-1')
        second_page = query_results(self.results, {'search': 'Enterprise'}, page=2, page_size=2, sort_by='started_at', descending=False)
        self.assertEqual(second_page['total'], 3)
        self.assertEqual(second_page['total_pages'], 2)
        self.assertEqual(second_page['items'][0]['task_id'], 'task-3')

    def test_query_clamps_page_size_and_handles_empty_result_set(self):
        page = query_results(self.results, {'risk_level': '不存在'}, page=9, page_size=999)
        self.assertEqual(page['total'], 0)
        self.assertEqual(page['page'], 1)
        self.assertEqual(page['page_size'], 100)
        self.assertEqual(page['items'], [])

    def test_filter_facets_expose_available_options_with_counts(self):
        facets = build_filter_facets(self.results)
        devices = {item['value']: item['count'] for item in facets['device']}
        self.assertEqual(devices['Enterprise A'], 2)
        self.assertEqual(devices['Enterprise B'], 1)
        self.assertEqual(len(facets['risk_level']), 3)

    def test_comparison_reports_best_metrics_risk_and_recommendation(self):
        comparison = compare_results(self.results[:2])
        score_metric = comparison['metrics'][0]
        latency_metric = comparison['metrics'][2]
        self.assertEqual(score_metric['best_task_ids'], ['task-1'])
        self.assertEqual(latency_metric['best_task_ids'], ['task-1'])
        self.assertEqual(comparison['summary']['worst_risk'], '中风险')
        self.assertEqual(comparison['summary']['score_spread'], 14.0)

    def test_comparison_requires_two_to_four_results(self):
        with self.assertRaises(ValueError):
            compare_results(self.results[:1])
        with self.assertRaises(ValueError):
            compare_results(self.results + [result('task-4', 'Enterprise C', 90, '低风险', '2026-07-29 11:00:00'), result('task-5', 'Enterprise C', 90, '低风险', '2026-07-29 12:00:00')])

    def test_history_trend_groups_sorts_and_calculates_direction(self):
        history = [self.results[1], self.results[0], self.results[2]]
        trend = build_history_trend(history, group_by='device')
        group = trend['groups'][0]
        self.assertEqual(group['key'], 'Enterprise A')
        self.assertEqual([item['task_id'] for item in group['points']], ['task-1', 'task-2'])
        self.assertEqual(group['trends']['score']['direction'], '恶化')
        self.assertEqual(group['trends']['latency']['direction'], '恶化')
        self.assertEqual(group['trends']['throughput']['direction'], '恶化')

    def test_history_rejects_unknown_grouping(self):
        with self.assertRaises(ValueError):
            build_history_trend(self.results, group_by='unknown')


if __name__ == '__main__':
    unittest.main()
