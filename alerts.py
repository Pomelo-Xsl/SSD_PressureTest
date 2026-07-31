import copy
from datetime import datetime, timedelta

from common import datetime_string, number_value, scope_identity, text_digest, wall_datetime


SEVERITY_RANK = {'信息': 1, '警告': 2, '严重': 3}
SEVERITY_ALIASES = {
    'info': '信息',
    'information': '信息',
    'warning': '警告',
    'warn': '警告',
    'critical': '严重',
    'error': '严重',
}
RULE_OPERATORS = {'>', '>=', '<', '<=', '==', '!='}
DEFAULT_DEDUP_SECONDS = 300
DEFAULT_SUPPRESSION_SECONDS = 900
DEFAULT_CHANNELS = ['web', 'audit']
MAX_POLICY_NAME_LENGTH = 80
MAX_RULE_NAME_LENGTH = 100
MAX_MESSAGE_LENGTH = 500
OPEN_ALERT_STATUSES = {'打开', '已确认'}
CLOSED_ALERT_STATUSES = {'已关闭', '已恢复', '已抑制'}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalize_channels(channels, fallback=None):
    source = _as_list(channels)
    if not source:
        source = _as_list(fallback)
    normalized = []
    for channel in source:
        value = str(channel or '').strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_CHANNELS)


def _minute_of_day(value):
    text = str(value or '').strip()
    parts = text.split(':')
    if len(parts) != 2:
        raise ValueError('维护窗口时间必须为 HH:MM')
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError('维护窗口时间必须为 HH:MM')
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError('维护窗口时间超出范围')
    return hour * 60 + minute


def _normal_severity(value):
    text = str(value or '警告').strip()
    return SEVERITY_ALIASES.get(text.lower(), text)


def _metric_value(metrics, field):
    current = metrics or {}
    for part in str(field or '').split('.'):
        if not isinstance(current, dict):
            return None
        if part not in current:
            return None
        current = current[part]
    return current


def _compare(actual, operator, threshold):
    if operator == '>':
        return actual > threshold
    if operator == '>=':
        return actual >= threshold
    if operator == '<':
        return actual < threshold
    if operator == '<=':
        return actual <= threshold
    if operator == '==':
        return actual == threshold
    if operator == '!=':
        return actual != threshold
    return False


def alert_fingerprint(policy_id, rule_id, scope):
    return text_digest(policy_id, rule_id, scope_identity(scope), length=24)


def _alert_id(fingerprint, occurred_at):
    return 'alert-{0}'.format(text_digest(fingerprint, datetime_string(occurred_at), length=24))


def _notification_id(alert_id, channel):
    return 'notice-{0}'.format(text_digest(alert_id, channel, length=24))


def _rule_message(rule, actual, scope):
    template = str(rule.get('message') or '').strip()
    scope = scope or {}
    values = {
        'rule': rule.get('name') or rule.get('id'),
        'metric': rule.get('metric'),
        'actual': actual,
        'operator': rule.get('operator'),
        'threshold': rule.get('threshold'),
        'device': scope.get('device') or scope.get('model') or scope.get('asset_id') or '--',
        'path': scope.get('path') or scope.get('device_path') or '--',
        'task_id': scope.get('task_id') or scope.get('id') or '--',
    }
    if template:
        try:
            return template.format(**values)[:MAX_MESSAGE_LENGTH]
        except (KeyError, IndexError, ValueError):
            pass
    return '{0}（{1}）触发 {2}：指标 {3} 当前为 {4}，规则条件为 {5} {6}'.format(
        values['device'], values['path'], values['rule'], values['metric'],
        actual, values['operator'], values['threshold']
    )[:MAX_MESSAGE_LENGTH]


def _history_time(alert):
    for field in ('last_seen_at', 'updated_at', 'created_at', 'opened_at', 'occurred_at', 'time'):
        parsed = wall_datetime(alert.get(field))
        if parsed:
            return parsed
    return None


def _history_status(alert):
    return str(alert.get('status') or '打开').strip()


def _rule_occurrence_count(rule_id, fingerprint, history, now, window_seconds):
    if window_seconds <= 0:
        return 0
    start = now - timedelta(seconds=window_seconds)
    count = 0
    for item in history or []:
        if item.get('rule_id') != rule_id or item.get('fingerprint') != fingerprint:
            continue
        occurred_at = _history_time(item)
        if occurred_at and occurred_at >= start:
            count += 1
    return count


