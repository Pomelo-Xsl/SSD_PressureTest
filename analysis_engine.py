from math import sqrt
from statistics import mean, median
from reliability_metrics import reliability_snapshot
SEVERITY_WEIGHT = {'信息': 0, '警告': 3, '严重': 12}

def _number(value):
    if isinstance(value, bool) or value in (None, '', '--'):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _series(samples, field):
    values = []
    for sample in samples:
        value = _number(sample.get(field))
        if value is not None:
            values.append(value)
    return values

def _percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower, upper = (int(position), min(int(position) + 1, len(ordered) - 1))
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

def _summary(values, unit, include_trend=True):
    if not values:
        return {'available': False, 'unit': unit, 'count': 0}
    average = mean(values)
    standard_deviation = sqrt(mean(((value - average) ** 2 for value in values)))
    data = {'available': True, 'unit': unit, 'count': len(values), 'min': round(min(values), 2), 'max': round(max(values), 2), 'avg': round(average, 2), 'median': round(median(values), 2), 'p95': round(_percentile(values, 95) or 0, 2), 'coefficient_variation': round(standard_deviation / average * 100, 2) if average else None}
    if include_trend:
        if len(values) < 2:
            data['slope'] = None
            data['drift_pct'] = None
        else:
            x_mean = (len(values) - 1) / 2
            y_mean = mean(values)
            denominator = sum(((index - x_mean) ** 2 for index in range(len(values))))
            slope = sum(((index - x_mean) * (value - y_mean) for index, value in enumerate(values))) / denominator
            baseline = mean(values[:max(1, len(values) // 4)])
            tail = mean(values[-max(1, len(values) // 4):])
            drift = None if baseline == 0 else (tail - baseline) / baseline * 100
            data['slope'] = round(slope, 4)
            data['drift_pct'] = round(drift, 2) if drift is not None else None
    return data

def _ewma_anomalies(values, absolute_floor, span=6):
    if len(values) < 6:
        return {'available': False, 'count': 0, 'indices': [], 'assessment': '样本不足，未执行 EWMA 异常检测'}
    alpha = 2 / (span + 1)
    ewma = values[0]
    residuals = []
    indices = []
    max_deviation = 0.0
    for index, value in enumerate(values[1:], 1):
        if len(residuals) >= 3:
            residual_mean = mean(residuals)
            sigma = sqrt(mean(((item - residual_mean) ** 2 for item in residuals)))
            control_limit = max(absolute_floor, sigma * 3)
            deviation = abs(value - ewma)
            max_deviation = max(max_deviation, deviation)
            if deviation > control_limit:
                indices.append(index)
        residuals.append(value - ewma)
        ewma = alpha * value + (1 - alpha) * ewma
    assessment = '未发现突发异常' if not indices else '发现 {0} 个 EWMA 异常点'.format(len(indices))
    return {'available': True, 'count': len(indices), 'indices': indices, 'max_deviation': round(max_deviation, 2), 'assessment': assessment}

def _linear_projection(values, progress):
    if len(values) < 4 or progress <= 0:
        return {'available': False, 'assessment': '样本或完成度不足，未进行趋势预测'}
    x_values = list(range(len(values)))
    x_mean, y_mean = (mean(x_values), mean(values))
    denominator = sum(((value - x_mean) ** 2 for value in x_values))
    slope = sum(((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values))) / denominator
    intercept = y_mean - slope * x_mean
    estimated_total = max(len(values), min(10000, round(len(values) * 100 / min(progress, 100))))
    projected_end = intercept + slope * (estimated_total - 1)
    baseline = mean(values[:max(1, len(values) // 4)])
    projected_change = None if baseline == 0 else (projected_end - baseline) / baseline * 100
    fitted = [intercept + slope * x for x in x_values]
    total_variance = sum(((value - y_mean) ** 2 for value in values))
    residual_variance = sum(((value - fit) ** 2 for value, fit in zip(values, fitted)))
    r_squared = None if not total_variance else max(0, 1 - residual_variance / total_variance)
    confidence = '低'
    if r_squared is not None and r_squared >= 0.7 and (len(values) >= 8):
        confidence = '中'
    if r_squared is not None and r_squared >= 0.85 and (len(values) >= 16):
        confidence = '高'
    return {'available': True, 'slope_per_sample': round(slope, 4), 'projected_end': round(projected_end, 2), 'projected_change_pct': round(projected_change, 2) if projected_change is not None else None, 'r_squared': round(r_squared, 3) if r_squared is not None else None, 'confidence': confidence, 'assessment': '基于当前采样的线性趋势外推；非设备寿命预测。'}

def _thermal_stability(samples, threshold):
    temperatures = []
    throughput = []
    for sample in samples:
        temperature = _number(sample.get('temperature'))
        sample_throughput = _number(sample.get('throughput'))
        if temperature is not None and sample_throughput is not None:
            temperatures.append(temperature)
            throughput.append(sample_throughput)
    if not temperatures:
        return {'available': False, 'assessment': '缺少成对温度与吞吐数据，无法分析热稳定性'}
    near_limit = sum((value >= threshold - 5 for value in temperatures))
    correlation = None
    if len(temperatures) >= 4 and len(temperatures) == len(throughput):
        temperature_mean = mean(temperatures)
        throughput_mean = mean(throughput)
        numerator = sum(((temperature - temperature_mean) * (value - throughput_mean) for temperature, value in zip(temperatures, throughput)))
        temperature_size = sqrt(sum(((temperature - temperature_mean) ** 2 for temperature in temperatures)))
        throughput_size = sqrt(sum(((value - throughput_mean) ** 2 for value in throughput)))
        if temperature_size and throughput_size:
            correlation = numerator / (temperature_size * throughput_size)
    suspected = bool(correlation is not None and correlation <= -0.65 and (near_limit > 0))
    if suspected:
        assessment = '温度升高与吞吐下降呈强负相关，存在疑似温控降速'
    elif near_limit:
        assessment = '存在接近温度阈值的采样，但未发现明确温控降速关联'
    elif correlation is None:
        assessment = '成对样本不足，未计算温度—吞吐相关性'
    else:
        assessment = '未发现温度升高导致吞吐下降的明显关联'
    return {'available': True, 'pair_count': len(temperatures), 'near_limit_count': near_limit, 'correlation': round(correlation, 3) if correlation is not None else None, 'suspected_throttling': suspected, 'assessment': assessment}

def _historical_baseline(task, history):
    if not history:
        return {'available': False, 'count': 0, 'assessment': '暂无历史任务，未建立同配置基线'}
    match_fields = ('path', 'mode', 'block_size', 'queue_depth', 'io_pattern', 'read_ratio')
    candidates = []
    for item in history:
        if item.get('id') == task.get('id'):
            continue
        if item.get('status') != '已完成':
            continue

        matches_current_config = True
        for field in match_fields:
            if item.get(field) != task.get(field):
                matches_current_config = False
                break
        if matches_current_config:
            candidates.append(item)
    if len(candidates) < 3:
        return {'available': False, 'count': len(candidates), 'assessment': '同设备同配置历史任务仅 {0} 条，至少需要 3 条'.format(len(candidates))}
    throughput_baseline = []
    latency_baseline = []
    for item in candidates:
        historical_samples = item.get('samples') or []
        throughput_values = _series(historical_samples, 'throughput')
        latency_values = _series(historical_samples, 'p99')
        if throughput_values:
            throughput_baseline.append(mean(throughput_values))
        if latency_values:
            latency_baseline.append(mean(latency_values))
    current_throughput = _series(task.get('samples') or [], 'throughput')
    current_latency = _series(task.get('samples') or [], 'p99')
    if not throughput_baseline and (not latency_baseline):
        return {'available': False, 'count': len(candidates), 'assessment': '历史任务缺少可比遥测数据'}
    result = {'available': True, 'count': len(candidates), 'assessment': '与同设备、同模式、同压力参数的历史任务对比'}
    if throughput_baseline and current_throughput:
        baseline = median(throughput_baseline)
        current = mean(current_throughput)
        result['throughput_baseline'] = round(baseline, 2)
        result['throughput_delta_pct'] = round((current - baseline) / baseline * 100, 2) if baseline else None
    if latency_baseline and current_latency:
        baseline = median(latency_baseline)
        current = mean(current_latency)
        result['latency_baseline'] = round(baseline, 2)
        result['latency_delta_pct'] = round((current - baseline) / baseline * 100, 2) if baseline else None
    return result

def _deduct(score, points, reasons, message):
    if points > 0:
        reasons.append('-{0} 分：{1}'.format(points, message))
    return max(0, score - points)

def _temperature_assessment(values, threshold):
    metric = _summary(values, '°C')
    reasons = []
    if not values:
        metric['assessment'] = '未采集到温度数据'
        return (0, metric, reasons)
    maximum, average = (max(values), mean(values))
    headroom = threshold - maximum
    metric.update({'threshold': threshold, 'headroom': round(headroom, 2)})
    penalty = 0
    if maximum >= threshold:
        penalty = 28
        metric['assessment'] = '温度超过配置阈值'
        reasons.append('最高温度 {0:.1f}°C 达到或超过阈值 {1:.1f}°C'.format(maximum, threshold))
    elif headroom < 3:
        penalty = 15
        metric['assessment'] = '温度接近阈值'
        reasons.append('最高温度 {0:.1f}°C，距阈值仅 {1:.1f}°C'.format(maximum, headroom))
    elif headroom < 8:
        penalty = 7
        metric['assessment'] = '温度存在余量但需关注'
        reasons.append('最高温度 {0:.1f}°C，温度余量 {1:.1f}°C'.format(maximum, headroom))
    else:
        metric['assessment'] = '温度余量充足'
        reasons.append('最高温度 {0:.1f}°C，低于阈值 {1:.1f}°C'.format(maximum, headroom))
    if metric.get('slope') is not None and metric['slope'] > 1.5:
        penalty += 5
        reasons.append('温度采样序列呈持续上升趋势')
    if average >= threshold - 5 and maximum < threshold:
        penalty += 3
        reasons.append('平均温度 {0:.1f}°C 接近阈值'.format(average))
    return (min(penalty, 35), metric, reasons)

def _latency_assessment(values):
    metric = _summary(values, 'ms')
    reasons = []
    if not values:
        metric['assessment'] = '未采集到有效 P99 延迟'
        return (0, metric, reasons)
    p95, average, maximum = (metric['p95'], metric['avg'], metric['max'])
    baseline = median(values)
    spikes = sum((value > max(20, baseline * 2.5) for value in values))
    metric['spike_count'] = spikes
    penalty = 0
    if p95 >= 50:
        penalty = 22
        metric['assessment'] = '尾延迟严重偏高'
    elif p95 >= 20:
        penalty = 12
        metric['assessment'] = '尾延迟偏高'
    elif p95 >= 10:
        penalty = 5
        metric['assessment'] = '尾延迟可接受但存在波动'
    else:
        metric['assessment'] = '尾延迟稳定'
    reasons.append('P99 延迟均值 {0:.2f} ms，P95 {1:.2f} ms，最高 {2:.2f} ms'.format(average, p95, maximum))
    if spikes:
        spike_penalty = min(8, spikes * 2)
        penalty += spike_penalty
        reasons.append('检测到 {0} 个明显 P99 延迟尖峰'.format(spikes))
    if metric.get('coefficient_variation') is not None and metric['coefficient_variation'] > 60:
        penalty += 4
        reasons.append('P99 延迟离散度较高')
    return (min(penalty, 30), metric, reasons)

def _throughput_assessment(values):
    metric = _summary(values, 'MB/s')
    reasons = []
    if not values:
        metric['assessment'] = '未采集到有效吞吐数据'
        return (0, metric, reasons)
    drift = metric.get('drift_pct')
    penalty = 0
    if drift is not None and drift <= -30:
        penalty = 16
        metric['assessment'] = '吞吐衰减明显'
    elif drift is not None and drift <= -15:
        penalty = 8
        metric['assessment'] = '吞吐存在衰减'
    elif metric.get('coefficient_variation') is not None and metric['coefficient_variation'] > 35:
        penalty = 6
        metric['assessment'] = '吞吐波动较大'
    else:
        metric['assessment'] = '吞吐趋势稳定'
    drift_text = '数据不足' if drift is None else '{0:+.1f}%'.format(drift)
    reasons.append('平均吞吐 {0:.1f} MB/s，首尾分段变化 {1}'.format(metric['avg'], drift_text))
    return (penalty, metric, reasons)

def _health_assessment(values):
    metric = _summary(values, '%', include_trend=False)
    reasons = []
    if not values:
        metric['assessment'] = '未采集到介质健康度'
        return (0, metric, reasons)
    lowest = min(values)
    penalty = 0
    if lowest < 80:
        penalty = 18
        metric['assessment'] = '介质健康度偏低'
    elif lowest < 90:
        penalty = 8
        metric['assessment'] = '介质健康度需关注'
    else:
        metric['assessment'] = '介质健康度正常'
    reasons.append('最低介质健康度 {0:.1f}%'.format(lowest))
    return (penalty, metric, reasons)

def _event_assessment(events):
    counts = {}
    for severity in SEVERITY_WEIGHT:
        counts[severity] = 0
    for event in events:
        severity = event.get('severity', '信息')
        counts[severity] = counts.get(severity, 0) + 1
    penalty = min(30, sum((counts.get(severity, 0) * weight for severity, weight in SEVERITY_WEIGHT.items())))
    reasons = []
    if counts.get('严重'):
        reasons.append('记录到 {0} 条严重事件'.format(counts['严重']))
    if counts.get('警告'):
        reasons.append('记录到 {0} 条警告事件'.format(counts['警告']))
    if not reasons:
        reasons.append('未记录警告或严重事件')
    return (penalty, {'counts': counts, 'assessment': '存在异常事件' if penalty else '未发现告警事件'}, reasons)

def analyze_task(task, history=None):
    samples = task.get('samples') or []
    events = task.get('events') or []
    temperature_values = _series(samples, 'temperature')
    latency_values = _series(samples, 'p99')
    throughput_values = _series(samples, 'throughput')
    health_values = _series(samples, 'health')
    threshold = _number(task.get('threshold_temp')) or 70
    progress = _number(task.get('progress')) or 0
    status = task.get('status', '未知')
    if status == '已完成' and progress >= 100:
        completion_penalty = 0
        completion = {'status': status, 'progress': progress, 'assessment': '已完成'}
        completion_reasons = ['任务已完成全部配置时长']
    elif status in {'已停止', '已中断', '失败'}:
        completion_penalty = 45
        completion = {'status': status, 'progress': progress, 'assessment': '未完整执行'}
        completion_reasons = ['任务状态为“{0}”，仅完成 {1:.1f}%'.format(status, progress)]
    elif status in {'运行中', '停止中', '排队中'}:
        completion_penalty = 25
        completion = {'status': status, 'progress': progress, 'assessment': '结果尚未定稿'}
        completion_reasons = ['任务仍处于“{0}”状态，当前进度 {1:.1f}%'.format(status, progress)]
    else:
        completion_penalty = 35
        completion = {'status': status, 'progress': progress, 'assessment': '状态未知'}
        completion_reasons = ['任务状态“{0}”无法作为完成依据'.format(status)]
    temperature_penalty, temperature, temperature_reasons = _temperature_assessment(temperature_values, threshold)
    latency_penalty, latency, latency_reasons = _latency_assessment(latency_values)
    throughput_penalty, throughput, throughput_reasons = _throughput_assessment(throughput_values)
    health_penalty, health, health_reasons = _health_assessment(health_values)
    event_penalty, events_metric, event_reasons = _event_assessment(events)
    anomalies = {'temperature': _ewma_anomalies(temperature_values, absolute_floor=3), 'latency': _ewma_anomalies(latency_values, absolute_floor=2), 'throughput': _ewma_anomalies(throughput_values, absolute_floor=150)}
    projections = {'temperature': _linear_projection(temperature_values, progress), 'latency': _linear_projection(latency_values, progress), 'throughput': _linear_projection(throughput_values, progress)}
    thermal = _thermal_stability(samples, threshold)
    historical = _historical_baseline(task, history)
    anomaly_count = sum((item['count'] for item in anomalies.values() if item.get('available')))
    anomaly_penalty = min(12, anomaly_count * 2)
    thermal_penalty = 6 if thermal.get('suspected_throttling') else 0
    baseline_penalty = 0
    if historical.get('available'):
        throughput_delta = historical.get('throughput_delta_pct')
        latency_delta = historical.get('latency_delta_pct')
        if throughput_delta is not None and throughput_delta <= -20:
            baseline_penalty += 6
        if latency_delta is not None and latency_delta >= 30:
            baseline_penalty += 6
        baseline_penalty = min(10, baseline_penalty)
    score = 100
    deductions = []
    score = _deduct(score, completion_penalty, deductions, completion_reasons[0])
    score = _deduct(score, temperature_penalty, deductions, temperature.get('assessment', '温度评估'))
    score = _deduct(score, latency_penalty, deductions, latency.get('assessment', '延迟评估'))
    score = _deduct(score, throughput_penalty, deductions, throughput.get('assessment', '吞吐评估'))
    score = _deduct(score, health_penalty, deductions, health.get('assessment', '健康度评估'))
    score = _deduct(score, event_penalty, deductions, events_metric.get('assessment', '事件评估'))
    score = _deduct(score, anomaly_penalty, deductions, 'EWMA 检测到 {0} 个突发异常点'.format(anomaly_count))
    score = _deduct(score, thermal_penalty, deductions, thermal.get('assessment', '热稳定性评估'))
    score = _deduct(score, baseline_penalty, deductions, '相较同配置历史基线存在显著性能偏差')
    complete = completion['assessment'] == '已完成'
    mode = task.get('mode', '')
    is_demo = mode == '安全演示'
    has_critical = events_metric['counts'].get('严重', 0) > 0
    if not complete:
        risk_level = '未定'
        risk_text = '任务未完整结束，当前分析仅反映已采集阶段。'
    elif has_critical or score < 65:
        risk_level = '高风险'
        risk_text = '存在明显稳定性或执行风险，建议复测并排查设备与环境。'
    elif score < 85:
        risk_level = '中风险'
        risk_text = '测试已完成，但存在需要关注的风险指标。'
    else:
        risk_level = '低风险'
        risk_text = '已采集指标未发现需要阻断验收的明显风险。'
    limitations = []
    if is_demo:
        limitations.append('本任务为安全演示，不向目标 SSD 写入数据；性能数值仅用于验证系统流程，不能作为真实盘性能验收依据。')
    if len(samples) < 3:
        limitations.append('有效遥测样本少于 3 条，趋势判断可信度有限。')
    missing = [name for name, values in (('温度', temperature_values), ('P99 延迟', latency_values), ('吞吐', throughput_values), ('介质健康度', health_values)) if not values]
    if missing:
        limitations.append('未采集到' + '、'.join(missing) + '的有效数据，相关指标未参与扣分。')
    if not historical.get('available'):
        limitations.append(historical['assessment'] + '，未对评分施加历史基线偏差。')
    if any((not item.get('available') for item in anomalies.values())):
        limitations.append('部分指标样本不足 6 条，未执行对应的 EWMA 异常检测。')
    if not complete:
        conclusion = '测试未完整结束，不能出具最终验收结论'
    elif is_demo:
        conclusion = '演示流程' + ('通过' if score >= 70 else '存在预警')
    elif score >= 85 and (not has_critical):
        conclusion = '通过'
    elif score >= 65:
        conclusion = '预警'
    else:
        conclusion = '不通过'
    evidence = {'completion': completion_reasons, 'temperature': temperature_reasons, 'latency': latency_reasons, 'throughput': throughput_reasons, 'health': health_reasons, 'events': event_reasons, 'advanced': ['EWMA 异常检测：温度 {0} 个、延迟 {1} 个、吞吐 {2} 个异常点。'.format(anomalies['temperature']['count'], anomalies['latency']['count'], anomalies['throughput']['count']), thermal['assessment'], historical['assessment']]}
    reliability = reliability_snapshot(task)
    return {'algorithm': 'SSD Stability Score v2.0', 'score': score, 'risk_level': risk_level, 'risk_text': risk_text, 'conclusion': conclusion, 'mode_notice': '安全演示结果仅用于系统流程验证' if is_demo else '真实 fio 结果可用于本次配置下的压力测试评价', 'deductions': deductions or ['未触发评分扣分项'], 'evidence': evidence, 'limitations': limitations, 'metrics': {'completion': completion, 'temperature': temperature, 'latency': latency, 'throughput': throughput, 'health': health, 'events': events_metric}, 'advanced': {'anomalies': anomalies, 'projections': projections, 'thermal': thermal, 'historical_baseline': historical}, 'reliability': reliability}
