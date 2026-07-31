from common import display_text as _text, number_value


REPORTABLE_ALERT_SEVERITIES = ('警告', '严重')
ACTIVE_TASK_STATUSES = ('运行中', '停止中', '排队中')
TERMINAL_TASK_STATUSES = ('已完成', '已停止', '已中断', '失败')
NUMERIC_SAMPLE_FIELDS = (
    ('temperature', '温度'),
    ('p99', 'P99 延迟'),
    ('throughput', '吞吐'),
    ('health', '介质健康度'),
)

def _round(value, digits=2):
    return round(value, digits) if value is not None else None


def _percentage(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / float(denominator) * 100, 2)


def _sample_value_state(sample, field):
    value = sample.get(field)
    if value in (None, '', '--'):
        return 'missing'
    if number_value(value) is None:
        return 'invalid'
    return 'valid'


def _stage_result_lookup(stage_results):
    by_ordinal = {}
    by_name = {}
    for result in stage_results:
        if not isinstance(result, dict):
            continue
        ordinal = result.get('ordinal')
        name = result.get('stage') or result.get('name')
        if ordinal is not None and ordinal not in by_ordinal:
            by_ordinal[ordinal] = result
        if name and name not in by_name:
            by_name[name] = result
    return by_ordinal, by_name


def stage_result_overview(task):
    stages = task.get('stages') or []
    stage_results = task.get('stage_results') or []
    by_ordinal, by_name = _stage_result_lookup(stage_results)
    task_status = _text(task.get('status'), '未知')
    active_stage = task.get('active_stage')
    items = []

    # 阶段执行器早期版本只落最后一个 fio 摘要，报告必须把计划阶段保留下来，不能把缺失记录误报为未执行。
    for position, stage in enumerate(stages, 1):
        if not isinstance(stage, dict):
            continue
        ordinal = stage.get('ordinal', position)
        name = _text(stage.get('name'), '阶段 {0}'.format(ordinal))
        result = by_ordinal.get(ordinal) or by_name.get(name)
        status = '未开始'
        evidence_source = '测试计划'
        if result and result.get('completed'):
            status = '已完成'
            evidence_source = '阶段执行摘要'
        elif result:
            status = '已结束（摘要未确认完成）'
            evidence_source = '阶段执行摘要'
        elif task_status in ACTIVE_TASK_STATUSES and active_stage == name:
            status = '执行中'
        elif task_status in TERMINAL_TASK_STATUSES and task_status == '已完成':
            status = '已完成（任务状态推断）'
            evidence_source = '任务终态推断'
        elif task_status in TERMINAL_TASK_STATUSES:
            status = '未完成'
        metrics = result.get('metrics') if isinstance(result, dict) and isinstance(result.get('metrics'), dict) else {}
        items.append({
            'ordinal': ordinal,
            'name': name,
            'purpose': _text(stage.get('purpose'), '未记录阶段目的'),
            'planned_seconds': stage.get('duration_seconds'),
            'planned_minutes': stage.get('duration_minutes'),
            'reported_elapsed_seconds': result.get('elapsed_seconds') if isinstance(result, dict) else None,
            'status': status,
            'completed': status == '已完成' or status == '已完成（任务状态推断）',
            'evidence_source': evidence_source,
            'metrics': {
                'throughput_mbps': metrics.get('throughput_mbps'),
                'p99_ms': metrics.get('p99_ms'),
                'read_iops': metrics.get('read_iops'),
                'write_iops': metrics.get('write_iops'),
                'job_count': metrics.get('job_count'),
            },
        })

    if not items:
        for position, result in enumerate(stage_results, 1):
            if not isinstance(result, dict):
                continue
            metrics = result.get('metrics') if isinstance(result.get('metrics'), dict) else {}
            items.append({
                'ordinal': result.get('ordinal', position),
                'name': _text(result.get('stage') or result.get('name'), '阶段 {0}'.format(position)),
                'purpose': '执行计划未保存，依据执行摘要恢复',
                'planned_seconds': result.get('planned_seconds'),
                'planned_minutes': None,
                'reported_elapsed_seconds': result.get('elapsed_seconds'),
                'status': '已完成' if result.get('completed') else '已结束（摘要未确认完成）',
                'completed': bool(result.get('completed')),
                'evidence_source': '阶段执行摘要',
                'metrics': {
                    'throughput_mbps': metrics.get('throughput_mbps'),
                    'p99_ms': metrics.get('p99_ms'),
                    'read_iops': metrics.get('read_iops'),
                    'write_iops': metrics.get('write_iops'),
                    'job_count': metrics.get('job_count'),
                },
            })

    completed = sum(1 for item in items if item['completed'])
    executing = sum(1 for item in items if item['status'] == '执行中')
    recorded = sum(1 for item in items if item['evidence_source'] == '阶段执行摘要')
    planned_seconds = sum(item['planned_seconds'] or 0 for item in items)
    if not items:
        assessment = '未记录阶段计划或阶段执行摘要'
    elif task_status == '已完成' and completed == len(items):
        assessment = '全部计划阶段已完成'
    elif executing:
        assessment = '当前处于“{0}”阶段'.format(_text(active_stage, '执行中'))
    elif task_status in TERMINAL_TASK_STATUSES:
        assessment = '任务已结束，阶段证据存在缺口'
    else:
        assessment = '阶段计划已建立，等待执行证据'
    return {
        'available': bool(items),
        'task_status': task_status,
        'active_stage': _text(active_stage, '无'),
        'planned_count': len(items),
        'recorded_count': recorded,
        'completed_count': completed,
        'executing_count': executing,
        'stage_record_coverage_pct': _percentage(recorded, len(items)),
        'planned_total_seconds': planned_seconds,
        'assessment': assessment,
        'stages': items,
    }