def _active_duplicate(fingerprint, history, now, dedup_seconds):
    if dedup_seconds <= 0:
        return None
    cutoff = now - timedelta(seconds=dedup_seconds)
    for item in reversed(history or []):
        if item.get('fingerprint') != fingerprint:
            continue
        if _history_status(item) not in OPEN_ALERT_STATUSES:
            continue
        last_seen = _history_time(item)
        if last_seen and last_seen >= cutoff:
            return item
    return None


def _recently_suppressed(fingerprint, history, now, suppression_seconds):
    if suppression_seconds <= 0:
        return None
    cutoff = now - timedelta(seconds=suppression_seconds)
    for item in reversed(history or []):
        if item.get('fingerprint') != fingerprint:
            continue
        if _history_status(item) not in CLOSED_ALERT_STATUSES:
            continue
        closed_at = wall_datetime(item.get('closed_at')) or _history_time(item)
        if closed_at and closed_at >= cutoff:
            return item
    return None


def maintenance_window_config(window, index=1):
    source = copy.deepcopy(window or {})
    start = str(source.get('start') or '00:00').strip()
    end = str(source.get('end') or '00:00').strip()
    _minute_of_day(start)
    _minute_of_day(end)
    weekdays = source.get('weekdays')
    if weekdays is None:
        weekdays = list(range(7))
    normalized_days = []
    for day in _as_list(weekdays):
        try:
            numeric_day = int(day)
        except (TypeError, ValueError):
            raise ValueError('维护窗口星期必须为 0 到 6')
        if numeric_day < 0 or numeric_day > 6:
            raise ValueError('维护窗口星期必须为 0 到 6')
        if numeric_day not in normalized_days:
            normalized_days.append(numeric_day)
    severities = []
    for severity in _as_list(source.get('severities')):
        normalized = _normal_severity(severity)
        if normalized not in SEVERITY_RANK:
            raise ValueError('维护窗口告警等级不受支持')
        if normalized not in severities:
            severities.append(normalized)
    return {
        'id': str(source.get('id') or 'maintenance-{0}'.format(index)).strip()[:80],
        'name': str(source.get('name') or '维护窗口 {0}'.format(index)).strip()[:MAX_RULE_NAME_LENGTH],
        'enabled': bool(source.get('enabled', True)),
        'weekdays': sorted(normalized_days),
        'start': start,
        'end': end,
        'severities': severities,
        'suppress_notifications': bool(source.get('suppress_notifications', True)),
    }


def alert_rule_config(rule, default_channels=None, index=1):
    source = copy.deepcopy(rule or {})
    rule_id = str(source.get('id') or 'rule-{0}'.format(index)).strip()
    metric = str(source.get('metric') or '').strip()
    operator = str(source.get('operator') or '>=').strip()
    threshold = number_value(source.get('threshold'))
    if not rule_id:
        raise ValueError('告警规则编号不能为空')
    if not metric:
        raise ValueError('告警规则指标不能为空')
    if operator not in RULE_OPERATORS:
        raise ValueError('告警规则比较符不受支持')
    if threshold is None:
        raise ValueError('告警规则阈值必须为数字')
    severity = _normal_severity(source.get('severity') or '警告')
    if severity not in SEVERITY_RANK:
        raise ValueError('告警规则等级不受支持')
    trigger_after = source.get('trigger_after', 1)
    try:
        trigger_after = int(trigger_after)
    except (TypeError, ValueError):
        raise ValueError('告警规则连续触发次数必须为整数')
    if trigger_after < 1 or trigger_after > 1000:
        raise ValueError('告警规则连续触发次数必须在 1 到 1000 之间')
    dedup_seconds = source.get('dedup_seconds')
    suppression_seconds = source.get('suppression_seconds')
    if dedup_seconds is not None:
        dedup_seconds = int(dedup_seconds)
        if dedup_seconds < 0 or dedup_seconds > 86400:
            raise ValueError('告警规则去重时间必须在 0 到 86400 秒之间')
    if suppression_seconds is not None:
        suppression_seconds = int(suppression_seconds)
        if suppression_seconds < 0 or suppression_seconds > 604800:
            raise ValueError('告警规则抑制时间必须在 0 到 604800 秒之间')
    return {
        'id': rule_id[:80],
        'name': str(source.get('name') or rule_id).strip()[:MAX_RULE_NAME_LENGTH],
        'enabled': bool(source.get('enabled', True)),
        'metric': metric[:120],
        'operator': operator,
        'threshold': threshold,
        'severity': severity,
        'trigger_after': trigger_after,
        'trigger_window_seconds': int(source.get('trigger_window_seconds', 3600)),
        'dedup_seconds': dedup_seconds,
        'suppression_seconds': suppression_seconds,
        'channels': _normalize_channels(source.get('channels'), default_channels),
        'message': str(source.get('message') or '').strip()[:MAX_MESSAGE_LENGTH],
        'metadata': copy.deepcopy(source.get('metadata') or {}),
    }


