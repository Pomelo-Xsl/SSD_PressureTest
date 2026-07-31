from copy import deepcopy
from math import ceil
from statistics import mean

from common import metric_series, number_value


POLICY_PHASES = {
    'plan-burnin': [('预热', 5, '建立写放大与缓存稳定前的基线'), ('耐久负载', 90, '执行随机混合读写，观察温度、尾延迟和吞吐衰减'), ('恢复观察', 5, '降低负载，观察延迟与吞吐恢复')],
    'plan-stability': [('预热', 10, '消除冷启动和缓存初始状态影响'), ('稳定负载', 80, '以均衡读写负载验证持续稳定性'), ('恢复观察', 10, '观察热状态回落和性能恢复')],
    'plan-spike': [('预热', 10, '建立突发负载前的响应基线'), ('突发负载', 75, '使用高 QD 随机 I/O 观察尾延迟尖峰'), ('恢复观察', 15, '观察负载撤除后的恢复能力')],
}
SUPPORTED_BLOCK_SIZES = {'4K', '8K', '16K', '32K', '64K', '128K', '256K', '1M'}
SUPPORTED_IO_PATTERNS = {'randrw', 'randread', 'randwrite', 'read', 'write'}
SUPPORTED_VERIFY_MODES = {'none', 'crc32c'}
RANDOM_GENERATORS = {'tausworthe', 'tausworthe64', 'lfsr'}
TEST_FIELD_RANGES = {
    'duration': (1, 720),
    'read_ratio': (0, 100),
    'queue_depth': (1, 1024),
    'threshold_temp': (35, 90),
    'num_jobs': (1, 32),
    'ramp_time': (0, 3600),
    'rate_limit': (0, 20000),
}
FIO_OPTION_RANGES = {
    'thinktime': (0, 1000000),
    'thinktime_blocks': (1, 100000),
    'iodepth_batch_submit': (1, 1024),
    'iodepth_batch_complete_min': (1, 1024),
    'iodepth_batch_complete_max': (1, 1024),
    'norandommap': (0, 1),
    'refill_buffers': (0, 1),
}


def planned_stages(plan_id, config):
    phase_definition = POLICY_PHASES.get(plan_id, POLICY_PHASES['plan-stability'])
    duration_seconds = int(config['duration']) * 3600
    stages = []
    allocated = 0
    for index, (name, percentage, purpose) in enumerate(phase_definition):
        if index == len(phase_definition) - 1:
            seconds = duration_seconds - allocated
        else:
            seconds = max(1, int(duration_seconds * percentage / 100))
            allocated += seconds
        stages.append({'ordinal': index + 1, 'name': name, 'ratio': percentage, 'duration_seconds': seconds, 'duration_minutes': round(seconds / 60, 1), 'purpose': purpose})
    return stages


def estimate_test_envelope(config):
    duration_seconds = int(config['duration']) * 3600
    rate_limit = int(config.get('rate_limit') or 0)
    jobs = int(config.get('num_jobs') or 1)
    queue_depth = int(config['queue_depth'])
    block_size = config['block_size']
    block_units = {'4K': 4, '8K': 8, '16K': 16, '32K': 32, '64K': 64, '128K': 128, '256K': 256, '1M': 1024}
    estimated_iops = max(1, jobs * queue_depth * 100)
    if rate_limit:
        throughput_mbps = rate_limit
        estimated_iops = max(1, int(rate_limit * 1024 / block_units.get(block_size, 4)))
    else:
        throughput_mbps = None
    estimated_write_ratio = 100 - int(config['read_ratio'])
    estimated_write_gib = None
    if throughput_mbps is not None:
        estimated_write_gib = round(throughput_mbps * duration_seconds * estimated_write_ratio / 100 / 1024, 2)
    return {'duration_seconds': duration_seconds, 'estimated_iops_ceiling': estimated_iops, 'rate_limited': bool(rate_limit), 'rate_limit_mbps': rate_limit or None, 'estimated_write_gib_when_limited': estimated_write_gib, 'minimum_sample_count': max(3, ceil(duration_seconds / 60))}


def active_stage(stages, elapsed_seconds):
    if not stages:
        return None
    consumed = 0
    for stage in stages:
        consumed += stage['duration_seconds']
        if elapsed_seconds < consumed:
            return stage
    return stages[-1]