def assess_telemetry_data_quality(task, analysis=None):
    samples = task.get('samples') or []
    stage_plan_exists = bool(task.get('stages'))
    fields = {}
    valid_values = 0
    possible_values = len(samples) * len(NUMERIC_SAMPLE_FIELDS)
    for field, label in NUMERIC_SAMPLE_FIELDS:
        valid = 0
        missing = 0
        invalid = 0
        for sample in samples:
            if not isinstance(sample, dict):
                invalid += 1
                continue
            state = _sample_value_state(sample, field)
            if state == 'valid':
                valid += 1
            elif state == 'missing':
                missing += 1
            else:
                invalid += 1
        valid_values += valid
        fields[field] = {
            'label': label,
            'valid_count': valid,
            'missing_count': missing,
            'invalid_count': invalid,
            'coverage_pct': _percentage(valid, len(samples)),
        }

    timestamps = []
    timed_samples = 0
    staged_samples = 0
    usable_samples = 0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        if sample.get('time') not in (None, '', '--'):
            timed_samples += 1
            timestamps.append(str(sample['time']))
        if sample.get('stage_name') not in (None, '', '--'):
            staged_samples += 1
        if any(_sample_value_state(sample, field) == 'valid' for field, _ in NUMERIC_SAMPLE_FIELDS):
            usable_samples += 1
    duplicate_timestamps = len(timestamps) - len(set(timestamps))
    metric_coverage = _percentage(valid_values, possible_values) if possible_values else 0
    timestamp_coverage = _percentage(timed_samples, len(samples)) if samples else 0
    stage_coverage = _percentage(staged_samples, len(samples)) if samples and stage_plan_exists else None
    score = metric_coverage * 0.55 + timestamp_coverage * 0.30
    if stage_plan_exists:
        score += (stage_coverage or 0) * 0.15
    else:
        score += 15
    score = round(score, 2)
    if not samples:
        level = '不足'
        assessment = '未采集遥测样本，报告只能展示任务配置和执行状态'
    elif score >= 80 and duplicate_timestamps == 0:
        level = '充分'
        assessment = '遥测字段与时间戳覆盖满足当前报告分析条件'
    elif score >= 55:
        level = '受限'
        assessment = '遥测可用于部分分析，但存在字段、时间戳或阶段归属缺口'
    else:
        level = '不足'
        assessment = '遥测覆盖不足，结论应以执行状态和有限指标为准'
    analysis_limitations = []
    if isinstance(analysis, dict):
        analysis_limitations = list(analysis.get('limitations') or [])
    return {
        'available': bool(samples),
        'sample_count': len(samples),
        'usable_sample_count': usable_samples,
        'metric_coverage_pct': metric_coverage,
        'timestamp_coverage_pct': timestamp_coverage,
        'stage_attribution_required': stage_plan_exists,
        'stage_attribution_coverage_pct': stage_coverage,
        'duplicate_timestamp_count': duplicate_timestamps,
        'score': score,
        'level': level,
        'assessment': assessment,
        'fields': fields,
        'analysis_limitations': analysis_limitations,
    }


