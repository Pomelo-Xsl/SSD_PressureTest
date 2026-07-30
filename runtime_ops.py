import json
import os
import platform
import shutil
import sqlite3
import subprocess
from datetime import datetime
from hashlib import sha256
from pathlib import Path


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


def database_health(database_file):
    path = Path(database_file)
    if not path.exists():
        return {'exists': False, 'size_bytes': 0, 'integrity': '未创建', 'tables': []}
    with sqlite3.connect(path) as connection:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {'exists': True, 'size_bytes': path.stat().st_size, 'integrity': integrity, 'tables': tables}


def backup_database(database_file, backup_folder, timestamp):
    source = Path(database_file)
    destination_folder = Path(backup_folder)
    if not source.exists():
        raise FileNotFoundError('结果数据库尚未创建')
    destination_folder.mkdir(parents=True, exist_ok=True)
    target = destination_folder / 'results_{0}.db'.format(timestamp)
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)
    return target


def list_backups(backup_folder):
    folder = Path(backup_folder)
    if not folder.exists():
        return []
    items = []
    for file in sorted(folder.glob('results_*.db'), key=lambda item: item.stat().st_mtime, reverse=True):
        items.append({'name': file.name, 'size_bytes': file.stat().st_size, 'modified_at': datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    return items


def prune_backups(backup_folder, keep_count):
    backups = list_backups(backup_folder)
    removed = []
    for item in backups[max(0, int(keep_count)):]:
        path = Path(backup_folder) / item['name']
        path.unlink(missing_ok=True)
        removed.append(item['name'])
    return removed


def asset_id(device):
    identity = '|'.join([str(device.get('serial') or ''), str(device.get('path') or ''), str(device.get('name') or '')])
    return sha256(identity.encode('utf-8')).hexdigest()[:16]


def classify_device_risk(device):
    reasons = device.get('test_reasons') or []
    text = '；'.join(reasons)
    if device.get('testable'):
        return {'level': '低', 'score': 0, 'action': '可作为专用测试盘候选，启动前仍需操作员确认。'}
    if any(keyword in text for keyword in ('系统盘', '启动盘', '交换分区', '挂载', '分区')):
        return {'level': '高', 'score': 90, 'action': '禁止裸盘测试；请先确认该盘没有系统、业务或分区依赖。'}
    if any(keyword in text for keyword in ('机械盘', '只读', '非 SSD')):
        return {'level': '高', 'score': 80, 'action': '设备类型或写入状态不满足测试要求。'}
    return {'level': '中', 'score': 50, 'action': '请复核设备信息、控制器状态和测试准入原因。'}


def enrich_device(device):
    item = dict(device)
    item['asset_id'] = asset_id(item)
    item['risk'] = classify_device_risk(item)
    item['asset_label'] = '{0} · {1}'.format(item.get('name', '未知 SSD'), item.get('serial', '未读取序列号'))
    return item


def summarize_inventory(devices):
    summary = {'total': len(devices), 'testable': 0, 'high_risk': 0, 'medium_risk': 0, 'low_risk': 0, 'by_interface': {}}
    for device in devices:
        if device.get('testable'):
            summary['testable'] += 1
        level = device.get('risk', {}).get('level', '中')
        if level == '高':
            summary['high_risk'] += 1
        elif level == '低':
            summary['low_risk'] += 1
        else:
            summary['medium_risk'] += 1
        interface = device.get('interface', '未知')
        summary['by_interface'][interface] = summary['by_interface'].get(interface, 0) + 1
    return summary


def command_version(command):
    executable = shutil.which(command)
    if not executable:
        return {'installed': False, 'path': None, 'version': None}
    try:
        output = subprocess.run([command, '--version'], capture_output=True, text=True, timeout=5, check=False)
        version = (output.stdout or output.stderr or '').splitlines()
        version = version[0].strip() if version else '已安装'
    except (OSError, subprocess.TimeoutExpired):
        version = '已安装，版本读取失败'
    return {'installed': True, 'path': executable, 'version': version}


def collect_environment_health(destructive_enabled, is_root):
    tools = {name: command_version(name) for name in ('fio', 'nvme', 'smartctl', 'lsblk')}
    checks = []
    checks.append({'name': 'Linux 内核', 'ok': platform.system() == 'Linux', 'detail': '{0} {1}'.format(platform.system(), platform.release())})
    checks.append({'name': 'root 权限', 'ok': is_root, 'detail': '真实裸盘测试必须以 root 运行'})
    checks.append({'name': 'fio 执行器', 'ok': tools['fio']['installed'], 'detail': tools['fio']['version'] or '未安装'})
    checks.append({'name': '破坏性模式开关', 'ok': destructive_enabled, 'detail': 'ENABLE_DESTRUCTIVE_FIO=1 才允许真实裸盘测试'})
    checks.append({'name': 'nvme-cli', 'ok': tools['nvme']['installed'], 'detail': tools['nvme']['version'] or '可选：用于扩展日志'})
    checks.append({'name': 'smartmontools', 'ok': tools['smartctl']['installed'], 'detail': tools['smartctl']['version'] or '可选：用于 SATA SMART'})
    return {'platform': platform.platform(), 'hostname': platform.node(), 'pid': os.getpid(), 'checks': checks, 'tools': tools}
