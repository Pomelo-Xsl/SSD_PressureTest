import csv
import json
import io
import os
import platform
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo
from analysis_engine import analyze_task
from alert_center import acknowledge_alert, alert_summary, build_alerts
from alert_rules import build_alert_fingerprint, create_policy_version, evaluate_alert_policy, normalize_alert_policy, policy_summary
from device_assets import enrich_device, summarize_inventory
from database_maintenance import backup_database, database_health, list_backups, prune_backups
from device_history import summarize_device_history
from evidence_bundle import build_evidence_bundle
from fio_executor import build_fio_command, parse_fio_json, stage_progress, summarize_stage_result
from operations_store import OperationsStore
from policy_engine import active_stage, build_test_stages, estimate_test_envelope
from report_builder import build_report
from report_enrichment import build_report_evidence
from results_center import build_filter_facets, build_history_trend, compare_results, query_results
from system_diagnostics import collect_environment_health
from strategy_catalog import enabled_strategies, normalize_strategy, strategy_snapshot
from task_scheduler import next_runnable_task, normalize_priority, queue_position, summarize_queue
from telemetry_rules import evaluate_sample, telemetry_summary
from time_series_store import InMemoryTimeSeriesStore
ROOT, DATA_FILE = (Path(__file__).parent, Path(__file__).parent / 'data.json')
LOG_ROOT = ROOT / 'logs'
RESULTS_DB_FILE = ROOT / 'results.db'
BACKUP_ROOT = ROOT / 'backups'
EVIDENCE_ROOT = ROOT / 'evidence'
STORE = OperationsStore(RESULTS_DB_FILE)
LOCK, PROCESSES = (threading.Lock(), {})
DEFAULT_PLANS = [{'id': 'plan-burnin', 'name': '72 小时耐久老化', 'duration': 72, 'block_size': '4K', 'read_ratio': 30, 'queue_depth': 64, 'threshold_temp': 70, 'description': '随机混合 I/O，验证企业盘长时写入稳定性、温度节流与尾延迟'}, {'id': 'plan-stability', 'name': '24 小时稳定性验证', 'duration': 24, 'block_size': '128K', 'read_ratio': 50, 'queue_depth': 32, 'threshold_temp': 65, 'description': '平衡读写负载，适用于到货验收、批量抽检'}, {'id': 'plan-spike', 'name': '突发负载恢复测试', 'duration': 8, 'block_size': '4K', 'read_ratio': 20, 'queue_depth': 128, 'threshold_temp': 72, 'description': '高队列深度脉冲压力，关注延迟尖峰与恢复能力'}]
BLOCK_SIZES = {'4K', '8K', '16K', '32K', '64K', '128K', '256K', '1M'}
IO_PATTERNS = {'randrw', 'randread', 'randwrite', 'read', 'write'}
VERIFY_MODES = {'none', 'crc32c'}
EXTRA_OPTION_RULES = {'thinktime': (0, 1000000), 'thinktime_blocks': (1, 100000), 'iodepth_batch_submit': (1, 1024), 'iodepth_batch_complete_min': (1, 1024), 'iodepth_batch_complete_max': (1, 1024), 'norandommap': (0, 1), 'refill_buffers': (0, 1)}
RANDOM_GENERATORS = {'tausworthe', 'tausworthe64', 'lfsr'}
BEIJING_TZ = ZoneInfo('Asia/Shanghai')

# 前后端共用限制，避免页面允许而 fio 拒绝。
MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 720
MIN_READ_RATIO = 0
MAX_READ_RATIO = 100
MIN_QUEUE_DEPTH = 1
MAX_QUEUE_DEPTH = 1024
MIN_TEMPERATURE_C = 35
MAX_TEMPERATURE_C = 90
MIN_JOBS = 1
MAX_JOBS = 32
MAX_RAMP_SECONDS = 3600
MAX_RATE_MBPS = 20000
MAX_TASK_EVENTS = 50
MAX_TELEMETRY_SAMPLES = 200
COMMAND_TIMEOUT_SECONDS = 12
NVME_LOG_TIMEOUT_SECONDS = 180
TIME_SERIES = InMemoryTimeSeriesStore(max_samples_per_series=MAX_TELEMETRY_SAMPLES)

def beijing_time_string():
    return datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')

def beijing_timestamp_token():
    return datetime.now(BEIJING_TZ).strftime('%Y%m%d_%H%M%S')

def running_on_linux():
    return platform.system() == 'Linux'

def destructive_stress_allowed():
    return os.getenv('ENABLE_DESTRUCTIVE_FIO') == '1'

def initialize_results_database():
    STORE.initialize()

def archive_finished_task(task, history):
    if task.get('status') not in ('已完成', '已停止', '已中断', '失败'):
        return
    analysis = analyze_task(task, history)
    archived_at = beijing_time_string()
    enrichment = build_task_report_evidence(task, analysis)
    report_html = build_report(task, analysis, archived_at, enrichment).decode('utf-8')
    STORE.archive_result(task, analysis, report_html, archived_at)

def build_task_report_evidence(task, analysis):
    asset_history = []
    alerts = []
    try:
        if task.get('asset_id'):
            asset_history = STORE.asset_snapshots(task['asset_id'])
        alerts = STORE.list_alert_records(task_id=task.get('id'))
    except sqlite3.Error:
        pass
    return build_report_evidence(task, analysis, asset_history, alerts)

initialize_results_database()