def alert_policy_config(policy):
    source = copy.deepcopy(policy or {})
    policy_id = str(source.get('id') or 'default-alert-policy').strip()
    if not policy_id:
        raise ValueError('告警策略编号不能为空')
    try:
        version = int(source.get('version', 1))
    except (TypeError, ValueError):
        raise ValueError('告警策略版本必须为整数')
    if version < 1:
        raise ValueError('告警策略版本必须大于 0')
    try:
        dedup_seconds = int(source.get('dedup_seconds', DEFAULT_DEDUP_SECONDS))
        suppression_seconds = int(source.get('suppression_seconds', DEFAULT_SUPPRESSION_SECONDS))
    except (TypeError, ValueError):
        raise ValueError('告警策略时间参数必须为整数')
    if dedup_seconds < 0 or dedup_seconds > 86400:
        raise ValueError('告警策略去重时间必须在 0 到 86400 秒之间')
    if suppression_seconds < 0 or suppression_seconds > 604800:
        raise ValueError('告警策略抑制时间必须在 0 到 604800 秒之间')
    channels = _normalize_channels(source.get('channels'), DEFAULT_CHANNELS)
    rules = []
    rule_ids = set()
    for index, rule in enumerate(source.get('rules') or [], 1):
        normalized = alert_rule_config(rule, channels, index)
        if normalized['id'] in rule_ids:
            raise ValueError('告警策略中存在重复规则编号：{0}'.format(normalized['id']))
        rule_ids.add(normalized['id'])
        rules.append(normalized)
    windows = []
    window_ids = set()
    for index, window in enumerate(source.get('maintenance_windows') or [], 1):
        normalized = maintenance_window_config(window, index)
        if not normalized['id']:
            raise ValueError('维护窗口编号不能为空')
        if normalized['id'] in window_ids:
            raise ValueError('维护窗口编号重复：{0}'.format(normalized['id']))
        window_ids.add(normalized['id'])
        windows.append(normalized)
    history = []
    for item in source.get('versions') or []:
        try:
            item_version = int(item.get('version'))
        except (AttributeError, TypeError, ValueError):
            continue
        history.append({
            'version': item_version,
            'published_at': datetime_string(item.get('published_at')),
            'reason': str(item.get('reason') or '').strip()[:240],
        })
    return {
        'id': policy_id[:80],
        'name': str(source.get('name') or policy_id).strip()[:MAX_POLICY_NAME_LENGTH],
        'enabled': bool(source.get('enabled', True)),
        'version': version,
        'channels': channels,
        'dedup_seconds': dedup_seconds,
        'suppression_seconds': suppression_seconds,
        'rules': rules,
        'maintenance_windows': windows,
        'versions': history,
        'description': str(source.get('description') or '').strip()[:500],
        'updated_at': datetime_string(source.get('updated_at')),
    }


def create_policy_version(policy, changes=None, changed_at=None, reason=''):
    current = alert_policy_config(policy)
    next_policy = copy.deepcopy(current)
    for key, value in (changes or {}).items():
        if key in ('id', 'version', 'versions'):
            continue
        next_policy[key] = copy.deepcopy(value)
    next_policy['version'] = current['version'] + 1
    published_at = datetime_string(changed_at or datetime.now())
    versions = list(current.get('versions') or [])
    versions.append({
        'version': current['version'],
        'published_at': current.get('updated_at') or published_at,
        'reason': '历史版本快照',
    })
    next_policy['versions'] = versions
    next_policy['updated_at'] = published_at
    normalized = alert_policy_config(next_policy)
    normalized['versions'][-1]['reason'] = str(reason or normalized['versions'][-1]['reason'])[:240]
    return normalized


def active_maintenance_windows(policy, now, severity=None):
    normalized = alert_policy_config(policy)
    current = wall_datetime(now) or datetime.now()
    minute = current.hour * 60 + current.minute
    weekday = current.weekday()
    result = []
    for window in normalized['maintenance_windows']:
        if not window['enabled']:
            continue
        allowed_severities = window.get('severities') or []
        if severity and allowed_severities and _normal_severity(severity) not in allowed_severities:
            continue
        start = _minute_of_day(window['start'])
        end = _minute_of_day(window['end'])
        if start == end:
            matched_day = weekday in window['weekdays']
        elif start < end:
            matched_day = weekday in window['weekdays'] and start <= minute < end
        else:
            previous_day = (weekday - 1) % 7
            matched_day = (weekday in window['weekdays'] and minute >= start) or (previous_day in window['weekdays'] and minute < end)
        if matched_day:
            result.append(window)
    return result


