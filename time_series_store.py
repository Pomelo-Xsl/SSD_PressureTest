import math
import re
import threading
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from statistics import median


TIMESTAMP_FIELDS = ('timestamp', 'time', 'sampled_at', 'observed_at', 'captured_at', 'created_at', 'timestamp_ms')
STAGE_FIELDS = ('stage', 'stage_name', 'phase', 'active_stage')
LABEL_FIELDS = ('labels', 'tags')
NON_METRIC_FIELDS = set(TIMESTAMP_FIELDS + STAGE_FIELDS + LABEL_FIELDS + (
    'id', 'task_id', 'series_id', 'asset_id', 'device', 'device_id', 'serial',
    'path', 'host', 'source', 'status', 'message', 'error', 'metadata',
    'metrics', 'timestamp_ms', 'ordinal', 'sequence',
))
DEFAULT_MAX_SAMPLES_PER_SERIES = 200000
DEFAULT_QUERY_LIMIT = 10000
DEFAULT_GAP_FACTOR = 1.5


def parse_sample_timestamp(value, default_timezone=timezone.utc):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if abs(seconds) >= 100000000000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError('时间戳超出可处理范围')
    elif isinstance(value, str):
        text = value.strip()
        if not text or text == '--':
            raise ValueError('采样记录缺少有效时间')
        try:
            numeric = float(text)
        except ValueError:
            numeric = None
        if numeric is not None and re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            return parse_sample_timestamp(numeric, default_timezone)
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            parsed = None
        if parsed is None:
            for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d'):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError('无法解析采样时间：{0}'.format(text))
    else:
        raise ValueError('采样时间类型不受支持')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def format_sample_timestamp(value):
    parsed = parse_sample_timestamp(value)
    if parsed.microsecond:
        text = parsed.isoformat(timespec='milliseconds')
    else:
        text = parsed.isoformat(timespec='seconds')
    return text.replace('+00:00', 'Z')


def _timestamp_milliseconds(value):
    parsed = parse_sample_timestamp(value)
    return int(round(parsed.timestamp() * 1000))


def _first_value(source, fields):
    for field in fields:
        if field in source and source[field] not in (None, '', '--'):
            return source[field]
    return None


def _metric_number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    if not isinstance(value, str):
        return None
    text = value.strip().replace(',', '')
    if not text or text == '--':
        return None
    try:
        number = float(text)
    except ValueError:
        matched = re.match(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:%|[A-Za-z/]+)?$', text)
        if not matched:
            return None
        try:
            number = float(matched.group(1))
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _normalized_labels(source):
    labels = {}
    for field in LABEL_FIELDS:
        value = source.get(field)
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            if item is None:
                continue
            label_key = str(key).strip()
            label_value = str(item).strip()
            if label_key and label_value:
                labels[label_key] = label_value
    return labels