def resolve_test_config(plan, overrides):
    overrides = overrides or {}
    try:
        # 自定义参数必须在服务端按白名单过滤，不能信任 Web 输入。
        raw_options = overrides.get('extra_options', '')
        if raw_options and not isinstance(raw_options, str):
            raise ValueError('自定义参数必须为文本')
        parsed_options = {}
        for line in raw_options.splitlines() if raw_options else []:
            line = line.strip()
            if not line:
                continue
            if '=' not in line:
                raise ValueError('自定义参数必须使用 参数=值 格式')
            key, value = (part.strip() for part in line.split('=', 1))
            if key in EXTRA_OPTION_RULES:
                number = int(value)
                low, high = EXTRA_OPTION_RULES[key]
                if not low <= number <= high:
                    raise ValueError('自定义参数 {0} 必须在 {1} 到 {2} 之间'.format(key, low, high))
                parsed_options[key] = number
            elif key == 'random_generator':
                if value not in RANDOM_GENERATORS:
                    raise ValueError('random_generator 仅支持 tausworthe、tausworthe64 或 lfsr')
                parsed_options[key] = value
            else:
                raise ValueError('不支持的自定义 fio 参数：{0}'.format(key))
        config = {'duration': int(overrides.get('duration', plan['duration'])), 'block_size': str(overrides.get('block_size', plan['block_size'])), 'read_ratio': int(overrides.get('read_ratio', plan['read_ratio'])), 'queue_depth': int(overrides.get('queue_depth', plan['queue_depth'])), 'threshold_temp': int(overrides.get('threshold_temp', plan['threshold_temp'])), 'io_pattern': str(overrides.get('io_pattern', 'randrw')), 'num_jobs': int(overrides.get('num_jobs', 1)), 'ramp_time': int(overrides.get('ramp_time', 0)), 'rate_limit': int(overrides.get('rate_limit', 0)), 'verify': str(overrides.get('verify', 'none')), 'extra_options': parsed_options}
    except (TypeError, ValueError):
        raise ValueError('测试参数格式不正确')
    if not MIN_DURATION_HOURS <= config['duration'] <= MAX_DURATION_HOURS:
        raise ValueError('测试时长必须在 1 到 720 小时之间')
    if config['block_size'] not in BLOCK_SIZES:
        raise ValueError('不支持的块大小')
    if not MIN_READ_RATIO <= config['read_ratio'] <= MAX_READ_RATIO:
        raise ValueError('读比例必须在 0% 到 100% 之间')
    if not MIN_QUEUE_DEPTH <= config['queue_depth'] <= MAX_QUEUE_DEPTH:
        raise ValueError('队列深度必须在 1 到 1024 之间')
    if not MIN_TEMPERATURE_C <= config['threshold_temp'] <= MAX_TEMPERATURE_C:
        raise ValueError('温度阈值必须在 35°C 到 90°C 之间')
    if config['io_pattern'] not in IO_PATTERNS:
        raise ValueError('不支持的 I/O 模式')
    if not MIN_JOBS <= config['num_jobs'] <= MAX_JOBS:
        raise ValueError('并发作业数必须在 1 到 32 之间')
    if not 0 <= config['ramp_time'] <= MAX_RAMP_SECONDS:
        raise ValueError('预热时间必须在 0 到 3600 秒之间')
    if not 0 <= config['rate_limit'] <= MAX_RATE_MBPS:
        raise ValueError('限速必须在 0 到 20000 MB/s 之间')
    if config['verify'] not in VERIFY_MODES:
        raise ValueError('不支持的数据校验模式')
    return config

def run_system_command(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None

def get_nvme_controller_path(path):
    match = re.fullmatch('(/dev/nvme\\d+)(?:n\\d+)?', path)
    return match.group(1) if match else None

def collect_telemetry(controller, output_file, critical=False):
    critical_args = ['-c'] if critical else []
    candidates = [['nvme', 'telemetry-log', controller, *critical_args, '-o', str(output_file)], ['nvme', 'telemetry-log', controller, *critical_args, '--output-file', str(output_file)], ['nvme', 'telemetry-log', controller, *critical_args, '--output-file={0}'.format(output_file)]]
    errors = []
    for cmd in candidates:
        output_file.unlink(missing_ok=True)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=NVME_LOG_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append('{0}：{1}'.format(' '.join(cmd), exc))
            continue
        if result.returncode == 0 and output_file.is_file() and (output_file.stat().st_size > 0):
            return (True, '')
        errors.append('{0}：{1}'.format(' '.join(cmd), (result.stderr or result.stdout or '未生成文件').strip()[-180:]))
    return (False, ' | '.join(errors))

def collect_nvme_logs(device):
    controller = get_nvme_controller_path(device['path'])
    if not controller:
        raise ValueError('仅支持 NVMe 设备日志采集')
    if not shutil.which('nvme'):
        raise ValueError('未安装 nvme-cli，无法采集日志')
    stamp = beijing_timestamp_token()
    folder = LOG_ROOT / '{0}_{1}'.format(device['id'], stamp)
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for name, filename, critical in [('全量 telemetry', 'telemetry_full.log', False), ('关键 telemetry', 'telemetry_critical.log', True)]:
        target = folder / filename
        ok, message = collect_telemetry(controller, target, critical)
        results.append({'name': name, 'ok': ok, 'file': str(target.relative_to(LOG_ROOT)) if ok else None, 'message': message})
    jobs = [('扩展 SMART 0xC0', ['nvme', 'get-log', controller, '-i', '0xC0', '-l', '1024']), ('扩展 SMART 0xCA', ['nvme', 'get-log', controller, '-i', '0xCA', '-l', '348'])]
    for name, cmd in jobs:
        output_file = folder / 'smart_c0.log' if '0xC0' in name else folder / 'smart_ca.log' if '0xCA' in name else None
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=NVME_LOG_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({'name': name, 'ok': False, 'message': str(exc)})
            continue
        if output_file:
            output_file.write_text(result.stdout + ('\n' + result.stderr if result.stderr else ''), encoding='utf-8')
        target = output_file or Path(cmd[-1])
        ok = result.returncode == 0 and target.exists()
        message = (result.stderr or result.stdout)[-300:] if not ok else ''
        if result.returncode == 0 and (not target.exists()):
            message = 'nvme-cli 未生成日志文件，请检查 telemetry-log --help 的输出文件参数'
        results.append({'name': name, 'ok': ok, 'file': str(target.relative_to(LOG_ROOT)) if target.exists() else None, 'message': message})
    return {'controller': controller, 'folder': str(folder.relative_to(LOG_ROOT)), 'results': results}

def flatten_mount_tree(items):
    for item in items:
        yield item
        yield from flatten_mount_tree(item.get('children', []))

def celsius(value):
    if value in (None, '', '--'):
        return '--'
    match = re.search('-?\\d+(?:\\.\\d+)?', str(value))
    if not match:
        return '--'
    temperature = float(match.group())
    if 200 <= temperature <= 450:
        temperature -= 273.15
    return round(temperature, 1)

def read_smart_log(path, transport):
    info = {'health': '--', 'temperature': '--'}
    result = run_system_command(['nvme', 'read_smart_log-log', path, '-o', 'json']) if transport == 'nvme' and shutil.which('nvme') else None
    if result and result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            info['temperature'] = celsius(data.get('temperature', '--'))
            info['health'] = data.get('percentage_used', 0)
            info['health'] = max(0, 100 - int(info['health']))
            return info
        except (ValueError, TypeError):
            pass
    result = run_system_command(['smartctl', '-A', '-j', path]) if shutil.which('smartctl') else None
    if result and result.stdout:
        try:
            data = json.loads(result.stdout)
            info['temperature'] = celsius(data.get('temperature', {}).get('current', '--'))
            info['health'] = 100 if data.get('smart_status', {}).get('passed') else '--'
        except (ValueError, TypeError):
            pass
    return info

def assess_bare_disk_eligibility(disk):
    descendants = list(flatten_mount_tree([disk]))
    partitions = [item.get('name') for item in descendants[1:] if item.get('type') == 'part']
    mountpoints = []
    for item in descendants:
        mountpoints.extend((m for m in item.get('mountpoints') or [] if m))
    mountpoints = sorted(set(mountpoints))
    reasons = []
    if str(disk.get('rota')) not in ('0', 'False', 'false', 'None'):
        reasons.append('检测为机械旋转盘，非 SSD')
    if str(disk.get('ro')) in ('1', 'True', 'true'):
        reasons.append('设备为只读状态')
    if partitions:
        reasons.append('磁盘含有分区，禁止裸盘测试')
    if mountpoints:
        if any((m in ('/', '/boot', '/boot/efi', '[SWAP]') for m in mountpoints)):
            reasons.append('包含系统盘、启动盘或交换分区')
        else:
            reasons.append('存在已挂载分区')
    return {'testable': not reasons, 'reasons': reasons, 'mountpoints': mountpoints}

def discover_linux_devices():
    if not running_on_linux() or not shutil.which('lsblk'):
        return []
    result = run_system_command(['lsblk', '--json', '--bytes', '--output', 'NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,RO,MOUNTPOINTS'])
    if not result or result.returncode:
        return []
    try:
        blocks = json.loads(result.stdout).get('blockdevices', [])
    except ValueError:
        return []
    devices = []
    for disk in flatten_mount_tree(blocks):
        if disk.get('type') != 'disk':
            continue
        path = disk.get('path', '')
        trans = (disk.get('tran') or 'nvme').lower()
        if not path.startswith('/dev/'):
            continue
        safety = assess_bare_disk_eligibility(disk)
        health = read_smart_log(path, trans)
        status = '可测试' if safety['testable'] else '不可测试'
        devices.append({'id': disk['name'], 'path': path, 'name': (disk.get('model') or 'Enterprise SSD').strip(), 'serial': (disk.get('serial') or '未读取').strip(), 'interface': 'NVMe' if trans == 'nvme' else trans.upper(), 'capacity': '{0:.0f} GB'.format(int(disk.get('size', 0)) / 1024 ** 3), 'health': health['health'], 'temperature': health['temperature'], 'mounted': bool(safety['mountpoints']), 'mountpoints': safety['mountpoints'], 'testable': safety['testable'], 'test_reasons': safety['reasons'], 'status': status})
    return devices

def build_demo_ssd_inventory():
    return [{'id': 'demo-nvme0', 'path': '/dev/nvme0n1', 'name': 'Samsung PM9A3 3.84TB', 'serial': 'DEMO-24001', 'interface': 'NVMe Gen4', 'capacity': '3.49 TB', 'health': 98, 'temperature': 38, 'mounted': False, 'mountpoints': [], 'testable': True, 'test_reasons': [], 'status': '演示设备'}]

def load_test_workspace_state():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding='utf-8'))
        except ValueError:
            pass
    return {'plans': DEFAULT_PLANS, 'tasks': []}
