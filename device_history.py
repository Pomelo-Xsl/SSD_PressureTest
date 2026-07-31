import re
from datetime import datetime

from common import number_in_text as _number, parse_datetime_value as _timestamp, text_value as _text


CAPACITY_UNITS = {
    'B': 1,
    'KB': 1000,
    'MB': 1000 ** 2,
    'GB': 1000 ** 3,
    'TB': 1000 ** 4,
    'PB': 1000 ** 5,
    'KIB': 1024,
    'MIB': 1024 ** 2,
    'GIB': 1024 ** 3,
    'TIB': 1024 ** 4,
    'PIB': 1024 ** 5,
}
SNAPSHOT_VALUE_FIELDS = ('asset_id', 'serial', 'name', 'model', 'path', 'interface', 'transport', 'firmware', 'firmware_version', 'fwrev', 'revision', 'capacity', 'capacity_bytes', 'size_bytes', 'size', 'health', 'temperature', 'status', 'testable')
FIRMWARE_FIELDS = ('firmware', 'firmware_version', 'fwrev', 'revision')
HEALTH_STABLE_DELTA = 0.1
CAPACITY_CHANGE_RATIO = 0.01


def _format_capacity(capacity_bytes):
    if capacity_bytes is None:
        return '--'
    if capacity_bytes >= CAPACITY_UNITS['TB']:
        return '{0:.2f} TB'.format(capacity_bytes / float(CAPACITY_UNITS['TB']))
    if capacity_bytes >= CAPACITY_UNITS['GB']:
        return '{0:.0f} GB'.format(capacity_bytes / float(CAPACITY_UNITS['GB']))
    if capacity_bytes >= CAPACITY_UNITS['MB']:
        return '{0:.0f} MB'.format(capacity_bytes / float(CAPACITY_UNITS['MB']))
    return '{0} B'.format(int(capacity_bytes))


def parse_capacity_bytes(value):
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = _text(value)
    if not text:
        return None
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kmgtpe]?i?b)?\s*', text, flags=re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or 'B').upper()
    multiplier = CAPACITY_UNITS.get(unit)
    return int(amount * multiplier) if multiplier and amount >= 0 else None


def _capacity_from_snapshot(source):
    for field in ('capacity_bytes', 'size_bytes', 'size'):
        value = source.get(field)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        if isinstance(value, str) and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*', value):
            return int(float(value))
    return parse_capacity_bytes(source.get('capacity'))


def _firmware_from_snapshot(source):
    for field in FIRMWARE_FIELDS:
        value = _text(source.get(field))
        if value:
            return value
    return '--'


def _normalize_interface(value):
    text = _text(value, '未知')
    upper = text.upper().replace('_', ' ')
    if 'NVME' in upper:
        return 'NVMe' + text[text.upper().find('NVME') + 4:]
    if 'SAS' in upper:
        return 'SAS'
    if 'SATA' in upper:
        return 'SATA'
    if 'PCIE' in upper or 'PCI-E' in upper:
        return 'PCIe'
    return text


def _snapshot_source(snapshot):
    source = {}
    payload = snapshot.get('payload') if isinstance(snapshot, dict) else None
    if isinstance(payload, dict):
        source.update(payload)
    if isinstance(snapshot, dict):
        for field in SNAPSHOT_VALUE_FIELDS:
            if field in snapshot and snapshot[field] is not None:
                source[field] = snapshot[field]
        if snapshot.get('captured_at'):
            source['captured_at'] = snapshot['captured_at']
    return source


def device_history_key(snapshot):
    source = _snapshot_source(snapshot)
    asset_id = _text(source.get('asset_id'))
    if asset_id:
        return asset_id
    serial = _text(source.get('serial'))
    if serial and serial not in ('未读取', '未读取序列号'):
        return 'serial:{0}'.format(serial)
    path = _text(source.get('path'))
    model = _text(source.get('name') or source.get('model'))
    return 'device:{0}|{1}'.format(path, model)


def device_snapshot_record(snapshot, captured_at=None):
    source = _snapshot_source(snapshot or {})
    capacity_bytes = _capacity_from_snapshot(source)
    health = _number(source.get('health'))
    temperature = _number(source.get('temperature'))
    if health is not None:
        health = round(min(100.0, max(0.0, health)), 3)
    if temperature is not None:
        temperature = round(temperature, 2)
    observed_at = _text(captured_at or source.get('captured_at'), '--')
    model = _text(source.get('name') or source.get('model'), '未知 SSD')
    serial = _text(source.get('serial'), '--')
    path = _text(source.get('path'), '--')
    return {
        'asset_id': device_history_key(source),
        'captured_at': observed_at,
        'name': model,
        'serial': serial,
        'path': path,
        'interface': _normalize_interface(source.get('interface') or source.get('transport')),
        'firmware': _firmware_from_snapshot(source),
        'capacity_bytes': capacity_bytes,
        'capacity': _format_capacity(capacity_bytes),
        'health': health,
        'temperature': temperature,
        'status': _text(source.get('status'), '--'),
        'testable': bool(source.get('testable')),
    }


def _value_change(before, after, comparable=True):
    known = before not in (None, '', '--') and after not in (None, '', '--')
    return {'available': known, 'changed': bool(known and comparable and before != after), 'previous': before, 'current': after}