def _stage_label(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_metric_sample(sample, default_timestamp=None, metric_fields=None, stage=None, default_timezone=timezone.utc):
    if not isinstance(sample, dict):
        raise TypeError('采样记录必须是字典')
    timestamp_value = _first_value(sample, TIMESTAMP_FIELDS)
    if timestamp_value is None:
        timestamp_value = default_timestamp
    if timestamp_value is None:
        raise ValueError('采样记录缺少时间字段')
    parsed_time = parse_sample_timestamp(timestamp_value, default_timezone)
    metrics_source = sample.get('metrics') if isinstance(sample.get('metrics'), dict) else {}
    selected_fields = metric_fields
    if selected_fields is None:
        selected_fields = []
        for key in metrics_source:
            if key not in selected_fields:
                selected_fields.append(key)
        for key in sample:
            if key not in NON_METRIC_FIELDS and key not in selected_fields:
                selected_fields.append(key)
    elif isinstance(selected_fields, str):
        selected_fields = [selected_fields]
    metrics = {}
    for field in selected_fields:
        field_name = str(field).strip()
        if not field_name:
            continue
        value = metrics_source.get(field_name, sample.get(field_name))
        number = _metric_number(value)
        if number is not None:
            metrics[field_name] = number
    raw_stage = stage if stage is not None else _first_value(sample, STAGE_FIELDS)
    return {
        'timestamp': format_sample_timestamp(parsed_time),
        'timestamp_ms': int(round(parsed_time.timestamp() * 1000)),
        'metrics': metrics,
        'stage': _stage_label(raw_stage),
        'labels': _normalized_labels(sample),
    }


def flatten_metric_sample(sample):
    normalized = normalize_metric_sample(sample)
    flattened = {
        'timestamp': normalized['timestamp'],
        'timestamp_ms': normalized['timestamp_ms'],
    }
    flattened.update(normalized['metrics'])
    if normalized['stage'] is not None:
        flattened['stage'] = normalized['stage']
    if normalized['labels']:
        flattened['labels'] = dict(normalized['labels'])
    return flattened


def _normalize_samples(samples, default_timestamp=None, metric_fields=None, ignore_invalid=False):
    if isinstance(samples, dict):
        samples = [samples]
    if samples is None:
        return [], []
    records = []
    rejected = []
    for index, sample in enumerate(samples):
        try:
            records.append(normalize_metric_sample(sample, default_timestamp, metric_fields))
        except (TypeError, ValueError) as exc:
            if not ignore_invalid:
                raise
            rejected.append({'index': index, 'error': str(exc)})
    records.sort(key=lambda item: item['timestamp_ms'])
    return records, rejected


def _clone_record(record):
    return {
        'timestamp': record['timestamp'],
        'timestamp_ms': record['timestamp_ms'],
        'metrics': dict(record.get('metrics') or {}),
        'stage': record.get('stage'),
        'labels': dict(record.get('labels') or {}),
    }


def _primary_metric(records, requested_metric=None):
    if requested_metric:
        return requested_metric if any(requested_metric in item['metrics'] for item in records) else None
    coverage = {}
    for record in records:
        for field, value in record['metrics'].items():
            if _metric_number(value) is not None:
                coverage[field] = coverage.get(field, 0) + 1
    if not coverage:
        return None
    return sorted(coverage, key=lambda field: (-coverage[field], field))[0]


def _uniform_indices(length, count):
    if count <= 0 or length <= 0:
        return []
    if count >= length:
        return list(range(length))
    if count == 1:
        return [length - 1]
    indexes = []
    for position in range(count):
        index = int(round(position * (length - 1) / float(count - 1)))
        if not indexes or index != indexes[-1]:
            indexes.append(index)
    return indexes


def _minmax_indices(records, max_points, metric):
    length = len(records)
    if max_points >= length:
        return list(range(length))
    if max_points == 1:
        return [length - 1]
    if max_points == 2:
        return [0, length - 1]
    interior_indexes = list(range(1, length - 1))
    slots = max_points - 2
    values = []
    last_value = None
    for record in records:
        value = _metric_number(record['metrics'].get(metric)) if metric else None
        if value is None:
            value = last_value
        if value is not None:
            last_value = value
        values.append(value)
    if not metric or all(value is None for value in values):
        return _uniform_indices(length, max_points)
    first_known = next(value for value in values if value is not None)
    values = [first_known if value is None else value for value in values]
    bucket_count = max(1, int(math.ceil(slots / 2.0)))
    candidates = {}
    global_min = min(interior_indexes, key=lambda index: values[index])
    global_max = max(interior_indexes, key=lambda index: values[index])
    candidates[global_min] = float('inf')
    candidates[global_max] = float('inf')
    for bucket in range(bucket_count):
        start = 1 + int(math.floor(bucket * len(interior_indexes) / float(bucket_count)))
        end = 1 + int(math.floor((bucket + 1) * len(interior_indexes) / float(bucket_count)))
        indexes = list(range(start, max(start + 1, end)))
        if not indexes:
            continue
        average = sum(values[index] for index in indexes) / float(len(indexes))
        low = min(indexes, key=lambda index: values[index])
        high = max(indexes, key=lambda index: values[index])
        candidates[low] = max(candidates.get(low, 0), abs(values[low] - average))
        candidates[high] = max(candidates.get(high, 0), abs(values[high] - average))
    selected = {global_min, global_max}
    for index, _ in sorted(candidates.items(), key=lambda item: (-item[1], item[0])):
        if len(selected) >= slots:
            break
        selected.add(index)
    if len(selected) < slots:
        for index in _uniform_indices(length, max_points):
            if 0 < index < length - 1:
                selected.add(index)
            if len(selected) >= slots:
                break
    return [0] + sorted(selected)[:slots] + [length - 1]


def downsample_time_series(samples, max_points, metric=None, method='minmax', ignore_invalid=False):
    try:
        max_points = int(max_points)
    except (TypeError, ValueError):
        raise ValueError('max_points 必须为正整数')
    if max_points < 1:
        raise ValueError('max_points 必须大于 0')
    records, _ = _normalize_samples(samples, ignore_invalid=ignore_invalid)
    if len(records) <= max_points:
        return [_clone_record(item) for item in records]
    if method not in ('minmax', 'uniform'):
        raise ValueError('不支持的降采样方法：{0}'.format(method))
    if method == 'uniform':
        indexes = _uniform_indices(len(records), max_points)
    else:
        indexes = _minmax_indices(records, max_points, _primary_metric(records, metric))
    return [_clone_record(records[index]) for index in indexes]


def _infer_interval_seconds(records):
    intervals = []
    for previous, current in zip(records, records[1:]):
        seconds = (current['timestamp_ms'] - previous['timestamp_ms']) / 1000.0
        if seconds > 0:
            intervals.append(seconds)
    return float(median(intervals)) if intervals else None


def calculate_missing_intervals(samples, expected_interval_seconds=None, gap_factor=DEFAULT_GAP_FACTOR, ignore_invalid=False):
    records, _ = _normalize_samples(samples, ignore_invalid=ignore_invalid)
    if expected_interval_seconds is not None:
        try:
            interval = float(expected_interval_seconds)
        except (TypeError, ValueError):
            raise ValueError('期望采样间隔必须是数字')
        if interval <= 0:
            raise ValueError('期望采样间隔必须大于 0')
    else:
        interval = _infer_interval_seconds(records)
    try:
        gap_factor = float(gap_factor)
    except (TypeError, ValueError):
        raise ValueError('缺失判定倍数必须是数字')
    if gap_factor <= 1:
        raise ValueError('缺失判定倍数必须大于 1')
    gaps = []
    if interval is not None:
        for previous, current in zip(records, records[1:]):
            seconds = (current['timestamp_ms'] - previous['timestamp_ms']) / 1000.0
            if seconds > interval * gap_factor:
                estimated = max(1, int(round(seconds / interval)) - 1)
                gaps.append({
                    'from': previous['timestamp'],
                    'to': current['timestamp'],
                    'gap_seconds': round(seconds, 3),
                    'estimated_missing_samples': estimated,
                })
    span = 0.0
    if len(records) > 1:
        span = (records[-1]['timestamp_ms'] - records[0]['timestamp_ms']) / 1000.0
    return {
        'available': len(records) >= 2 and interval is not None,
        'sample_count': len(records),
        'expected_interval_seconds': round(interval, 3) if interval is not None else None,
        'observed_span_seconds': round(span, 3),
        'gap_count': len(gaps),
        'missing_sample_count': sum(item['estimated_missing_samples'] for item in gaps),
        'largest_gap_seconds': max([item['gap_seconds'] for item in gaps] or [0]),
        'gaps': gaps,
    }


def data_quality_summary(samples, expected_interval_seconds=None, gap_factor=DEFAULT_GAP_FACTOR):
    records, rejected = _normalize_samples(samples, ignore_invalid=True)
    original_records = []
    for sample in samples or []:
        try:
            original_records.append(normalize_metric_sample(sample))
        except (TypeError, ValueError):
            continue
    out_of_order_count = 0
    for previous, current in zip(original_records, original_records[1:]):
        if current['timestamp_ms'] < previous['timestamp_ms']:
            out_of_order_count += 1
    duplicate_count = 0
    seen = set()
    for record in records:
        if record['timestamp_ms'] in seen:
            duplicate_count += 1
        seen.add(record['timestamp_ms'])
    missing = calculate_missing_intervals(records, expected_interval_seconds, gap_factor)
    denominator = len(records) + missing['missing_sample_count']
    completeness = round(len(records) / float(denominator) * 100, 2) if denominator else None
    metric_counts = {}
    for record in records:
        for field in record['metrics']:
            metric_counts[field] = metric_counts.get(field, 0) + 1
    metric_coverage = {}
    for field, count in sorted(metric_counts.items()):
        metric_coverage[field] = {
            'present_samples': count,
            'coverage_pct': round(count / float(len(records)) * 100, 2) if records else None,
        }
    if not records:
        quality = '无数据'
    elif rejected or duplicate_count or out_of_order_count or (completeness is not None and completeness < 80):
        quality = '需关注'
    elif completeness is not None and completeness < 98:
        quality = '可用'
    else:
        quality = '良好'
    return {
        'quality': quality,
        'valid_sample_count': len(records),
        'invalid_sample_count': len(rejected),
        'invalid_samples': rejected,
        'duplicate_timestamp_count': duplicate_count,
        'out_of_order_count': out_of_order_count,
        'completeness_pct': completeness,
        'metric_coverage': metric_coverage,
        'missing_intervals': missing,
    }


def _stage_window(stage, index, cursor_ms, baseline_ms):
    if not isinstance(stage, dict):
        stage = {'name': stage}
    name = _stage_label(stage.get('name') or stage.get('label') or stage.get('stage') or stage.get('id'))
    if name is None:
        name = '阶段 {0}'.format(index + 1)
    absolute_start = _first_value(stage, ('start_at', 'started_at', 'start_time'))
    relative_start = _first_value(stage, ('start_seconds', 'offset_seconds', 'start_offset'))
    if absolute_start is not None:
        start_ms = _timestamp_milliseconds(absolute_start)
    elif relative_start is not None:
        start_ms = baseline_ms + int(float(relative_start) * 1000)
    else:
        start_ms = cursor_ms
    absolute_end = _first_value(stage, ('end_at', 'ended_at', 'end_time'))
    relative_end = _first_value(stage, ('end_seconds', 'end_offset'))
    duration = _first_value(stage, ('duration_seconds', 'duration', 'seconds'))
    if absolute_end is not None:
        end_ms = _timestamp_milliseconds(absolute_end)
    elif relative_end is not None:
        end_ms = baseline_ms + int(float(relative_end) * 1000)
    elif duration is not None:
        end_ms = start_ms + int(float(duration) * 1000)
    else:
        end_ms = None
    if end_ms is not None and end_ms < start_ms:
        raise ValueError('阶段结束时间早于开始时间：{0}'.format(name))
    return {'name': name, 'start_ms': start_ms, 'end_ms': end_ms, 'ordinal': index + 1}, end_ms if end_ms is not None else start_ms


def merge_stage_labels(samples, stages, default_stage=None, overwrite=False, ignore_invalid=False):
    records, _ = _normalize_samples(samples, ignore_invalid=ignore_invalid)
    if not records:
        return []
    baseline = records[0]['timestamp_ms']
    cursor = baseline
    windows = []
    for index, stage in enumerate(stages or []):
        window, cursor = _stage_window(stage, index, cursor, baseline)
        windows.append(window)
    merged = []
    for record in records:
        item = _clone_record(record)
        matched = None
        for position, window in enumerate(windows):
            is_last = position == len(windows) - 1
            if record['timestamp_ms'] < window['start_ms']:
                continue
            if window['end_ms'] is None or record['timestamp_ms'] < window['end_ms'] or (is_last and record['timestamp_ms'] == window['end_ms']):
                matched = window
                break
        if (overwrite or not item['stage']) and matched is not None:
            item['stage'] = matched['name']
            item['labels']['stage_ordinal'] = str(matched['ordinal'])
        elif (overwrite or not item['stage']) and default_stage is not None:
            item['stage'] = _stage_label(default_stage)
        merged.append(item)
    return merged


class InMemoryTimeSeriesStore:

    def __init__(self, max_samples_per_series=DEFAULT_MAX_SAMPLES_PER_SERIES):
        try:
            max_samples_per_series = int(max_samples_per_series)
        except (TypeError, ValueError):
            raise ValueError('每个序列的最大样本数必须是正整数')
        if max_samples_per_series < 1:
            raise ValueError('每个序列的最大样本数必须大于 0')
        self.max_samples_per_series = max_samples_per_series
        self._series = {}
        self._lock = threading.RLock()

    def write_samples(self, series_id, samples, stage=None, merge_same_timestamp=True):
        key = str(series_id or '').strip()
        if not key:
            raise ValueError('series_id 不能为空')
        if isinstance(samples, dict):
            samples = [samples]
        prepared = []
        for sample in samples or []:
            prepared.append(normalize_metric_sample(sample, stage=stage))
        if not prepared:
            return {'series_id': key, 'written': 0, 'merged': 0, 'evicted': 0, 'sample_count': self.count_samples(key)}
        with self._lock:
            records = self._series.setdefault(key, [])
            written = 0
            merged = 0
            for incoming in prepared:
                timestamps = [item['timestamp_ms'] for item in records]
                left = bisect_left(timestamps, incoming['timestamp_ms'])
                right = bisect_right(timestamps, incoming['timestamp_ms'])
                target = records[left] if merge_same_timestamp and left < right else None
                if target is not None:
                    target['metrics'].update(incoming['metrics'])
                    target['labels'].update(incoming['labels'])
                    if incoming['stage'] is not None:
                        target['stage'] = incoming['stage']
                    merged += 1
                else:
                    records.insert(right, _clone_record(incoming))
                    written += 1
            evicted = max(0, len(records) - self.max_samples_per_series)
            if evicted:
                del records[:evicted]
            return {'series_id': key, 'written': written, 'merged': merged, 'evicted': evicted, 'sample_count': len(records)}

    def query_samples(self, series_id, start=None, end=None, metric_names=None, stages=None, limit=None, descending=False):
        key = str(series_id or '').strip()
        if limit is None:
            limit = DEFAULT_QUERY_LIMIT
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError('查询条数必须是正整数')
        if limit < 1:
            raise ValueError('查询条数必须大于 0')
        start_ms = _timestamp_milliseconds(start) if start is not None else None
        end_ms = _timestamp_milliseconds(end) if end is not None else None
        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            raise ValueError('查询开始时间不能晚于结束时间')
        if isinstance(metric_names, str):
            metric_names = [metric_names]
        selected_metrics = set(metric_names or [])
        if isinstance(stages, str):
            stages = [stages]
        selected_stages = {_stage_label(value) for value in (stages or [])}
        with self._lock:
            records = self._series.get(key, [])
            timestamps = [item['timestamp_ms'] for item in records]
            left = bisect_left(timestamps, start_ms) if start_ms is not None else 0
            right = bisect_right(timestamps, end_ms) if end_ms is not None else len(records)
            selected = []
            for record in records[left:right]:
                if selected_stages and record.get('stage') not in selected_stages:
                    continue
                item = _clone_record(record)
                if selected_metrics:
                    item['metrics'] = {field: value for field, value in item['metrics'].items() if field in selected_metrics}
                selected.append(item)
            if descending:
                selected.reverse()
            return selected[:limit]

    def query_downsampled(self, series_id, max_points, metric=None, start=None, end=None, stages=None, method='minmax'):
        records = self.query_samples(series_id, start, end, stages=stages, limit=self.max_samples_per_series)
        return downsample_time_series(records, max_points, metric, method)

    def quality_summary(self, series_id, expected_interval_seconds=None, start=None, end=None):
        records = self.query_samples(series_id, start, end, limit=self.max_samples_per_series)
        return data_quality_summary(records, expected_interval_seconds)

    def series_summary(self, series_id):
        key = str(series_id or '').strip()
        with self._lock:
            records = self._series.get(key, [])
            metric_names = sorted({field for record in records for field in record['metrics']})
            stages = sorted({record['stage'] for record in records if record.get('stage')})
            return {
                'series_id': key,
                'sample_count': len(records),
                'first_timestamp': records[0]['timestamp'] if records else None,
                'last_timestamp': records[-1]['timestamp'] if records else None,
                'metrics': metric_names,
                'stages': stages,
            }

    def list_series(self):
        with self._lock:
            return [self.series_summary(key) for key in sorted(self._series)]

    def count_samples(self, series_id):
        key = str(series_id or '').strip()
        with self._lock:
            return len(self._series.get(key, []))

    def clear_series(self, series_id):
        key = str(series_id or '').strip()
        with self._lock:
            removed = len(self._series.pop(key, []))
        return removed

    def snapshot(self):
        with self._lock:
            return {key: [_clone_record(record) for record in records] for key, records in self._series.items()}