def _fio_options(raw_options):
    if not raw_options:
        return {}
    if not isinstance(raw_options, str):
        raise ValueError('fio 扩展参数应为文本')
    options = {}
    for row in raw_options.splitlines():
        row = row.strip()
        if not row:
            continue
        if '=' not in row:
            raise ValueError('fio 扩展参数缺少“=”：{0}'.format(row))
        key, value = (part.strip() for part in row.split('=', 1))
        bounds = FIO_OPTION_RANGES.get(key)
        if bounds:
            try:
                value = int(value)
            except ValueError:
                raise ValueError('{0} 只接受整数'.format(key))
            if value < bounds[0] or value > bounds[1]:
                raise ValueError('{0} 超出 fio 可接受范围'.format(key))
            options[key] = value
            continue
        if key == 'random_generator' and value in RANDOM_GENERATORS:
            options[key] = value
            continue
        raise ValueError('fio 扩展参数未开放：{0}'.format(key))
    return options


def test_config(plan, overrides=None, block_sizes=None):
    source = overrides or {}
    blocks = block_sizes or SUPPORTED_BLOCK_SIZES
    integer_fields = ('duration', 'read_ratio', 'queue_depth', 'threshold_temp', 'num_jobs', 'ramp_time', 'rate_limit')
    defaults = {'num_jobs': 1, 'ramp_time': 0, 'rate_limit': 0}
    config = {}
    try:
        for field in integer_fields:
            config[field] = int(source.get(field, plan.get(field, defaults.get(field))))
    except (TypeError, ValueError):
        raise ValueError('测试参数中存在无法识别的数字')
    config['block_size'] = str(source.get('block_size', plan['block_size']))
    config['io_pattern'] = str(source.get('io_pattern', plan.get('io_pattern', 'randrw')))
    config['verify'] = str(source.get('verify', plan.get('verify', 'none')))
    config['extra_options'] = _fio_options(source.get('extra_options', ''))
    for field, bounds in TEST_FIELD_RANGES.items():
        value = config[field]
        if value < bounds[0] or value > bounds[1]:
            raise ValueError('{0}={1} 超出允许区间 [{2}, {3}]'.format(field, value, bounds[0], bounds[1]))
    if config['block_size'] not in blocks:
        raise ValueError('当前 fio 配置不接受块大小 {0}'.format(config['block_size']))
    if config['io_pattern'] not in SUPPORTED_IO_PATTERNS:
        raise ValueError('当前执行器未启用 I/O 模式 {0}'.format(config['io_pattern']))
    if config['verify'] not in SUPPORTED_VERIFY_MODES:
        raise ValueError('当前执行器未启用校验方式 {0}'.format(config['verify']))
    return config


REQUIRED_STRATEGY_FIELDS = {'id', 'name', 'duration', 'block_size', 'read_ratio', 'queue_depth', 'threshold_temp'}


def strategy_config(strategy, block_sizes=None):
    missing = REQUIRED_STRATEGY_FIELDS - set(strategy)
    if missing:
        raise ValueError('策略缺少字段：' + '、'.join(sorted(missing)))
    item = deepcopy(strategy)
    item['name'] = str(item['name']).strip()[:80]
    if not item['name']:
        raise ValueError('策略名称为空')
    item['description'] = str(item.get('description') or '').strip()[:240]
    checked = test_config(item, item, block_sizes)
    for field in ('duration', 'block_size', 'read_ratio', 'queue_depth', 'threshold_temp'):
        item[field] = checked[field]
    item['enabled'] = bool(item.get('enabled', True))
    item['version'] = int(item.get('version', 1))
    return item


def enabled_strategies(plans):
    return [plan for plan in plans if plan.get('enabled', True)]


def strategy_snapshot(plan):
    return {'id': plan['id'], 'name': plan['name'], 'version': plan.get('version', 1), 'duration': plan['duration'], 'block_size': plan['block_size'], 'read_ratio': plan['read_ratio'], 'queue_depth': plan['queue_depth'], 'threshold_temp': plan['threshold_temp'], 'description': plan.get('description', '')}