def _history_source(asset_history, task):
    if asset_history is None:
        asset_history = task.get('asset_history') or []
    if isinstance(asset_history, dict):
        for key in ('snapshots', 'points', 'items'):
            if isinstance(asset_history.get(key), list):
                return asset_history.get(key), asset_history
        trend = asset_history.get('health_trend')
        if isinstance(trend, dict) and isinstance(trend.get('points'), list):
            return trend.get('points'), asset_history
        return [], asset_history
    return asset_history if isinstance(asset_history, list) else [], {}


def _snapshot_field(snapshot, field):
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get(field) not in (None, '', '--'):
        return snapshot.get(field)
    payload = snapshot.get('payload')
    if isinstance(payload, dict):
        return payload.get(field)
    return None


def _history_snapshot(snapshot):
    return {
        'captured_at': _text(_snapshot_field(snapshot, 'captured_at')),
        'asset_id': _text(_snapshot_field(snapshot, 'asset_id')),
        'serial': _text(_snapshot_field(snapshot, 'serial')),
        'path': _text(_snapshot_field(snapshot, 'path')),
        'firmware': _text(_snapshot_field(snapshot, 'firmware') or _snapshot_field(snapshot, 'firmware_version') or _snapshot_field(snapshot, 'fwrev') or _snapshot_field(snapshot, 'revision')),
        'interface': _text(_snapshot_field(snapshot, 'interface') or _snapshot_field(snapshot, 'transport')),
        'health': number_value(_snapshot_field(snapshot, 'health')),
        'temperature': number_value(_snapshot_field(snapshot, 'temperature')),
        'capacity': _text(_snapshot_field(snapshot, 'capacity')),
    }


def _trend(start, end):
    if start is None or end is None:
        return '未知'
    if end < start:
        return '下降'
    if end > start:
        return '上升'
    return '稳定'


