import hashlib
import re
from datetime import datetime
from html import escape


DEFAULT_TIME_PATTERNS = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%dT%H:%M',
    '%Y-%m-%d',
)


def text_value(value, fallback='', strip=True):
    if value is None:
        return fallback
    text = str(value)
    if strip:
        text = text.strip()
    return text if text and text != '--' else fallback


def display_text(value, fallback='--'):
    return text_value(value, fallback=fallback, strip=False)


def html_text(value, fallback='--'):
    return escape(display_text(value, fallback=fallback))


def number_value(value):
    if isinstance(value, bool) or value in (None, '', '--'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def number_in_text(value):
    if value in (None, '', '--'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r'-?\d+(?:\.\d+)?', str(value))
        return float(match.group()) if match else None


def metric_series(samples, field):
    values = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        value = number_value(sample.get(field))
        if value is not None:
            values.append(value)
    return values


def parse_datetime_value(value, patterns=None, strip_microseconds=False, naive=False):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = text_value(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            parsed = None
        if parsed is None:
            for pattern in patterns or DEFAULT_TIME_PATTERNS:
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
    if naive:
        parsed = parsed.replace(tzinfo=None)
    if strip_microseconds:
        parsed = parsed.replace(microsecond=0)
    return parsed


def datetime_string(value, fallback='', patterns=None):
    parsed = parse_datetime_value(value, patterns=patterns, strip_microseconds=True, naive=True)
    return parsed.strftime('%Y-%m-%d %H:%M:%S') if parsed else fallback


def wall_datetime(value):
    return parse_datetime_value(value, strip_microseconds=True, naive=True)


def scope_identity(scope):
    scope = scope or {}
    for field in ('asset_id', 'serial', 'device_path', 'path', 'task_id', 'id'):
        value = scope.get(field)
        if value not in (None, ''):
            return '{0}:{1}'.format(field, value)
    return 'global'


def text_digest(*parts, **options):
    algorithm = options.get('algorithm', 'sha1')
    length = options.get('length')
    separator = options.get('separator', '|')
    source = separator.join(str(part) for part in parts).encode('utf-8')
    digest = hashlib.new(algorithm, source).hexdigest()
    return digest[:length] if length else digest


def bytes_digest(data, algorithm='sha256'):
    return hashlib.new(algorithm, data).hexdigest()
