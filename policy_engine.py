from math import ceil


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