STATE = load_test_workspace_state()
if 'plans' not in STATE:
    STATE['plans'] = DEFAULT_PLANS
if 'tasks' not in STATE:
    STATE['tasks'] = []
if 'alert_policies' not in STATE:
    STATE['alert_policies'] = []

def persist_test_workspace_state():
    DATA_FILE.write_text(json.dumps({'plans': STATE['plans'], 'tasks': STATE['tasks'], 'alert_policies': STATE['alert_policies']}, ensure_ascii=False, indent=2), encoding='utf-8')
    for task in STATE['tasks']:
        try:
            archive_finished_task(task, STATE['tasks'])
        except sqlite3.Error:
            pass
    try:
        for batch in STORE.list_batches():
            task_states = [next((task.get('status') for task in STATE['tasks'] if task.get('id') == item['task_id']), '已中断') for item in batch['items']]
            STORE.refresh_batch(batch['batch_id'], task_states, beijing_time_string())
    except sqlite3.Error:
        pass

def append_task_event(task, severity, text, event_id=None, rule_id=None, policy_id=None, metadata=None, event_time=None):
    event_time = event_time or beijing_time_string()
    event = {'id': event_id or '{0}:event:{1}'.format(task.get('id'), uuid.uuid4().hex[:12]), 'time': event_time, 'severity': severity, 'text': text}
    if rule_id:
        event['rule_id'] = rule_id
    if policy_id:
        event['policy_id'] = policy_id
    if metadata:
        event.update(metadata)
    task.setdefault('events', []).append(event)
    task['events'] = task['events'][-MAX_TASK_EVENTS:]
    try:
        STORE.record_audit(event_time, 'task_event', severity, {'text': text, 'device': task.get('device')}, task.get('id'), task.get('path'))
    except sqlite3.Error:
        pass
    if severity in ('警告', '严重'):
        try:
            STORE.upsert_alert_record({'id': event['id'], 'task_id': task.get('id'), 'asset_id': task.get('asset_id') or task.get('serial') or task.get('path'), 'severity': severity, 'rule_id': rule_id, 'time': event_time, 'text': text, 'acknowledged': event['id'] in (task.get('acknowledged_alerts') or []), **event})
        except sqlite3.Error:
            pass
    return event