def apply_alert_policy(policy, metrics, scope=None, alert_history=None, now=None):
    normalized = alert_policy_config(policy)
    current = wall_datetime(now) or datetime.now().replace(microsecond=0)
    history = list(alert_history or [])
    outcome = {
        'policy_id': normalized['id'],
        'policy_version': normalized['version'],
        'evaluated_at': datetime_string(current),
        'matches': [],
        'suppressed': [],
        'notifications': [],
        'skipped': [],
    }
    if not normalized['enabled']:
        outcome['skipped'].append({'reason': '策略已停用'})
        return outcome
    scope = copy.deepcopy(scope or {})
    for rule in normalized['rules']:
        if not rule['enabled']:
            outcome['skipped'].append({'rule_id': rule['id'], 'reason': '规则已停用'})
            continue
        raw_value = _metric_value(metrics, rule['metric'])
        actual = number_value(raw_value)
        if actual is None:
            outcome['skipped'].append({'rule_id': rule['id'], 'reason': '指标值不可用', 'metric': rule['metric']})
            continue
        if not _compare(actual, rule['operator'], rule['threshold']):
            outcome['skipped'].append({'rule_id': rule['id'], 'reason': '未满足触发条件', 'actual': actual})
            continue
        fingerprint = alert_fingerprint(normalized['id'], rule['id'], scope)
        occurrences = _rule_occurrence_count(rule['id'], fingerprint, history, current, rule['trigger_window_seconds'])
        if occurrences + 1 < rule['trigger_after']:
            outcome['skipped'].append({'rule_id': rule['id'], 'reason': '连续触发次数不足', 'actual': actual, 'occurrences': occurrences + 1, 'required': rule['trigger_after']})
            continue
        candidate = {
            'id': _alert_id(fingerprint, current),
            'fingerprint': fingerprint,
            'policy_id': normalized['id'],
            'policy_version': normalized['version'],
            'rule_id': rule['id'],
            'rule_name': rule['name'],
            'severity': rule['severity'],
            'metric': rule['metric'],
            'operator': rule['operator'],
            'threshold': rule['threshold'],
            'actual': actual,
            'scope': scope,
            'occurred_at': datetime_string(current),
            'status': '打开',
            'message': _rule_message(rule, actual, scope),
            'channels': list(rule['channels']),
            'metadata': copy.deepcopy(rule['metadata']),
            'occurrence_count': occurrences + 1,
        }
        duplicate = _active_duplicate(fingerprint, history, current, rule['dedup_seconds'] if rule['dedup_seconds'] is not None else normalized['dedup_seconds'])
        if duplicate:
            candidate['status'] = '已抑制'
            candidate['suppression_reason'] = '去重：{0} 秒内已有未关闭同类告警'.format(rule['dedup_seconds'] if rule['dedup_seconds'] is not None else normalized['dedup_seconds'])
            candidate['duplicate_of'] = duplicate.get('id')
            outcome['suppressed'].append(candidate)
            continue
        cooldown = _recently_suppressed(fingerprint, history, current, rule['suppression_seconds'] if rule['suppression_seconds'] is not None else normalized['suppression_seconds'])
        if cooldown:
            candidate['status'] = '已抑制'
            candidate['suppression_reason'] = '抑制：近期已关闭同类告警'
            candidate['suppressed_by'] = cooldown.get('id')
            outcome['suppressed'].append(candidate)
            continue
        maintenance = active_maintenance_windows(normalized, current, rule['severity'])
        if maintenance:
            candidate['maintenance_windows'] = [window['id'] for window in maintenance]
            if any(window['suppress_notifications'] for window in maintenance):
                candidate['status'] = '已抑制'
                candidate['suppression_reason'] = '维护窗口：{0}'.format('、'.join(window['name'] for window in maintenance))
                outcome['suppressed'].append(candidate)
                continue
        outcome['matches'].append(candidate)
    outcome['notifications'] = prepare_notification_outbox(outcome['matches'], current)
    return outcome


