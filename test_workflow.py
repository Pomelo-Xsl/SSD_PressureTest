from copy import deepcopy
from math import ceil
from statistics import mean


POLICY_PHASES = {
    'plan-burnin': [('预热', 5, '建立写放大与缓存稳定前的基线'), ('耐久负载', 90, '执行随机混合读写，观察温度、尾延迟和吞吐衰减'), ('恢复观察', 5, '降低负载，观察延迟与吞吐恢复')],
    'plan-stability': [('预热', 10, '消除冷启动和缓存初始状态影响'), ('稳定负载', 80, '以均衡读写负载验证持续稳定性'), ('恢复观察', 10, '观察热状态回落和性能恢复')],
    'plan-spike': [('预热', 10, '建立突发负载前的响应基线'), ('突发负载', 75, '使用高 QD 随机 I/O 观察尾延迟尖峰'), ('恢复观察', 15, '观察负载撤除后的恢复能力')],
}


def build_test_stages(plan_id, config):
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


REQUIRED_STRATEGY_FIELDS = {'id', 'name', 'duration', 'block_size', 'read_ratio', 'queue_depth', 'threshold_temp'}


def validate_strategy(strategy, block_sizes):
    missing = REQUIRED_STRATEGY_FIELDS - set(strategy)
    if missing:
        raise ValueError('策略缺少字段：' + '、'.join(sorted(missing)))
    if not str(strategy['name']).strip():
        raise ValueError('策略名称不能为空')
    if int(strategy['duration']) < 1 or int(strategy['duration']) > 720:
        raise ValueError('策略时长必须在 1 到 720 小时之间')
    if strategy['block_size'] not in block_sizes:
        raise ValueError('策略块大小不受支持')
    if int(strategy['read_ratio']) < 0 or int(strategy['read_ratio']) > 100:
        raise ValueError('策略读比例必须在 0 到 100 之间')
    if int(strategy['queue_depth']) < 1 or int(strategy['queue_depth']) > 1024:
        raise ValueError('策略队列深度必须在 1 到 1024 之间')
    if int(strategy['threshold_temp']) < 35 or int(strategy['threshold_temp']) > 90:
        raise ValueError('策略温度阈值必须在 35 到 90 摄氏度之间')


def normalize_strategy(strategy, block_sizes):
    item = deepcopy(strategy)
    item['name'] = str(item['name']).strip()[:80]
    item['description'] = str(item.get('description') or '').strip()[:240]
    item['duration'] = int(item['duration'])
    item['read_ratio'] = int(item['read_ratio'])
    item['queue_depth'] = int(item['queue_depth'])
    item['threshold_temp'] = int(item['threshold_temp'])
    item['enabled'] = bool(item.get('enabled', True))
    item['version'] = int(item.get('version', 1))
    validate_strategy(item, block_sizes)
    return item


def enabled_strategies(plans):
    return [plan for plan in plans if plan.get('enabled', True)]


def strategy_snapshot(plan):
    return {'id': plan['id'], 'name': plan['name'], 'version': plan.get('version', 1), 'duration': plan['duration'], 'block_size': plan['block_size'], 'read_ratio': plan['read_ratio'], 'queue_depth': plan['queue_depth'], 'threshold_temp': plan['threshold_temp'], 'description': plan.get('description', '')}


TERMINAL_STATUSES = {'已完成', '已停止', '已中断', '失败'}
ACTIVE_STATUSES = {'运行中', '停止中'}
QUEUE_STATUS = '排队中'


def priority_value(task):
    value = task.get('priority', '普通')
    levels = {'紧急': 300, '高': 200, '普通': 100, '低': 0}
    return levels.get(value, levels['普通'])


def task_sort_key(task):
    return (-priority_value(task), task.get('queue_sequence', 0), task.get('created_at', ''), task.get('id', ''))


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


def summarize_queue(tasks):
    queued = queued_tasks(tasks)
    by_priority = {'紧急': 0, '高': 0, '普通': 0, '低': 0}
    for task in queued:
        priority = task.get('priority', '普通')
        by_priority[priority] = by_priority.get(priority, 0) + 1
    return {'active_count': len(active_tasks(tasks)), 'queued_count': len(queued), 'by_priority': by_priority, 'next_task_id': queued[0].get('id') if queued else None}


def can_change_priority(task):
    return task.get('status') == QUEUE_STATUS


def normalize_priority(value):
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


def telemetry_number(value):
    if value in (None, '', '--'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_sample(task, sample):
    events = []
    temperature = telemetry_number(sample.get('temperature'))
    p99 = telemetry_number(sample.get('p99'))
    throughput = telemetry_number(sample.get('throughput'))
    threshold = telemetry_number(task.get('threshold_temp')) or 70
    if temperature is not None:
        if temperature >= threshold:
            events.append(('严重', '温度 {0:.1f}°C 达到阈值 {1:.1f}°C'.format(temperature, threshold)))
        elif temperature >= threshold - 3:
            events.append(('警告', '温度 {0:.1f}°C 接近阈值 {1:.1f}°C'.format(temperature, threshold)))
    if p99 is not None:
        if p99 >= 50:
            events.append(('严重', 'P99 延迟达到 {0:.2f} ms'.format(p99)))
        elif p99 >= 20:
            events.append(('警告', 'P99 延迟偏高：{0:.2f} ms'.format(p99)))
    samples = task.get('samples') or []
    throughput_history = [telemetry_number(item.get('throughput')) for item in samples[-12:]]
    throughput_history = [item for item in throughput_history if item is not None]
    if throughput is not None and len(throughput_history) >= 6:
        baseline = mean(throughput_history[:-1])
        if baseline and throughput <= baseline * 0.7:
            events.append(('警告', '当前吞吐 {0:.1f} MB/s，较近期均值下降超过 30%'.format(throughput)))
    return events


def telemetry_summary(samples):
    fields = {'temperature': '温度', 'p99': 'P99 延迟', 'throughput': '吞吐', 'health': '健康度'}
    summary = {}
    for field, label in fields.items():
        values = [telemetry_number(sample.get(field)) for sample in samples]
        values = [value for value in values if value is not None]
        if not values:
            summary[field] = {'label': label, 'count': 0, 'min': None, 'max': None, 'avg': None}
            continue
        summary[field] = {'label': label, 'count': len(values), 'min': round(min(values), 2), 'max': round(max(values), 2), 'avg': round(mean(values), 2)}
    return summary