def asset_history_evidence(task, asset_history=None):
    source, source_summary = _history_source(asset_history, task)
    snapshots = [_history_snapshot(item) for item in source if isinstance(item, dict)]
    snapshots.sort(key=lambda item: (item['captured_at'] == '--', item['captured_at']))
    if not snapshots:
        known_asset_id = source_summary.get('asset_id') if isinstance(source_summary, dict) else None
        return {
            'available': False,
            'asset_id': _text(known_asset_id or task.get('asset_id')),
            'snapshot_count': 0,
            'assessment': '未提供可用资产历史快照，无法判断跨任务硬件变化',
            'changes': [],
            'health': {'available': False, 'trend': '未知'},
            'temperature': {'available': False},
        }
    first = snapshots[0]
    latest = snapshots[-1]
    health_values = [item['health'] for item in snapshots if item['health'] is not None]
    temperature_values = [item['temperature'] for item in snapshots if item['temperature'] is not None]
    firmware_values = [item['firmware'] for item in snapshots if item['firmware'] != '--']
    interface_values = [item['interface'] for item in snapshots if item['interface'] != '--']
    capacity_values = [item['capacity'] for item in snapshots if item['capacity'] != '--']
    changes = []
    for field, label, values in (
        ('firmware', '固件版本', firmware_values),
        ('interface', '连接接口', interface_values),
        ('capacity', '标称容量', capacity_values),
    ):
        if len(set(values)) > 1:
            changes.append({
                'field': field,
                'label': label,
                'previous': first[field],
                'current': latest[field],
            })
    health = {
        'available': bool(health_values),
        'first': _round(health_values[0]) if health_values else None,
        'latest': _round(health_values[-1]) if health_values else None,
        'minimum': _round(min(health_values)) if health_values else None,
        'delta': _round(health_values[-1] - health_values[0]) if len(health_values) >= 2 else None,
        'trend': _trend(health_values[0], health_values[-1]) if health_values else '未知',
    }
    temperature = {
        'available': bool(temperature_values),
        'latest': _round(temperature_values[-1]) if temperature_values else None,
        'maximum': _round(max(temperature_values)) if temperature_values else None,
        'average': _round(sum(temperature_values) / float(len(temperature_values))) if temperature_values else None,
    }
    if changes:
        assessment = '资产历史发现 {0} 项硬件属性变化，报告应结合变化前后数据解释'.format(len(changes))
    elif len(snapshots) == 1:
        assessment = '仅有 1 条资产快照，尚未形成跨时段趋势'
    else:
        assessment = '资产历史未发现固件、接口或容量变化'
    return {
        'available': True,
        'asset_id': latest['asset_id'] if latest['asset_id'] != '--' else _text(source_summary.get('asset_id') or task.get('asset_id')),
        'snapshot_count': len(snapshots),
        'first_captured_at': first['captured_at'],
        'latest_captured_at': latest['captured_at'],
        'latest_firmware': latest['firmware'],
        'latest_interface': latest['interface'],
        'health': health,
        'temperature': temperature,
        'changes': changes,
        'assessment': assessment,
    }


def _normalized_severity(value):
    mapping = {'warning': '警告', 'warn': '警告', 'critical': '严重', 'error': '严重'}
    return mapping.get(str(value).lower(), value)


def _derived_alerts(task):
    task_id = _text(task.get('id'), '')
    acknowledged = task.get('acknowledged_alerts') or []
    alerts = []
    for index, event in enumerate(task.get('events') or []):
        if not isinstance(event, dict):
            continue
        severity = _normalized_severity(event.get('severity'))
        if severity not in REPORTABLE_ALERT_SEVERITIES:
            continue
        legacy_id = '{0}:{1}'.format(task_id, index)
        alert_id = event.get('id') or legacy_id
        alerts.append({
            'id': alert_id,
            'task_id': task_id,
            'severity': severity,
            'time': event.get('time'),
            'text': event.get('text'),
            'acknowledged': alert_id in acknowledged or legacy_id in acknowledged,
        })
    return alerts