TERMINAL_STATUSES = {'已完成', '已停止', '已中断', '失败'}
ACTIVE_STATUSES = {'运行中', '停止中'}
QUEUE_STATUS = '排队中'


def priority_rank(task):
    value = task.get('priority', '普通')
    levels = {'紧急': 300, '高': 200, '普通': 100, '低': 0}
    return levels.get(value, levels['普通'])


def task_sort_key(task):
    return (-priority_rank(task), task.get('queue_sequence', 0), task.get('created_at', ''), task.get('id', ''))


def active_tasks(tasks):
    return [task for task in tasks if task.get('status') in ACTIVE_STATUSES]


def queued_tasks(tasks):
    candidates = [task for task in tasks if task.get('status') == QUEUE_STATUS]
    return sorted(candidates, key=task_sort_key)


def next_runnable_task(tasks):
    if active_tasks(tasks):
        return None
    candidates = queued_tasks(tasks)
    return candidates[0] if candidates else None


def queue_position(tasks, task_id):
    for index, task in enumerate(queued_tasks(tasks), 1):
        if task.get('id') == task_id:
            return index
    return None


def queue_overview(tasks):
    queued = queued_tasks(tasks)
    by_priority = {'紧急': 0, '高': 0, '普通': 0, '低': 0}
    for task in queued:
        priority = task.get('priority', '普通')
        by_priority[priority] = by_priority.get(priority, 0) + 1
    return {'active_count': len(active_tasks(tasks)), 'queued_count': len(queued), 'by_priority': by_priority, 'next_task_id': queued[0].get('id') if queued else None}


def can_change_priority(task):
    return task.get('status') == QUEUE_STATUS


def priority_value(value):
    allowed = {'紧急', '高', '普通', '低'}
    return value if value in allowed else '普通'


def batch_status(tasks):
    statuses = [task.get('status') for task in tasks]
    if not statuses:
        return '空批次'
    if any(status in ACTIVE_STATUSES or status == QUEUE_STATUS for status in statuses):
        return '执行中'
    if any(status == '失败' for status in statuses):
        return '已完成（含失败）'
    if any(status in {'已停止', '已中断'} for status in statuses):
        return '已完成（含中断）'
    return '已完成'


def check_sample_limits(task, sample):
    events = []
    temperature = number_value(sample.get('temperature'))
    p99 = number_value(sample.get('p99'))
    throughput = number_value(sample.get('throughput'))
    threshold = number_value(task.get('threshold_temp')) or 70
    if temperature is not None:
        if temperature >= threshold:
            events.append(('严重', 'SSD 温度 {0:.1f}°C 已触及停测线 {1:.1f}°C'.format(temperature, threshold)))
        elif temperature >= threshold - 3:
            events.append(('警告', 'SSD 温度距停测线仅剩 {0:.1f}°C（当前 {1:.1f}°C）'.format(threshold - temperature, temperature)))
    if p99 is not None:
        if p99 >= 50:
            events.append(('严重', 'P99 尾延迟升至 {0:.2f} ms，负载响应已明显失稳'.format(p99)))
        elif p99 >= 20:
            events.append(('警告', 'P99 尾延迟为 {0:.2f} ms，需结合温度与吞吐曲线复核'.format(p99)))
    samples = task.get('samples') or []
    throughput_history = metric_series(samples[-12:], 'throughput')
    if throughput is not None and len(throughput_history) >= 6:
        baseline = mean(throughput_history[:-1])
        if baseline and throughput <= baseline * 0.7:
            events.append(('警告', 'SSD 当前吞吐 {0:.1f} MB/s，较本轮近期均值衰减超过 30%'.format(throughput)))
    return events


def telemetry_rollup(samples):
    fields = {'temperature': '温度', 'p99': 'P99 延迟', 'throughput': '吞吐', 'health': '健康度'}
    summary = {}
    for field, label in fields.items():
        values = metric_series(samples, field)
        if not values:
            summary[field] = {'label': label, 'count': 0, 'min': None, 'max': None, 'avg': None}
            continue
        summary[field] = {'label': label, 'count': len(values), 'min': round(min(values), 2), 'max': round(max(values), 2), 'avg': round(mean(values), 2)}
    return summary
