from statistics import mean


def numeric_values(samples, field):
    values = []
    for sample in samples:
        value = sample.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        values.append(number)
    return values


def temperature_dwell(samples, threshold):
    temperatures = numeric_values(samples, 'temperature')
    if not temperatures:
        return {'available': False, 'sample_count': 0, 'near_limit_count': 0, 'over_limit_count': 0, 'near_limit_pct': None}
    near_limit = sum(value >= threshold - 5 for value in temperatures)
    over_limit = sum(value >= threshold for value in temperatures)
    return {'available': True, 'sample_count': len(temperatures), 'near_limit_count': near_limit, 'over_limit_count': over_limit, 'near_limit_pct': round(near_limit / len(temperatures) * 100, 2)}


def latency_slo(samples, target_ms=20):
    values = numeric_values(samples, 'p99')
    if not values:
        return {'available': False, 'target_ms': target_ms, 'pass_rate_pct': None, 'violations': 0}
    violations = sum(value > target_ms for value in values)
    return {'available': True, 'target_ms': target_ms, 'pass_rate_pct': round((len(values) - violations) / len(values) * 100, 2), 'violations': violations, 'worst_ms': round(max(values), 2)}


def throughput_jitter(samples):
    values = numeric_values(samples, 'throughput')
    if len(values) < 2:
        return {'available': False, 'jitter_pct': None, 'drop_count': 0}
    average = mean(values)
    differences = [abs(current - previous) for previous, current in zip(values, values[1:])]
    jitter = mean(differences) / average * 100 if average else 0
    drops = sum(current < previous * 0.8 for previous, current in zip(values, values[1:]))
    return {'available': True, 'jitter_pct': round(jitter, 2), 'drop_count': drops, 'average_mbps': round(average, 2)}


def health_decline(samples):
    values = numeric_values(samples, 'health')
    if len(values) < 2:
        return {'available': False, 'decline_pct': None, 'per_sample_pct': None}
    decline = values[0] - values[-1]
    return {'available': True, 'decline_pct': round(decline, 3), 'per_sample_pct': round(decline / (len(values) - 1), 4), 'start_pct': values[0], 'end_pct': values[-1]}


def reliability_snapshot(task):
    samples = task.get('samples') or []
    threshold = task.get('threshold_temp') or 70
    return {'temperature_dwell': temperature_dwell(samples, threshold), 'latency_slo': latency_slo(samples), 'throughput_jitter': throughput_jitter(samples), 'health_decline': health_decline(samples)}