def alert_lifecycle_evidence(task, alerts=None):
    task_id = task.get('id')
    source = '任务事件推断'
    if alerts is None:
        records = _derived_alerts(task)
    else:
        source = '传入告警记录'
        records = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            if alert.get('task_id') not in (None, task_id):
                continue
            records.append(alert)
    normalized = []
    for index, alert in enumerate(records):
        severity = _normalized_severity(alert.get('severity'))
        if severity not in REPORTABLE_ALERT_SEVERITIES:
            continue
        alert_id = alert.get('id') or alert.get('alert_id') or '{0}:external:{1}'.format(_text(task_id, 'task'), index)
        raw_status = _text(alert.get('status'), '')
        closed = bool(alert.get('closed_at')) or raw_status in ('已关闭', '关闭')
        acknowledged = bool(alert.get('acknowledged')) or bool(alert.get('acknowledged_at')) or raw_status in ('已确认', '确认', '已关闭', '关闭')
        status = '已关闭' if closed else '已确认' if acknowledged else '打开'
        normalized.append({
            'id': alert_id,
            'severity': severity,
            'status': status,
            'opened_at': _text(alert.get('opened_at') or alert.get('time')),
            'acknowledged_at': _text(alert.get('acknowledged_at')),
            'closed_at': _text(alert.get('closed_at')),
            'text': _text(alert.get('text') or alert.get('message'), '未记录告警内容'),
        })
    normalized.sort(key=lambda item: item['opened_at'], reverse=True)
    critical = sum(1 for item in normalized if item['severity'] == '严重')
    warning = sum(1 for item in normalized if item['severity'] == '警告')
    acknowledged = sum(1 for item in normalized if item['status'] in ('已确认', '已关闭'))
    closed = sum(1 for item in normalized if item['status'] == '已关闭')
    open_critical = sum(1 for item in normalized if item['severity'] == '严重' and item['status'] == '打开')
    if not normalized:
        assessment = '未记录警告或严重告警'
    elif open_critical:
        assessment = '存在 {0} 条未确认严重告警，报告结论需人工复核'.format(open_critical)
    elif closed == len(normalized):
        assessment = '告警均已关闭，形成完整处置闭环'
    elif acknowledged == len(normalized):
        assessment = '告警均已确认，等待后续处置关闭'
    else:
        assessment = '存在未确认告警，建议补充处置记录'
    return {
        'available': bool(normalized),
        'source': source,
        'total': len(normalized),
        'critical': critical,
        'warning': warning,
        'acknowledged': acknowledged,
        'closed': closed,
        'open': len(normalized) - closed,
        'unacknowledged': len(normalized) - acknowledged,
        'open_critical': open_critical,
        'acknowledgement_rate_pct': _percentage(acknowledged, len(normalized)),
        'closure_rate_pct': _percentage(closed, len(normalized)),
        'assessment': assessment,
        'alerts': normalized,
    }


def report_evidence(task, analysis, asset_history=None, alerts=None):
    stage_overview = stage_result_overview(task)
    data_quality = assess_telemetry_data_quality(task, analysis)
    asset_summary = asset_history_evidence(task, asset_history)
    alert_lifecycle = alert_lifecycle_evidence(task, alerts)
    analysis = analysis if isinstance(analysis, dict) else {}
    recommendations = []
    if data_quality['level'] != '充分':
        recommendations.append('补充温度、P99 延迟、吞吐和介质健康度遥测样本后再出具完整性能结论。')
    if stage_overview['available'] and stage_overview['stage_record_coverage_pct'] not in (None, 100):
        recommendations.append('补充各阶段 fio 摘要，避免阶段完成状态仅依赖任务终态推断。')
    if not asset_summary['available']:
        recommendations.append('在测试前后采集资产快照，建立固件、健康度和温度的跨任务基线。')
    if alert_lifecycle['unacknowledged']:
        recommendations.append('确认未确认告警并记录处置结果，形成可审计的告警闭环。')
    if not recommendations:
        recommendations.append('证据完整度满足当前报告归档条件，可结合原始 telemetry 与 fio JSON 复核。')
    report_ready = data_quality['level'] == '充分' and not alert_lifecycle['open_critical']
    return {
        'task': {
            'id': _text(task.get('id')),
            'device': _text(task.get('device')),
            'serial': _text(task.get('serial')),
            'path': _text(task.get('path')),
            'plan': _text(task.get('plan')),
            'status': _text(task.get('status')),
        },
        'analysis': {
            'algorithm': _text(analysis.get('algorithm')),
            'score': analysis.get('score'),
            'conclusion': _text(analysis.get('conclusion')),
            'risk_level': _text(analysis.get('risk_level')),
        },
        'stage_overview': stage_overview,
        'data_quality': data_quality,
        'asset_history': asset_summary,
        'alert_lifecycle': alert_lifecycle,
        'report_ready': report_ready,
        'recommendations': recommendations,
    }


def render_analysis_report(task, analysis, generated_at, enrichment=None):
    # 报告模板很长，留在内部文件，外部调用不再同时依赖证据模块和模板模块。
    from _report_html import render_report_page
    return render_report_page(task, analysis, generated_at, enrichment)
