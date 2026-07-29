import json


def build_fio_command(task, runtime_seconds, stage_name=None):
    name = 'enterprise-ssd-{0}'.format(task['id'])
    if stage_name:
        name += '-' + str(stage_name)
    command = ['fio', '--name={0}'.format(name), '--filename={0}'.format(task['path']), '--direct=1', '--ioengine=libaio', '--time_based=1', '--runtime={0}'.format(runtime_seconds), '--rw={0}'.format(task['io_pattern']), '--bs={0}'.format(task['block_size']), '--iodepth={0}'.format(task['queue_depth']), '--numjobs={0}'.format(task['num_jobs']), '--ramp_time={0}'.format(task.get('ramp_time', 0)), '--group_reporting=1', '--output-format=json']
    if task.get('io_pattern') == 'randrw':
        command.append('--rwmixread={0}'.format(task['read_ratio']))
    if task.get('rate_limit'):
        command.append('--rate={0}M'.format(task['rate_limit']))
    if task.get('verify') and task['verify'] != 'none':
        command.append('--verify={0}'.format(task['verify']))
    for key, value in task.get('extra_options', {}).items():
        command.append('--{0}={1}'.format(key, value))
    return command


def parse_fio_json(output):
    data = json.loads(output)
    jobs = data.get('jobs') or []
    if not jobs:
        raise ValueError('fio 输出中没有 jobs 数据')
    read_bandwidth = 0
    write_bandwidth = 0
    p99_ns = 0
    read_iops = 0
    write_iops = 0
    for job in jobs:
        read = job.get('read') or {}
        write = job.get('write') or {}
        read_bandwidth += read.get('bw_bytes', 0)
        write_bandwidth += write.get('bw_bytes', 0)
        read_iops += read.get('iops', 0)
        write_iops += write.get('iops', 0)
        read_p99 = ((read.get('clat_ns') or {}).get('percentile') or {}).get('99.000000', 0)
        write_p99 = ((write.get('clat_ns') or {}).get('percentile') or {}).get('99.000000', 0)
        p99_ns = max(p99_ns, read_p99, write_p99)
    return {'p99_ms': round(p99_ns / 1000000.0, 2), 'throughput_mbps': round((read_bandwidth + write_bandwidth) / 1000000.0, 2), 'read_mbps': round(read_bandwidth / 1000000.0, 2), 'write_mbps': round(write_bandwidth / 1000000.0, 2), 'read_iops': round(read_iops, 2), 'write_iops': round(write_iops, 2), 'job_count': len(jobs)}


def stage_progress(stages, completed_seconds):
    total_seconds = sum(stage.get('duration_seconds', 0) for stage in stages)
    if not total_seconds:
        return {'overall_progress': 0, 'stage_index': 0, 'stage_progress': 0}
    consumed = 0
    for index, stage in enumerate(stages):
        duration = stage.get('duration_seconds', 0)
        if completed_seconds < consumed + duration:
            local_elapsed = max(0, completed_seconds - consumed)
            return {'overall_progress': round(min(100, completed_seconds / total_seconds * 100), 1), 'stage_index': index, 'stage_progress': round(local_elapsed / duration * 100, 1) if duration else 100}
        consumed += duration
    return {'overall_progress': 100, 'stage_index': max(0, len(stages) - 1), 'stage_progress': 100}


def summarize_stage_result(stage, fio_metrics, elapsed_seconds):
    return {'stage': stage.get('name'), 'ordinal': stage.get('ordinal'), 'planned_seconds': stage.get('duration_seconds'), 'elapsed_seconds': elapsed_seconds, 'metrics': fio_metrics, 'completed': elapsed_seconds >= stage.get('duration_seconds', 0)}