def append_task_sample(task, sample):
    sample = dict(sample)
    sample['stage_name'] = sample.get('stage_name') or task.get('active_stage')
    task.setdefault('samples', []).append(sample)
    task['samples'] = task['samples'][-MAX_TELEMETRY_SAMPLES:]
    try:
        STORE.append_metric_sample(task['id'], sample, task.get('active_stage'))
    except sqlite3.Error:
        pass
    try:
        series_sample = dict(sample)
        sample_time = series_sample.get('time')
        if sample_time:
            series_sample['timestamp'] = datetime.strptime(str(sample_time), '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING_TZ).isoformat()
        TIME_SERIES.write_samples(task['id'], series_sample, task.get('active_stage'))
    except (TypeError, ValueError):
        pass
    evaluate_task_alert_policies(task, sample)

def evaluate_task_alert_policies(task, sample):
    policy_ids = task.get('alert_policy_ids') or []
    if not policy_ids:
        return
    policies = [policy for policy in STATE['alert_policies'] if policy.get('id') in policy_ids and policy.get('enabled', True)]
    if not policies:
        return
    scope = {'task_id': task.get('id'), 'asset_id': task.get('asset_id') or task.get('serial') or task.get('path'), 'device': task.get('device'), 'path': task.get('path'), 'serial': task.get('serial')}
    metrics = {'temperature': sample.get('temperature'), 'throughput': sample.get('throughput'), 'health': sample.get('health'), 'latency': {'p99': sample.get('p99')}}
    try:
        stored_history = [dict(record.get('evidence') or {}, id=record.get('alert_id'), status=record.get('status'), opened_at=record.get('opened_at'), acknowledged_at=record.get('acknowledged_at'), closed_at=record.get('closed_at')) for record in STORE.list_alert_records(task_id=task.get('id'), limit=2000)]
    except sqlite3.Error:
        stored_history = []
    observations = task.setdefault('policy_observations', [])
    history = stored_history + observations
    for policy in policies:
        outcome = evaluate_alert_policy(policy, metrics, scope, history, sample.get('time') or beijing_time_string())
        for skipped in outcome['skipped']:
            if skipped.get('reason') != '连续触发次数不足' or not skipped.get('rule_id'):
                continue
            observations.append({'id': 'observation-{0}'.format(uuid.uuid4().hex[:16]), 'policy_id': outcome['policy_id'], 'rule_id': skipped['rule_id'], 'fingerprint': build_alert_fingerprint(outcome['policy_id'], skipped['rule_id'], scope), 'status': '观察中', 'occurred_at': outcome['evaluated_at']})
        for alert in outcome['matches']:
            append_task_event(task, alert['severity'], alert['message'], alert['id'], alert.get('rule_id'), alert.get('policy_id'), alert, alert.get('occurred_at'))
        try:
            STORE.enqueue_notifications(outcome['notifications'])
        except sqlite3.Error:
            pass
        history.extend(outcome['matches'])
    task['policy_observations'] = observations[-500:]

def restore_task_time_series():
    for task in STATE['tasks']:
        for sample in task.get('samples') or []:
            try:
                record = dict(sample)
                if record.get('time'):
                    record['timestamp'] = datetime.strptime(str(record['time']), '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING_TZ).isoformat()
                TIME_SERIES.write_samples(task.get('id'), record, task.get('active_stage'))
            except (TypeError, ValueError):
                continue

restore_task_time_series()

def recover_interrupted_tasks():
    changed = False
    for task in STATE['tasks']:
        if task.get('status') in ('运行中', '停止中', '排队中'):
            previous = task['status']
            task['status'] = '已中断'
            task['ended_at'] = beijing_time_string()
            task['result'] = '服务重启中断' if previous != '排队中' else '服务重启取消'
            task.setdefault('events', [])
            append_task_event(task, '警告', '服务重启后未检测到可恢复的执行器，原状态“{0}”任务已中断'.format(previous))
            changed = True
    if changed:
        persist_test_workspace_state()
recover_interrupted_tasks()
persist_test_workspace_state()

def collect_task_health_sample(task):
    info = read_smart_log(task['path'], task['transport'].lower())
    return {'time': beijing_time_string(), 'temperature': info['temperature'], 'p99': '--', 'throughput': '--', 'health': info['health']}

def start_task_execution(task):
    task['status'] = '运行中'
    task['started_at'] = beijing_time_string()
    append_task_event(task, '信息', '排队任务已开始执行' if task.get('queued') else '任务已开始执行')
    task['queued'] = False
    runner = run_fio_stress_task if task['mode'] == '真实 fio 裸盘' else run_demo_stress_task
    threading.Thread(target=runner, args=(task['id'],), daemon=True).start()

def start_next_queued_task():
    next_task = next_runnable_task(STATE['tasks'])
    if next_task:
        start_task_execution(next_task)

def finalize_test_task(task, result):
    task['status'] = '已完成'
    task['ended_at'] = beijing_time_string()
    task['result'] = result
    append_task_event(task, '信息', '测试完成，稳定性结论：{0}'.format(result))
    start_next_queued_task()
    persist_test_workspace_state()

def run_demo_stress_task(task_id):
    random.seed(task_id)
    while True:
        time.sleep(2)
        with LOCK:
            task = next((x for x in STATE['tasks'] if x['id'] == task_id), None)
            if not task or task['status'] != '运行中':
                return
            task['elapsed'] += 10
            task['progress'] = min(100, round(task['elapsed'] / (task['duration'] * 60) * 100, 1))
            stage = active_stage(task.get('stages') or [], task['elapsed'])
            if stage:
                task['active_stage'] = stage['name']
            p = task['progress']
            s = {'time': beijing_time_string(), 'temperature': round(38 + 18 * p / 100 + random.uniform(-2, 2), 1), 'p99': round(3 + 10 * p / 100 + random.random() * 2, 2), 'throughput': round(2400 - 420 * p / 100 + random.uniform(-80, 80)), 'health': round(100 - p * 0.015, 2)}
            append_task_sample(task, s)
            triggered_rules = task.setdefault('triggered_rules', [])
            for severity, text in evaluate_sample(task, s):
                rule_key = severity + ':' + text.split('：')[0].split('达到')[0]
                if rule_key not in triggered_rules:
                    append_task_event(task, severity, text)
                    triggered_rules.append(rule_key)
                    if severity == '严重':
                        task['temp_alerted'] = True
                    if 'P99' in text:
                        task['latency_alerted'] = True
            if p >= 100:
                finalize_test_task(task, '预警' if task.get('temp_alerted') or task.get('latency_alerted') else '通过')
                return
            persist_test_workspace_state()

def run_fio_stress_task(task_id):
    with LOCK:
        task = next((x for x in STATE['tasks'] if x['id'] == task_id))
        if task['status'] != '运行中':
            return
        runtime = task['duration'] * 3600
        cmd = build_fio_command(task, runtime)
        append_task_event(task, '信息', '已启动 fio 裸盘测试：{0}（该设备上的数据将被覆盖）'.format(task['path']))
        persist_test_workspace_state()
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        PROCESSES[task_id] = process
    except OSError as exc:
        with LOCK:
            task['status'] = '失败'
            task['result'] = '执行器错误'
            task['ended_at'] = beijing_time_string()
            append_task_event(task, '严重', str(exc))
            start_next_queued_task()
            persist_test_workspace_state()
        return
    started = time.time()
    while process.poll() is None:
        time.sleep(15)
        with LOCK:
            if task['status'] != '运行中':
                process.terminate()
                break
            task['elapsed'] = int(time.time() - started)
            task['progress'] = min(99.9, round(task['elapsed'] / runtime * 100, 1))
            stage_state = stage_progress(task.get('stages') or [], task['elapsed'])
            task['stage_progress'] = stage_state['stage_progress']
            stage = active_stage(task.get('stages') or [], task['elapsed'])
            if stage:
                task['active_stage'] = stage['name']
            s = collect_task_health_sample(task)
            append_task_sample(task, s)
            try:
                if float(s['temperature']) >= task['threshold_temp'] and (not task.get('temp_alerted')):
                    append_task_event(task, '严重', '温度 {0}°C 达到阈值'.format(s['temperature']))
                    task['temp_alerted'] = True
            except (ValueError, TypeError):
                pass
            persist_test_workspace_state()
    stdout, stderr = process.communicate()
    PROCESSES.pop(task_id, None)
    with LOCK:
        if task['status'] == '停止中':
            task['status'] = '已停止'
            task['ended_at'] = beijing_time_string()
            task['result'] = '人工终止'
            append_task_event(task, '警告', 'fio 进程已停止')
            start_next_queued_task()
            persist_test_workspace_state()
            return
        if task['status'] == '已停止':
            return
        if process.returncode:
            task['status'] = '失败'
            task['result'] = 'fio 执行失败'
            task['ended_at'] = beijing_time_string()
            append_task_event(task, '严重', (stderr or 'fio 返回异常')[-300:])
            start_next_queued_task()
            persist_test_workspace_state()
            return
        try:
            metrics = parse_fio_json(stdout)
            append_task_sample(task, {'time': beijing_time_string(), 'temperature': '--', 'p99': metrics['p99_ms'], 'throughput': metrics['throughput_mbps'], 'health': '--'})
            final_stage = active_stage(task.get('stages') or [], runtime)
            if final_stage:
                task.setdefault('stage_results', []).append(summarize_stage_result(final_stage, metrics, runtime))
        except (ValueError, KeyError, IndexError, TypeError):
            append_task_event(task, '警告', 'fio 已完成，但无法解析部分性能摘要')
        task['progress'] = 100
        finalize_test_task(task, '预警' if task.get('temp_alerted') else '通过')

class Handler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'static'), **kwargs)

    def log_message(self, *args):
        pass

    def send_json(self, body, status=200):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')

    def state(self):
        discovered_devices = discover_linux_devices() if running_on_linux() else build_demo_ssd_inventory()
        devices = [enrich_device(device) for device in discovered_devices]
        is_root = getattr(os, 'geteuid', lambda: 1)() == 0
        diagnostics = collect_environment_health(destructive_stress_allowed(), is_root)
        alerts = build_alerts(STATE['tasks'])
        try:
            persisted_alerts = STORE.list_alert_records(limit=100)
            notifications = STORE.list_notifications(limit=100)
        except sqlite3.Error:
            persisted_alerts = []
            notifications = []
        return {'devices': devices, 'inventory': summarize_inventory(devices), 'plans': enabled_strategies(STATE['plans']), 'all_plans': STATE['plans'], 'alert_policies': [policy_summary(policy) for policy in STATE['alert_policies']], 'tasks': STATE['tasks'], 'environment': {'linux': running_on_linux(), 'fio': bool(shutil.which('fio')), 'destructive_enabled': destructive_stress_allowed(), 'root': is_root}, 'diagnostics': diagnostics, 'scheduler': summarize_queue(STATE['tasks']), 'alerts': alerts, 'alert_summary': alert_summary(alerts), 'persisted_alerts': persisted_alerts, 'notification_summary': {'total': len(notifications), 'pending': sum(1 for item in notifications if item.get('status') == '待发送'), 'failed': sum(1 for item in notifications if item.get('status') == '发送失败')}, 'telemetry_summaries': {task['id']: telemetry_summary(task.get('samples') or []) for task in STATE['tasks']}}

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == '/api/state':
            with LOCK:
                return self.send_json(self.state())
        if path == '/api/alerts':
            with LOCK:
                alerts = build_alerts(STATE['tasks'])
                return self.send_json({'alerts': alerts, 'summary': alert_summary(alerts), 'records': STORE.list_alert_records()})
        if path == '/api/notifications':
            return self.send_json({'notifications': STORE.list_notifications()})
        if path.startswith('/api/tasks/') and path.endswith('/telemetry'):
            task_id = path.removeprefix('/api/tasks/').removesuffix('/telemetry').strip('/')
            query = parse_qs(parsed_url.query)
            try:
                with LOCK:
                    task = next((item for item in STATE['tasks'] if item.get('id') == task_id), None)
                    if not task:
                        return self.send_json({'error': '任务不存在'}, 404)
                    if not TIME_SERIES.count_samples(task_id):
                        for sample in STORE.task_metric_samples(task_id):
                            try:
                                record = dict(sample)
                                if record.get('time'):
                                    record['timestamp'] = datetime.strptime(str(record['time']), '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING_TZ).isoformat()
                                TIME_SERIES.write_samples(task_id, record, record.get('stage_name'))
                            except (TypeError, ValueError):
                                continue
                    max_points = query.get('max_points', ['240'])[-1]
                    samples = TIME_SERIES.query_downsampled(task_id, max_points, query.get('metric', [None])[-1], query.get('start', [None])[-1], query.get('end', [None])[-1], query.get('stage', []), query.get('method', ['minmax'])[-1])
                    quality = TIME_SERIES.quality_summary(task_id, query.get('expected_interval_seconds', [None])[-1], query.get('start', [None])[-1], query.get('end', [None])[-1])
                    return self.send_json({'task_id': task_id, 'series': TIME_SERIES.series_summary(task_id), 'samples': samples, 'quality': quality})
            except (sqlite3.Error, ValueError) as exc:
                return self.send_json({'error': '遥测查询失败：{0}'.format(exc)}, 400)
        if path.startswith('/api/tasks/') and path.endswith('/report-evidence'):
            task_id = path.removeprefix('/api/tasks/').removesuffix('/report-evidence').strip('/')
            with LOCK:
                task = next((item for item in STATE['tasks'] if item.get('id') == task_id), None)
                if task:
                    analysis = analyze_task(task, STATE['tasks'])
                else:
                    record = next((item for item in STORE.result_records() if item.get('task_id') == task_id), None)
                    if not record:
                        return self.send_json({'error': '任务不存在'}, 404)
                    task = record['result_json']
                    analysis = record['analysis_json']
                return self.send_json(build_task_report_evidence(task, analysis))
        if path == '/api/tasks/export.csv':
            with LOCK:
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['任务编号', '设备型号', '序列号', '设备路径', '测试策略', '测试模式', '任务状态', '测试结论', '稳定性评分', '风险等级', '开始时间（北京时间）', '结束时间（北京时间）', '进度（%）', '温度阈值（°C）', '事件数'])
                for task in STATE['tasks']:
                    analysis = analyze_task(task, STATE['tasks'])
                    writer.writerow([task.get('id'), task.get('device'), task.get('serial'), task.get('path'), task.get('plan'), task.get('mode'), task.get('status'), analysis.get('conclusion'), analysis.get('score'), analysis.get('risk_level'), task.get('started_at') or '未开始', task.get('ended_at') or '--', task.get('progress', 0), task.get('threshold_temp', '--'), len(task.get('events') or [])])
                raw = ('\ufeff' + output.getvalue()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="ssd-task-summary.csv"')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path == '/api/results':
            query = parse_qs(parsed_url.query)
            filters = {key: values[-1] for key, values in query.items() if key in ('device', 'serial', 'path', 'plan', 'mode', 'status', 'conclusion', 'risk_level', 'search', 'score_min', 'score_max', 'started_after', 'started_before')}
            records = STORE.result_records()
            page = query_results(records, filters, query.get('page', ['1'])[-1], query.get('page_size', ['20'])[-1], query.get('sort_by', ['started_at'])[-1], query.get('descending', ['true'])[-1].lower() != 'false')
            return self.send_json({'results': page, 'facets': build_filter_facets(records)})
        if path == '/api/results/history':
            query = parse_qs(parsed_url.query)
            return self.send_json(build_history_trend(STORE.result_records(), query.get('group_by', ['device'])[-1]))
        if path == '/api/batches':
            return self.send_json({'batches': STORE.list_batches()})
        if path == '/api/audit-events':
            return self.send_json({'events': STORE.recent_audit_events()})
        if path == '/api/alert-policies':
            return self.send_json({'policies': STATE['alert_policies']})
        if path == '/api/database/health':
            return self.send_json({'database': database_health(RESULTS_DB_FILE), 'backups': list_backups(BACKUP_ROOT)})
        if path == '/api/assets':
            return self.send_json({'assets': STORE.list_assets()})
        if path.startswith('/api/assets/') and path.endswith('/snapshots'):
            asset_id = path.removeprefix('/api/assets/').removesuffix('/snapshots').strip('/')
            return self.send_json({'asset_id': asset_id, 'snapshots': STORE.asset_snapshots(asset_id)})
        if path.startswith('/api/assets/') and path.endswith('/history'):
            asset_id = path.removeprefix('/api/assets/').removesuffix('/history').strip('/')
            return self.send_json(summarize_device_history(STORE.asset_snapshots(asset_id)))
        if path.startswith('/api/results/') and path.endswith('/report'):
            task_id = path.removeprefix('/api/results/').removesuffix('/report').strip('/')
            report_html = STORE.report_snapshot(task_id)
            if report_html is None:
                return self.send_json({'error': '数据库中未找到该任务报告'}, 404)
            raw = report_html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="enterprise-ssd-report-{0}.html"'.format(task_id))
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path.startswith('/api/evidence/'):
            task_id = path.removeprefix('/api/evidence/').strip('/')
            task = next((item for item in STATE['tasks'] if item.get('id') == task_id), None)
            if task:
                analysis = analyze_task(task, STATE['tasks'])
                enrichment = build_task_report_evidence(task, analysis)
                report_html = build_report(task, analysis, beijing_time_string(), enrichment)
            else:
                record = next((item for item in STORE.result_records() if item.get('task_id') == task_id), None)
                if not record:
                    return self.send_json({'error': '未找到任务证据'}, 404)
                task = record['result_json']
                analysis = record['analysis_json']
                enrichment = build_task_report_evidence(task, analysis)
                report_html = STORE.report_snapshot(task_id).encode('utf-8')
            payloads = {'report.html': report_html, 'task.json': json.dumps(task, ensure_ascii=False, indent=2).encode('utf-8'), 'analysis.json': json.dumps(analysis, ensure_ascii=False, indent=2).encode('utf-8'), 'report-evidence.json': json.dumps(enrichment, ensure_ascii=False, indent=2).encode('utf-8'), 'events.json': json.dumps(task.get('events') or [], ensure_ascii=False, indent=2).encode('utf-8')}
            target = EVIDENCE_ROOT / '{0}.zip'.format(task_id)
            build_evidence_bundle(target, payloads, {'task_id': task_id, 'device': task.get('device'), 'generated_at': beijing_time_string()})
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="ssd-evidence-{0}.zip"'.format(task_id))
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path.startswith('/api/logs/'):
            try:
                file = (LOG_ROOT / unquote(path.removeprefix('/api/logs/'))).resolve()
                if LOG_ROOT.resolve() not in file.parents or not file.is_file():
                    raise FileNotFoundError
                raw = file.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', 'attachment; filename="{0}"'.format(file.name))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            except FileNotFoundError:
                return self.send_json({'error': '日志文件不存在'}, 404)
        if path.startswith('/api/log-archives/'):
            try:
                folder = (LOG_ROOT / unquote(path.removeprefix('/api/log-archives/'))).resolve()
                if LOG_ROOT.resolve() not in folder.parents or not folder.is_dir():
                    raise FileNotFoundError
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for file in folder.iterdir():
                        if file.is_file():
                            archive.write(file, file.name)
                raw = buffer.getvalue()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', 'attachment; filename="{0}.zip"'.format(folder.name))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            except FileNotFoundError:
                return self.send_json({'error': '日志目录不存在'}, 404)
        if path.startswith('/api/report/'):
            with LOCK:
                task = next((x for x in STATE['tasks'] if x['id'] == path.rsplit('/', 1)[-1]), None)
                if not task:
                    return self.send_json({'error': '任务不存在'}, 404)
                analysis = analyze_task(task, STATE['tasks'])
                page = build_report(task, analysis, beijing_time_string(), build_task_report_evidence(task, analysis))
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="enterprise-ssd-report.html"')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            return self.wfile.write(page)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
            with LOCK:
                if path == '/api/batches':
                    task_ids = data.get('task_ids') or []
                    selected_tasks = [task for task in STATE['tasks'] if task.get('id') in task_ids]
                    if len(task_ids) < 2 or len(selected_tasks) != len(task_ids):
                        return self.send_json({'error': '批次至少需要两条有效任务'}, 400)
                    if any(task.get('batch_id') for task in selected_tasks):
                        return self.send_json({'error': '所选任务中存在已归属其他批次的任务'}, 409)
                    batch_id = 'batch-' + uuid.uuid4().hex[:8]
                    name = str(data.get('name') or 'SSD 批次 {0}'.format(beijing_timestamp_token())).strip()[:80]
                    for task in selected_tasks:
                        task['batch_id'] = batch_id
                        append_task_event(task, '信息', '已加入批次“{0}”'.format(name))
                    STORE.create_batch(batch_id, name, beijing_time_string(), {'priority': data.get('priority', '普通'), 'created_by': 'web'}, task_ids)
                    persist_test_workspace_state()
                    return self.send_json({'batch_id': batch_id, 'name': name, 'task_count': len(selected_tasks)}, 201)
                if path == '/api/results/compare':
                    task_ids = data.get('task_ids') or []
                    records = [record for record in STORE.result_records() if record.get('task_id') in task_ids]
                    return self.send_json(compare_results(records))
                if path == '/api/assets/scan':
                    raw_devices = discover_linux_devices() if running_on_linux() else build_demo_ssd_inventory()
                    assets = [enrich_device(device) for device in raw_devices]
                    captured_at = beijing_time_string()
                    for asset in assets:
                        STORE.upsert_asset(asset, captured_at)
                        STORE.append_asset_snapshot(asset, captured_at)
                    STORE.record_audit(captured_at, 'asset_scan', '信息', {'asset_count': len(assets)})
                    return self.send_json({'assets': assets, 'captured_at': captured_at}, 201)
                if path == '/api/database/backup':
                    backup = backup_database(RESULTS_DB_FILE, BACKUP_ROOT, beijing_timestamp_token())
                    keep_count = int(data.get('keep_count', 10))
                    removed = prune_backups(BACKUP_ROOT, max(1, min(100, keep_count)))
                    STORE.record_audit(beijing_time_string(), 'database_backup', '信息', {'backup': backup.name, 'removed': removed})
                    return self.send_json({'backup': backup.name, 'removed': removed, 'health': database_health(RESULTS_DB_FILE)}, 201)
                if path.startswith('/api/notifications/') and path.endswith('/mark'):
                    notification_id = path.removeprefix('/api/notifications/').removesuffix('/mark').strip('/')
                    status = data.get('status')
                    processed = beijing_time_string()
                    if not STORE.mark_notification(notification_id, status, processed, data.get('error_text', '')):
                        return self.send_json({'error': '通知记录不存在'}, 404)
                    STORE.record_audit(processed, 'notification_marked', '信息', {'notification_id': notification_id, 'status': status})
                    return self.send_json({'id': notification_id, 'status': status, 'processed_at': processed})
                if path == '/api/strategies':
                    strategy = normalize_strategy(data, BLOCK_SIZES)
                    if any(plan['id'] == strategy['id'] for plan in STATE['plans']):
                        return self.send_json({'error': '策略编号已存在'}, 409)
                    STATE['plans'].append(strategy)
                    persist_test_workspace_state()
                    return self.send_json(strategy, 201)
                if path == '/api/alert-policies':
                    policy = normalize_alert_policy(data)
                    if any(item['id'] == policy['id'] for item in STATE['alert_policies']):
                        return self.send_json({'error': '告警策略编号已存在'}, 409)
                    STATE['alert_policies'].append(policy)
                    persist_test_workspace_state()
                    return self.send_json(policy, 201)
                if path.startswith('/api/alert-policies/') and path.endswith('/version'):
                    policy_id = path.removeprefix('/api/alert-policies/').removesuffix('/version').strip('/')
                    policy = next((item for item in STATE['alert_policies'] if item['id'] == policy_id), None)
                    if not policy:
                        return self.send_json({'error': '告警策略不存在'}, 404)
                    updated = create_policy_version(policy, data.get('changes'), beijing_time_string(), data.get('reason', '策略调整'))
                    STATE['alert_policies'][STATE['alert_policies'].index(policy)] = updated
                    persist_test_workspace_state()
                    return self.send_json(updated)
                if path.startswith('/api/strategies/') and path.endswith('/toggle'):
                    strategy_id = path.removeprefix('/api/strategies/').removesuffix('/toggle').strip('/')
                    strategy = next((plan for plan in STATE['plans'] if plan['id'] == strategy_id), None)
                    if not strategy:
                        return self.send_json({'error': '策略不存在'}, 404)
                    strategy['enabled'] = not strategy.get('enabled', True)
                    strategy['version'] = strategy.get('version', 1) + 1
                    persist_test_workspace_state()
                    return self.send_json(strategy)
                if path.startswith('/api/alerts/') and path.endswith('/acknowledge'):
                    alert_id = path.removeprefix('/api/alerts/').removesuffix('/acknowledge').strip('/')
                    task_id = alert_id.split(':', 1)[0]
                    task = next((item for item in STATE['tasks'] if item.get('id') == task_id), None)
                    if not task:
                        return self.send_json({'error': '告警所属任务不存在'}, 404)
                    result = acknowledge_alert(task, alert_id)
                    STORE.acknowledge_alert_record(result['alert_id'], beijing_time_string())
                    append_task_event(task, '信息', '操作员已确认告警 {0}'.format(alert_id))
                    persist_test_workspace_state()
                    return self.send_json(result)
                if path == '/api/tasks':
                    raw_devices = discover_linux_devices() if running_on_linux() else build_demo_ssd_inventory()
                    devices = [enrich_device(device) for device in raw_devices]
                    device = next((x for x in devices if x['id'] == data.get('device_id')), None)
                    plan = next((x for x in enabled_strategies(STATE['plans']) if x['id'] == data.get('plan_id')), None)
                    mode = data.get('mode', 'demo')
                    if not device or not plan:
                        return self.send_json({'error': '请选择有效设备和策略'}, 400)
                    if not device.get('testable', False):
                        return self.send_json({'error': '该 SSD 不满足测试准入条件：' + '；'.join(device.get('test_reasons', ['设备状态未知']))}, 403)
                    if mode == 'real':
                        if not (running_on_linux() and shutil.which('fio') and destructive_stress_allowed() and (getattr(os, 'geteuid', lambda: 1)() == 0)):
                            return self.send_json({'error': '真实压测要求 Linux、root、fio 与 ENABLE_DESTRUCTIVE_FIO=1'}, 403)
                        if not data.get('confirmed_device'):
                            return self.send_json({'error': '请确认当前选择的是专用测试 SSD，且允许覆盖其数据'}, 403)
                    selected_policy_ids = data.get('alert_policy_ids') or []
                    if isinstance(selected_policy_ids, str):
                        selected_policy_ids = [selected_policy_ids]
                    if not isinstance(selected_policy_ids, list):
                        return self.send_json({'error': '告警策略必须是编号列表'}, 400)
                    selected_policy_ids = [str(policy_id).strip() for policy_id in selected_policy_ids if str(policy_id).strip()]
                    known_policy_ids = {policy['id'] for policy in STATE['alert_policies'] if policy.get('enabled', True)}
                    if any(policy_id not in known_policy_ids for policy_id in selected_policy_ids):
                        return self.send_json({'error': '选择了不存在或已停用的告警策略'}, 400)
                    config = resolve_test_config(plan, data.get('config'))
                    stages = build_test_stages(plan['id'], config)
                    execution_envelope = estimate_test_envelope(config)
                    customized = any((config[key] != plan[key] for key in ('duration', 'block_size', 'read_ratio', 'queue_depth', 'threshold_temp'))) or any((config[key] != default for key, default in {'io_pattern': 'randrw', 'num_jobs': 1, 'ramp_time': 0, 'rate_limit': 0, 'verify': 'none', 'extra_options': {}}.items()))
                    has_active_task = any(task['status'] in ('运行中', '停止中') for task in STATE['tasks'])
                    has_queued_task = any(task['status'] == '排队中' for task in STATE['tasks'])
                    busy = has_active_task or has_queued_task
                    queue_sequence = max([item.get('queue_sequence', 0) for item in STATE['tasks']] or [0]) + 1
                    priority = normalize_priority(data.get('priority', '普通'))
                    task = {'id': uuid.uuid4().hex[:8], 'name': '{0} · {1}'.format(device['name'], plan['name']), 'device': device['name'], 'serial': device['serial'], 'asset_id': device['asset_id'], 'path': device['path'], 'transport': device['interface'], 'plan': plan['name'] + ('（自定义参数）' if customized else ''), 'strategy_snapshot': strategy_snapshot(plan), 'alert_policy_ids': selected_policy_ids, 'duration': config['duration'], 'block_size': config['block_size'], 'read_ratio': config['read_ratio'], 'queue_depth': config['queue_depth'], 'threshold_temp': config['threshold_temp'], 'io_pattern': config['io_pattern'], 'num_jobs': config['num_jobs'], 'ramp_time': config['ramp_time'], 'rate_limit': config['rate_limit'], 'verify': config['verify'], 'extra_options': config['extra_options'], 'stages': stages, 'active_stage': stages[0]['name'], 'execution_envelope': execution_envelope, 'priority': priority, 'queue_sequence': queue_sequence, 'created_at': beijing_time_string(), 'mode': '真实 fio 裸盘' if mode == 'real' else '安全演示', 'status': '排队中' if busy else '运行中', 'result': '--', 'started_at': None, 'ended_at': None, 'elapsed': 0, 'progress': 0, 'samples': [], 'events': [], 'queued': busy}
                    append_task_event(task, '信息', '已有测试任务正在运行，任务已进入全局队列' if busy else '任务已创建')
                    STATE['tasks'].insert(0, task)
                    if not has_active_task:
                        start_next_queued_task()
                    persist_test_workspace_state()
                    return self.send_json(task, 201)
                if path.startswith('/api/devices/') and path.endswith('/logs'):
                    device_id = path.split('/')[3]
                    devices = discover_linux_devices() if running_on_linux() else build_demo_ssd_inventory()
                    device = next((x for x in devices if x['id'] == device_id), None)
                    if not device:
                        return self.send_json({'error': '设备不存在'}, 404)
                    return self.send_json(collect_nvme_logs(device), 201)
                if path.startswith('/api/tasks/') and path.endswith('/stop'):
                    task = next((x for x in STATE['tasks'] if x['id'] == path.split('/')[3]), None)
                    if not task:
                        return self.send_json({'error': '任务不存在'}, 404)
                    if task['status'] == '排队中':
                        task['status'] = '已停止'
                        task['ended_at'] = beijing_time_string()
                        task['result'] = '已取消'
                        append_task_event(task, '信息', '排队任务已取消')
                        persist_test_workspace_state()
                    elif task['status'] == '运行中':
                        process = PROCESSES.get(task['id'])
                        if task['mode'] == '真实 fio 裸盘' and process:
                            task['status'] = '停止中'
                            append_task_event(task, '警告', '正在终止 fio 进程，设备释放后将继续下一条任务')
                            process.terminate()
                            persist_test_workspace_state()
                        else:
                            task['status'] = '已停止'
                            task['ended_at'] = beijing_time_string()
                            task['result'] = '人工终止'
                            append_task_event(task, '警告', '任务由操作员停止')
                            start_next_queued_task()
                            persist_test_workspace_state()
                    return self.send_json(task)
                if path.startswith('/api/tasks/') and path.endswith('/priority'):
                    task = next((item for item in STATE['tasks'] if item['id'] == path.split('/')[3]), None)
                    if not task:
                        return self.send_json({'error': '任务不存在'}, 404)
                    if task.get('status') != '排队中':
                        return self.send_json({'error': '仅排队中的任务可以调整优先级'}, 409)
                    task['priority'] = normalize_priority(data.get('priority'))
                    append_task_event(task, '信息', '任务优先级调整为“{0}”'.format(task['priority']))
                    persist_test_workspace_state()
                    return self.send_json({'task': task, 'queue_position': queue_position(STATE['tasks'], task['id'])})
            return self.send_json({'error': '接口不存在'}, 404)
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            return self.send_json({'error': '请求处理失败：{0}'.format(exc)}, 400)
if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    print('SSD PressureTest: http://127.0.0.1:{0}'.format(port))
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
