import json

from common import metric_series, number_value


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MIN_COMPARISON_RESULTS = 2
MAX_COMPARISON_RESULTS = 4
RISK_ORDER = {'低风险': 1, '中风险': 2, '高风险': 3, '未定': 4}
FILTER_FIELDS = ('device', 'serial', 'path', 'plan', 'mode', 'status', 'conclusion', 'risk_level')
SORT_FIELDS = ('task_id', 'device', 'plan', 'status', 'conclusion', 'score', 'risk_level', 'started_at', 'ended_at', 'archived_at', 'progress', 'temperature_max', 'latency_p95', 'throughput_avg', 'health_min')
TREND_GROUP_FIELDS = ('device', 'serial', 'path', 'plan')


def _mapping(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _value(source, task, names, default=None):
    for name in names:
        value = source.get(name)
        if value not in (None, ''):
            return value
    for name in names:
        value = task.get(name)
        if value not in (None, ''):
            return value
    return default


def _metric_value(metrics, metric_name, field):
    metric = _mapping(metrics.get(metric_name))
    return number_value(metric.get(field))


def _average(values):
    if not values:
        return None
    return sum(values) / len(values)


def _analysis_and_task(record):
    source = _mapping(record)
    task = _mapping(source.get('task'))
    if not task:
        task = _mapping(source.get('result'))
    if not task:
        task = _mapping(source.get('result_json'))
    analysis = _mapping(source.get('analysis'))
    if not analysis:
        analysis = _mapping(source.get('analysis_json'))
    return source, task, analysis


def result_record(record):
    source, task, analysis = _analysis_and_task(record)
    metrics = _mapping(analysis.get('metrics'))
    samples = task.get('samples') or source.get('samples') or []
    temperatures = metric_series(samples, 'temperature')
    latencies = metric_series(samples, 'p99')
    throughputs = metric_series(samples, 'throughput')
    health_values = metric_series(samples, 'health')
    score = number_value(_value(source, analysis, ('score',)))
    progress = number_value(_value(source, task, ('progress',)))
    temperature_max = number_value(_value(source, task, ('temperature_max', 'max_temperature')))
    latency_p95 = number_value(_value(source, task, ('latency_p95', 'p99_p95')))
    throughput_avg = number_value(_value(source, task, ('throughput_avg', 'average_throughput')))
    health_min = number_value(_value(source, task, ('health_min', 'minimum_health')))
    if temperature_max is None:
        temperature_max = _metric_value(metrics, 'temperature', 'max')
    if latency_p95 is None:
        latency_p95 = _metric_value(metrics, 'latency', 'p95')
    if throughput_avg is None:
        throughput_avg = _metric_value(metrics, 'throughput', 'avg')
    if health_min is None:
        health_min = _metric_value(metrics, 'health', 'min')
    if temperature_max is None and temperatures:
        temperature_max = max(temperatures)
    if latency_p95 is None and latencies:
        latency_p95 = max(latencies)
    if throughput_avg is None and throughputs:
        throughput_avg = _average(throughputs)
    if health_min is None and health_values:
        health_min = min(health_values)
    event_count = len(task.get('events') or source.get('events') or [])
    return {
        'task_id': _value(source, task, ('task_id', 'id'), '未编号'),
        'device': _value(source, task, ('device', 'model'), '未知设备'),
        'serial': _value(source, task, ('serial',), ''),
        'path': _value(source, task, ('device_path', 'path'), ''),
        'plan': _value(source, task, ('plan', 'strategy_name'), ''),
        'mode': _value(source, task, ('mode',), ''),
        'status': _value(source, task, ('task_status', 'status'), '未知'),
        'conclusion': _value(source, analysis, ('conclusion',), _value(task, task, ('result',), '--')),
        'score': round(score, 2) if score is not None else None,
        'risk_level': _value(source, analysis, ('risk_level',), '未定'),
        'started_at': _value(source, task, ('started_at',), None),
        'ended_at': _value(source, task, ('ended_at',), None),
        'archived_at': _value(source, task, ('archived_at',), None),
        'progress': round(progress, 2) if progress is not None else None,
        'temperature_max': round(temperature_max, 2) if temperature_max is not None else None,
        'latency_p95': round(latency_p95, 2) if latency_p95 is not None else None,
        'throughput_avg': round(throughput_avg, 2) if throughput_avg is not None else None,
        'health_min': round(health_min, 2) if health_min is not None else None,
        'sample_count': len(samples),
        'event_count': event_count,
    }


def _filter_value_matches(actual, expected):
    if isinstance(expected, (list, tuple, set)):
        return any(_filter_value_matches(actual, item) for item in expected)
    if expected in (None, ''):
        return True
    return str(actual or '').casefold() == str(expected).casefold()


def _matches_filters(item, filters):
    for field in FILTER_FIELDS:
        if not _filter_value_matches(item.get(field), filters.get(field)):
            return False
    search = str(filters.get('search') or '').strip().casefold()
    if search:
        searchable = ' '.join(str(item.get(field) or '') for field in ('task_id', 'device', 'serial', 'path', 'plan', 'conclusion', 'risk_level')).casefold()
        if search not in searchable:
            return False
    score_min = number_value(filters.get('score_min'))
    score_max = number_value(filters.get('score_max'))
    if score_min is not None and (item.get('score') is None or item['score'] < score_min):
        return False
    if score_max is not None and (item.get('score') is None or item['score'] > score_max):
        return False
    started_after = filters.get('started_after')
    started_before = filters.get('started_before')
    started_at = item.get('started_at') or ''
    if started_after and (not started_at or started_at < str(started_after)):
        return False
    if started_before and (not started_at or started_at > str(started_before)):
        return False
    return True


def _integer(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _sort_items(items, sort_by, descending):
    field = sort_by if sort_by in SORT_FIELDS else 'started_at'
    ordered = list(items)
    ordered.sort(key=lambda item: str(item.get('task_id') or ''))
    if field == 'risk_level':
        ordered.sort(key=lambda item: RISK_ORDER.get(item.get('risk_level'), 0), reverse=descending)
    elif field in ('score', 'progress', 'temperature_max', 'latency_p95', 'throughput_avg', 'health_min'):
        ordered.sort(key=lambda item: item.get(field) if item.get(field) is not None else 0, reverse=descending)
    else:
        ordered.sort(key=lambda item: str(item.get(field) or ''), reverse=descending)
    ordered.sort(key=lambda item: item.get(field) is None or item.get(field) == '')
    return ordered


def query_results(records, filters=None, page=1, page_size=DEFAULT_PAGE_SIZE, sort_by='started_at', descending=True):
    active_filters = dict(filters or {})
    normalized = [result_record(record) for record in records or []]
    matched = [item for item in normalized if _matches_filters(item, active_filters)]
    ordered = _sort_items(matched, sort_by, bool(descending))
    size = _integer(page_size, DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    total = len(ordered)
    total_pages = max(1, (total + size - 1) // size)
    current_page = _integer(page, 1, 1, total_pages)
    offset = (current_page - 1) * size
    return {
        'items': ordered[offset:offset + size],
        'total': total,
        'page': current_page,
        'page_size': size,
        'total_pages': total_pages,
        'sort_by': sort_by if sort_by in SORT_FIELDS else 'started_at',
        'descending': bool(descending),
        'filters': active_filters,
    }


def filter_facets(records):
    normalized = [result_record(record) for record in records or []]
    facets = {}
    for field in FILTER_FIELDS:
        counts = {}
        for item in normalized:
            value = item.get(field)
            if value in (None, ''):
                continue
            counts[value] = counts.get(value, 0) + 1
        facets[field] = [{'value': value, 'count': counts[value]} for value in sorted(counts, key=lambda item: str(item))]
    return facets


def _metric_comparison(items, field, label, higher_is_better):
    values = []
    for item in items:
        value = item.get(field)
        if value is not None:
            values.append({'task_id': item['task_id'], 'value': value})
    if not values:
        return {'field': field, 'label': label, 'available': False, 'values': []}
    best_value = max(value['value'] for value in values) if higher_is_better else min(value['value'] for value in values)
    best_ids = [value['task_id'] for value in values if value['value'] == best_value]
    comparisons = []
    for value in values:
        gap = value['value'] - best_value if higher_is_better else best_value - value['value']
        comparisons.append({'task_id': value['task_id'], 'value': value['value'], 'gap_to_best': round(gap, 2)})
    return {
        'field': field,
        'label': label,
        'available': True,
        'higher_is_better': higher_is_better,
        'best_value': best_value,
        'best_task_ids': best_ids,
        'minimum': min(value['value'] for value in values),
        'maximum': max(value['value'] for value in values),
        'spread': round(max(value['value'] for value in values) - min(value['value'] for value in values), 2),
        'values': comparisons,
    }


def compare_results(records):
    items = [result_record(record) for record in records or []]
    if not MIN_COMPARISON_RESULTS <= len(items) <= MAX_COMPARISON_RESULTS:
        raise ValueError('请选择 {0} 至 {1} 条测试结果进行对比'.format(MIN_COMPARISON_RESULTS, MAX_COMPARISON_RESULTS))
    comparisons = [
        _metric_comparison(items, 'score', '稳定性评分', True),
        _metric_comparison(items, 'throughput_avg', '平均吞吐（MB/s）', True),
        _metric_comparison(items, 'latency_p95', 'P99 延迟 P95（ms）', False),
        _metric_comparison(items, 'temperature_max', '最高温度（°C）', False),
        _metric_comparison(items, 'health_min', '最低健康度（%）', True),
    ]
    scores = [item['score'] for item in items if item['score'] is not None]
    risks = {}
    for item in items:
        risk = item.get('risk_level') or '未定'
        risks[risk] = risks.get(risk, 0) + 1
    worst_risk = max(risks, key=lambda item: RISK_ORDER.get(item, 0)) if risks else '未定'
    if worst_risk == '高风险':
        recommendation = '存在高风险测试结果，建议优先复测并排查温度、延迟和介质健康度。'
    elif scores and max(scores) - min(scores) >= 15:
        recommendation = '同组结果稳定性评分差异较大，建议按设备序列号和压力参数继续定位差异来源。'
    else:
        recommendation = '同组结果未发现明显的综合评分分化，可结合具体指标继续进行验收判断。'
    return {
        'items': items,
        'metrics': comparisons,
        'summary': {
            'count': len(items),
            'risk_distribution': risks,
            'worst_risk': worst_risk,
            'score_min': min(scores) if scores else None,
            'score_max': max(scores) if scores else None,
            'score_spread': round(max(scores) - min(scores), 2) if len(scores) >= 2 else None,
            'recommendation': recommendation,
        },
    }


def _history_time(item):
    return item.get('started_at') or item.get('ended_at') or item.get('archived_at') or ''


def _metric_trend(points, field, higher_is_better):
    values = [point[field] for point in points if point.get(field) is not None]
    if len(values) < 2:
        return {'available': False, 'sample_count': len(values), 'direction': '数据不足'}
    first = values[0]
    latest = values[-1]
    change = latest - first
    change_pct = None if first == 0 else change / first * 100
    tolerance = max(abs(first) * 0.01, 0.01)
    if abs(change) <= tolerance:
        direction = '稳定'
    elif (change > 0) == higher_is_better:
        direction = '改善'
    else:
        direction = '恶化'
    return {
        'available': True,
        'sample_count': len(values),
        'first': first,
        'latest': latest,
        'change': round(change, 2),
        'change_pct': round(change_pct, 2) if change_pct is not None else None,
        'direction': direction,
    }


def history_trend(records, group_by='device'):
    if group_by not in TREND_GROUP_FIELDS:
        raise ValueError('历史趋势分组字段不支持：{0}'.format(group_by))
    grouped = {}
    for record in records or []:
        item = result_record(record)
        key = item.get(group_by) or '未标识'
        grouped.setdefault(key, []).append(item)
    groups = []
    for key in sorted(grouped, key=lambda item: str(item)):
        points = sorted(grouped[key], key=lambda item: (_history_time(item), str(item.get('task_id') or '')))
        groups.append({
            'key': key,
            'count': len(points),
            'points': points,
            'trends': {
                'score': _metric_trend(points, 'score', True),
                'throughput': _metric_trend(points, 'throughput_avg', True),
                'latency': _metric_trend(points, 'latency_p95', False),
                'temperature': _metric_trend(points, 'temperature_max', False),
                'health': _metric_trend(points, 'health_min', True),
            },
        })
    return {'group_by': group_by, 'total_results': sum(group['count'] for group in groups), 'groups': groups}
