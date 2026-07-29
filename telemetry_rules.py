from statistics import mean


def number(value):
    if value in (None, '', '--'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_sample(task, sample):
    events = []
    temperature = number(sample.get('temperature'))
    p99 = number(sample.get('p99'))
    throughput = number(sample.get('throughput'))
    threshold = number(task.get('threshold_temp')) or 70
    if temperature is not None:
        if temperature >= threshold:
            events.append(('严重', '温度 {0:.1f}°C 达到阈值 {1:.1f}°C'.format(temperature, threshold)))
        elif temperature >= threshold - 3:
            events.append(('警告', '温度 {0:.1f}°C 接近阈值 {1:.1f}°C'.format(temperature, threshold)))
    if p99 is not None:
        if p99 >= 50:
            events.append(('严重', 'P99 延迟达到 {0:.2f} ms'.format(p99)))
        elif p99 >= 20:
            events.append(('警告', 'P99 延迟偏高：{0:.2f} ms'.format(p99)))
    samples = task.get('samples') or []
    throughput_history = [number(item.get('throughput')) for item in samples[-12:]]
    throughput_history = [item for item in throughput_history if item is not None]
    if throughput is not None and len(throughput_history) >= 6:
        baseline = mean(throughput_history[:-1])
        if baseline and throughput <= baseline * 0.7:
            events.append(('警告', '当前吞吐 {0:.1f} MB/s，较近期均值下降超过 30%'.format(throughput)))
    return events


def telemetry_summary(samples):
    fields = {'temperature': '温度', 'p99': 'P99 延迟', 'throughput': '吞吐', 'health': '健康度'}
    summary = {}
    for field, label in fields.items():
        values = [number(sample.get(field)) for sample in samples]
        values = [value for value in values if value is not None]
        if not values:
            summary[field] = {'label': label, 'count': 0, 'min': None, 'max': None, 'avg': None}
            continue
        summary[field] = {'label': label, 'count': len(values), 'min': round(min(values), 2), 'max': round(max(values), 2), 'avg': round(mean(values), 2)}
    return summary
