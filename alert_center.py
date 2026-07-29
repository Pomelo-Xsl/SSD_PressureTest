ALERT_SEVERITIES = {'警告': 2, '严重': 3}


def build_alerts(tasks):
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


def alert_summary(alerts):
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