def prepare_notification_outbox(alerts, now=None):
    current = wall_datetime(now) or datetime.now().replace(microsecond=0)
    records = []
    seen = set()
    for alert in alerts or []:
        if alert.get('status') not in (None, '', '打开'):
            continue
        for channel in _normalize_channels(alert.get('channels'), DEFAULT_CHANNELS):
            notification_id = _notification_id(alert.get('id'), channel)
            if notification_id in seen:
                continue
            seen.add(notification_id)
            records.append({
                'id': notification_id,
                'alert_id': alert.get('id'),
                'fingerprint': alert.get('fingerprint'),
                'channel': channel,
                'status': '待发送',
                'created_at': datetime_string(current),
                'payload': {
                    'title': '{0}：{1}'.format(alert.get('severity') or '告警', alert.get('rule_name') or alert.get('rule_id') or '未命名规则'),
                    'message': alert.get('message') or '',
                    'severity': alert.get('severity'),
                    'scope': copy.deepcopy(alert.get('scope') or {}),
                    'occurred_at': alert.get('occurred_at'),
                    'policy_id': alert.get('policy_id'),
                    'policy_version': alert.get('policy_version'),
                    'rule_id': alert.get('rule_id'),
                },
            })
    return records


def mark_notification_result(record, succeeded, processed_at=None, error_text=''):
    result = copy.deepcopy(record or {})
    current = wall_datetime(processed_at) or datetime.now().replace(microsecond=0)
    result['status'] = '已发送' if succeeded else '发送失败'
    result['processed_at'] = datetime_string(current)
    result['error_text'] = '' if succeeded else str(error_text or '未知通知错误')[:500]
    return result


def policy_overview(policy):
    normalized = alert_policy_config(policy)
    severity_counts = {'信息': 0, '警告': 0, '严重': 0}
    enabled_rules = 0
    for rule in normalized['rules']:
        if rule['enabled']:
            enabled_rules += 1
            severity_counts[rule['severity']] += 1
    return {
        'policy_id': normalized['id'],
        'name': normalized['name'],
        'version': normalized['version'],
        'enabled': normalized['enabled'],
        'rule_count': len(normalized['rules']),
        'enabled_rule_count': enabled_rules,
        'severity_counts': severity_counts,
        'maintenance_window_count': len(normalized['maintenance_windows']),
        'channels': normalized['channels'],
    }


ALERT_SEVERITIES = {'警告': 2, '严重': 3}


def alerts_for_tasks(tasks):
    alerts = []
    for task in tasks:
        for index, event in enumerate(task.get('events') or []):
            severity = event.get('severity')
            if severity not in ALERT_SEVERITIES:
                continue
            legacy_id = '{0}:{1}'.format(task.get('id'), index)
            alert_id = event.get('id') or legacy_id
            acknowledged = task.get('acknowledged_alerts') or []
            alerts.append({'id': alert_id, 'task_id': task.get('id'), 'device': task.get('device'), 'path': task.get('path'), 'plan': task.get('plan'), 'severity': severity, 'time': event.get('time'), 'text': event.get('text'), 'acknowledged': alert_id in acknowledged or legacy_id in acknowledged})
    return sorted(alerts, key=lambda alert: (ALERT_SEVERITIES[alert['severity']], alert.get('time') or ''), reverse=True)


def alert_overview(alerts):
    summary = {'total': len(alerts), 'critical': 0, 'warning': 0, 'acknowledged': 0, 'unacknowledged': 0}
    for alert in alerts:
        if alert['severity'] == '严重':
            summary['critical'] += 1
        else:
            summary['warning'] += 1
        if alert['acknowledged']:
            summary['acknowledged'] += 1
        else:
            summary['unacknowledged'] += 1
    return summary


def acknowledge_alert(task, alert_id):
    prefix = '{0}:'.format(task.get('id'))
    if not alert_id.startswith(prefix):
        raise ValueError('告警不属于当前任务')
    events = task.get('events') or []
    event = next((item for item in events if item.get('id') == alert_id), None)
    if event is None:
        try:
            index = int(alert_id.removeprefix(prefix))
        except ValueError:
            raise ValueError('告警编号格式不正确')
        if index < 0 or index >= len(events):
            raise ValueError('告警不存在或不是可确认告警')
        event = events[index]
    if event.get('severity') not in ALERT_SEVERITIES:
        raise ValueError('告警不存在或不是可确认告警')
    acknowledged = task.setdefault('acknowledged_alerts', [])
    canonical_id = event.get('id') or alert_id
    if canonical_id not in acknowledged:
        acknowledged.append(canonical_id)
    return {'alert_id': canonical_id, 'acknowledged': True}