def _capacity_change(before, after):
    previous = before.get('capacity_bytes')
    current = after.get('capacity_bytes')
    available = previous is not None and current is not None
    difference = current - previous if available else None
    ratio = difference / float(previous) if available and previous else None
    changed = bool(available and abs(ratio if ratio is not None else 0) > CAPACITY_CHANGE_RATIO)
    return {
        'available': available,
        'changed': changed,
        'previous': before.get('capacity'),
        'current': after.get('capacity'),
        'previous_bytes': previous,
        'current_bytes': current,
        'difference_bytes': difference,
        'difference_pct': round(ratio * 100, 3) if ratio is not None else None,
    }


def compare_device_snapshots(previous_snapshot, current_snapshot):
    previous = device_snapshot_record(previous_snapshot)
    current = device_snapshot_record(current_snapshot)
    same_asset = previous['asset_id'] == current['asset_id']
    firmware = _value_change(previous['firmware'], current['firmware'], same_asset)
    interface = _value_change(previous['interface'], current['interface'], same_asset)
    capacity = _capacity_change(previous, current)
    if not same_asset:
        firmware['changed'] = False
        interface['changed'] = False
        capacity['changed'] = False
    changes = []
    if firmware['changed']:
        changes.append({'field': 'firmware', 'label': '固件版本', 'severity': '警告', 'previous': firmware['previous'], 'current': firmware['current']})
    if interface['changed']:
        changes.append({'field': 'interface', 'label': '连接接口', 'severity': '警告', 'previous': interface['previous'], 'current': interface['current']})
    if capacity['changed']:
        changes.append({'field': 'capacity', 'label': '标称容量', 'severity': '严重', 'previous': capacity['previous'], 'current': capacity['current']})
    health = _value_change(previous['health'], current['health'], same_asset)
    if health['available']:
        health['difference'] = round(current['health'] - previous['health'], 3)
    temperature = _value_change(previous['temperature'], current['temperature'], same_asset)
    if temperature['available']:
        temperature['difference'] = round(current['temperature'] - previous['temperature'], 2)
    return {
        'asset_id': current['asset_id'],
        'same_asset': same_asset,
        'previous_captured_at': previous['captured_at'],
        'current_captured_at': current['captured_at'],
        'changed': bool(changes),
        'changes': changes,
        'firmware': firmware,
        'interface': interface,
        'capacity': capacity,
        'health': health,
        'temperature': temperature,
    }


def _trend_direction(start, current, stable_delta=HEALTH_STABLE_DELTA):
    if start is None or current is None:
        return '未知'
    difference = current - start
    if difference < -stable_delta:
        return '下降'
    if difference > stable_delta:
        return '改善'
    return '稳定'


def _ordered_snapshots(snapshots):
    normalized = [device_snapshot_record(item) for item in snapshots if isinstance(item, dict)]
    indexed = list(enumerate(normalized))
    def sort_key(item):
        captured_at = _timestamp(item[1]['captured_at'])
        if captured_at is None:
            return (1, datetime.max, item[0])
        return (0, captured_at.replace(tzinfo=None), item[0])
    indexed.sort(key=sort_key)
    return [item[1] for item in indexed]


def health_timeline(snapshots):
    timeline = _ordered_snapshots(snapshots or [])
    health_points = [item for item in timeline if item['health'] is not None]
    temperature_points = [item for item in timeline if item['temperature'] is not None]
    health_values = [item['health'] for item in health_points]
    temperature_values = [item['temperature'] for item in temperature_points]
    health = {'available': bool(health_points), 'sample_count': len(health_points), 'start': health_values[0] if health_values else None, 'current': health_values[-1] if health_values else None, 'minimum': min(health_values) if health_values else None, 'maximum': max(health_values) if health_values else None}
    health['difference'] = round(health['current'] - health['start'], 3) if health['available'] else None
    health['trend'] = _trend_direction(health['start'], health['current'])
    temperature = {'available': bool(temperature_points), 'sample_count': len(temperature_points), 'current': temperature_values[-1] if temperature_values else None, 'minimum': min(temperature_values) if temperature_values else None, 'maximum': max(temperature_values) if temperature_values else None, 'average': round(sum(temperature_values) / len(temperature_values), 2) if temperature_values else None}
    points = []
    for item in timeline:
        points.append({'captured_at': item['captured_at'], 'health': item['health'], 'temperature': item['temperature'], 'firmware': item['firmware'], 'capacity': item['capacity']})
    return {
        'available': bool(timeline),
        'sample_count': len(timeline),
        'asset_id': timeline[-1]['asset_id'] if timeline else None,
        'latest': timeline[-1] if timeline else None,
        'health': health,
        'temperature': temperature,
        'points': points,
    }


def device_history_overview(snapshots):
    timeline = _ordered_snapshots(snapshots or [])
    trend = health_timeline(timeline)
    latest_change = compare_device_snapshots(timeline[-2], timeline[-1]) if len(timeline) >= 2 else None
    firmware_versions = []
    for item in timeline:
        firmware = item['firmware']
        if firmware != '--' and firmware not in firmware_versions:
            firmware_versions.append(firmware)
    interfaces = []
    for item in timeline:
        interface = item['interface']
        if interface != '未知' and interface not in interfaces:
            interfaces.append(interface)
    capacities = []
    for item in timeline:
        capacity = item['capacity']
        if capacity != '--' and capacity not in capacities:
            capacities.append(capacity)
    return {
        'available': trend['available'],
        'asset_id': trend['asset_id'],
        'sample_count': trend['sample_count'],
        'latest': trend['latest'],
        'health_trend': trend,
        'latest_change': latest_change,
        'firmware_versions': firmware_versions,
        'interfaces_seen': interfaces,
        'capacities_seen': capacities,
        'firmware_changed': len(firmware_versions) > 1,
        'interface_changed': len(interfaces) > 1,
        'capacity_changed': len(capacities) > 1,
    }
