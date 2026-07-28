"""HTML report renderer for the SSD stability analysis result."""
from __future__ import annotations

from html import escape
from typing import Any


def _text(value: Any, fallback: str = "--") -> str:
    if value in (None, "", "--"):
        return fallback
    return escape(str(value))


def _metric_value(metric: dict[str, Any], key: str, suffix: str = "") -> str:
    if not metric.get("available"):
        return "未采集"
    value = metric.get(key)
    return "--" if value is None else f"{value}{suffix}"


def _metric_card(title: str, metric: dict[str, Any], main_key: str, suffix: str, details: list[tuple[str, str]]) -> str:
    main = _metric_value(metric, main_key, suffix)
    body = "".join(f"<li><span>{escape(label)}</span><b>{escape(value)}</b></li>" for label, value in details)
    return f"""
    <article class=metric-card>
      <p>{escape(title)}</p><strong>{main}</strong>
      <small>{_text(metric.get('assessment'), '未参与分析')}</small>
      <ul>{body}</ul>
    </article>"""


def _event_rows(events: list[dict[str, Any]]) -> str:
    if not events:
        return "<tr><td colspan=3 class=empty>未记录任务事件。</td></tr>"
    return "".join(
        f"<tr><td>{_text(event.get('time'))}</td><td><span class='severity {_text(event.get('severity'))}'>{_text(event.get('severity'))}</span></td><td>{_text(event.get('text'))}</td></tr>"
        for event in events
    )


def _advanced_algorithm_section(advanced: dict[str, Any]) -> str:
    anomalies = advanced["anomalies"]
    projections = advanced["projections"]
    thermal = advanced["thermal"]
    baseline = advanced["historical_baseline"]
    anomaly_rows = "".join(
        f"<tr><td>{escape(label)}</td><td>{_text(data.get('count'), '0')}</td><td>{_text(data.get('max_deviation'), '--')}</td><td>{_text(data.get('assessment'))}</td></tr>"
        for label, data in (("温度", anomalies["temperature"]), ("P99 延迟", anomalies["latency"]), ("吞吐", anomalies["throughput"]))
    )
    projection_rows = "".join(
        f"<tr><td>{escape(label)}</td><td>{_text(data.get('slope_per_sample'))}</td><td>{_text(data.get('projected_end'))}</td><td>{_text(data.get('projected_change_pct'))}%</td><td>{_text(data.get('r_squared'))}</td><td>{_text(data.get('confidence'))}</td></tr>"
        for label, data in (("温度", projections["temperature"]), ("P99 延迟", projections["latency"]), ("吞吐", projections["throughput"]))
    )
    thermal_correlation = _text(thermal.get("correlation"), "未计算")
    baseline_throughput = _text(baseline.get("throughput_delta_pct"), "未对比")
    baseline_latency = _text(baseline.get("latency_delta_pct"), "未对比")
    return f"""
<section class=section><h2>高级算法诊断</h2>
  <div class=advanced-grid>
    <article class=algorithm-card><h3>EWMA + 3σ 突发异常检测</h3><p>以指数加权移动平均建立动态基线，识别偏离控制限的温度、尾延迟和吞吐采样点。</p><table class=algorithm-table><thead><tr><th>指标</th><th>异常点</th><th>最大偏离</th><th>判定</th></tr></thead><tbody>{anomaly_rows}</tbody></table></article>
    <article class=algorithm-card><h3>热稳定性关联分析</h3><p><b>温度—吞吐相关系数：</b>{thermal_correlation}</p><p><b>接近阈值次数：</b>{_text(thermal.get('near_limit_count'), '0')} / {_text(thermal.get('pair_count'), '0')} 个成对样本</p><p><b>判定：</b>{_text(thermal.get('assessment'))}</p></article>
    <article class=algorithm-card><h3>同配置历史基线</h3><p><b>可比历史任务：</b>{_text(baseline.get('count'), '0')} 条</p><p><b>平均吞吐偏差：</b>{baseline_throughput}%</p><p><b>平均 P99 偏差：</b>{baseline_latency}%</p><p><b>判定：</b>{_text(baseline.get('assessment'))}</p></article>
  </div>
  <article class='algorithm-card projection'><h3>线性回归趋势外推</h3><p>依据当前采样序列进行最小二乘拟合，预测当前测试若按既有趋势继续运行时的指标变化；仅反映本任务采样趋势，不代表 SSD 寿命预测。</p><table class=algorithm-table><thead><tr><th>指标</th><th>每样本斜率</th><th>预测结束值</th><th>相对起始变化</th><th>拟合 R²</th><th>置信度</th></tr></thead><tbody>{projection_rows}</tbody></table></article>
</section>"""


def build_report(task: dict[str, Any], analysis: dict[str, Any], generated_at: str) -> bytes:
    """Render a standalone, downloadable report. All task content is escaped."""
    metrics = analysis["metrics"]
    temperature = metrics["temperature"]
    latency = metrics["latency"]
    throughput = metrics["throughput"]
    health = metrics["health"]
    events = metrics["events"]
    advanced = analysis["advanced"]
    score_class = "score-good" if analysis["score"] >= 85 else "score-warn" if analysis["score"] >= 65 else "score-bad"
    risk_class = {"低风险": "low", "中风险": "medium", "高风险": "high"}.get(analysis["risk_level"], "pending")

    cards = "".join(
        [
            _metric_card(
                "温度与热余量", temperature, "max", "°C",
                [
                    ("平均温度", _metric_value(temperature, "avg", "°C")),
                    ("温度阈值", _text(temperature.get("threshold"), "--") + "°C"),
                    ("阈值余量", _text(temperature.get("headroom"), "--") + "°C"),
                    ("有效样本", _text(temperature.get("count"), "0")),
                ],
            ),
            _metric_card(
                "P99 延迟", latency, "p95", " ms",
                [
                    ("平均 P99", _metric_value(latency, "avg", " ms")),
                    ("最高 P99", _metric_value(latency, "max", " ms")),
                    ("延迟尖峰", _text(latency.get("spike_count"), "0") + " 次"),
                    ("有效样本", _text(latency.get("count"), "0")),
                ],
            ),
            _metric_card(
                "吞吐稳定性", throughput, "avg", " MB/s",
                [
                    ("最高吞吐", _metric_value(throughput, "max", " MB/s")),
                    ("首尾变化", _text(throughput.get("drift_pct"), "--") + "%"),
                    ("变异系数", _text(throughput.get("coefficient_variation"), "--") + "%"),
                    ("有效样本", _text(throughput.get("count"), "0")),
                ],
            ),
            _metric_card(
                "介质健康度", health, "min", "%",
                [
                    ("平均健康度", _metric_value(health, "avg", "%")),
                    ("最低健康度", _metric_value(health, "min", "%")),
                    ("有效样本", _text(health.get("count"), "0")),
                    ("严重事件", _text(events.get("counts", {}).get("严重"), "0") + " 条"),
                ],
            ),
        ]
    )
    deductions = "".join(f"<li>{_text(item)}</li>" for item in analysis["deductions"])
    limitations = "".join(f"<li>{_text(item)}</li>" for item in analysis["limitations"])
    if not limitations:
        limitations = "<li>已采集数据满足当前算法的基本分析条件。</li>"
    evidence_rows = "".join(
        f"<tr><th>{escape(title)}</th><td>{'<br>'.join(_text(item) for item in items)}</td></tr>"
        for title, items in (
            ("完成度", analysis["evidence"]["completion"]),
            ("温度", analysis["evidence"]["temperature"]),
            ("延迟", analysis["evidence"]["latency"]),
            ("吞吐", analysis["evidence"]["throughput"]),
            ("健康度", analysis["evidence"]["health"]),
            ("异常事件", analysis["evidence"]["events"]),
            ("高级算法", analysis["evidence"]["advanced"]),
        )
    )

    page = f"""<!doctype html>
<html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>SSD 稳定性与耐久测试分析报告</title>
<style>
:root{{--ink:#172033;--muted:#64748b;--line:#dde5ef;--blue:#2563eb;--soft:#f7faff;--good:#15803d;--warn:#a16207;--bad:#b91c1c}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fb;color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;line-height:1.6}}main{{max-width:1080px;margin:30px auto;padding:0 20px 36px}}.report{{background:#fff;border:1px solid var(--line);border-radius:15px;overflow:hidden;box-shadow:0 14px 36px #1e3a5f12}}header{{padding:34px 38px 28px;background:linear-gradient(135deg,#eff6ff,#fff)}}.eyebrow{{font-size:11px;font-weight:800;letter-spacing:1.4px;color:var(--blue);margin:0}}h1{{font-size:27px;line-height:1.25;margin:7px 0;color:#0f2c59}}h2{{font-size:18px;margin:0 0 13px}}h3{{font-size:14px;margin:0}}.meta{{color:var(--muted);margin:0}}.grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:18px;padding:24px 38px}}.card{{border:1px solid var(--line);border-radius:11px;padding:18px;background:#fff}}.info{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 22px;margin:0}}.info div{{border-bottom:1px dashed #e5eaf1;padding-bottom:8px}}.info dt{{color:var(--muted);font-size:11px}}.info dd{{margin:2px 0 0;font-weight:700;word-break:break-all}}.score{{display:flex;align-items:center;gap:17px}}.score-number{{width:94px;height:94px;border-radius:50%;display:grid;place-items:center;font-size:30px;font-weight:800}}.score-good{{background:#dcfce7;color:var(--good)}}.score-warn{{background:#fef3c7;color:var(--warn)}}.score-bad{{background:#fee2e2;color:var(--bad)}}.risk{{display:inline-block;border-radius:999px;padding:3px 9px;font-weight:800;font-size:11px}}.risk.low{{background:#dcfce7;color:var(--good)}}.risk.medium{{background:#fef3c7;color:var(--warn)}}.risk.high{{background:#fee2e2;color:var(--bad)}}.risk.pending{{background:#e8eef6;color:#52637d}}.notice{{margin:0 38px 22px;padding:12px 15px;background:#fff8e8;border:1px solid #fde3a2;border-radius:8px;color:#8a5307}}.section{{padding:0 38px 25px}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.metric-card,.algorithm-card{{border:1px solid var(--line);border-radius:10px;padding:14px;background:var(--soft)}}.metric-card p,.algorithm-card p{{margin:0 0 8px;color:#375376;font-size:11px}}.metric-card strong{{display:block;font-size:22px;margin:4px 0 0}}.metric-card small{{display:block;min-height:36px;color:#375376;font-size:11px}}.metric-card ul{{list-style:none;padding:9px 0 0;margin:9px 0 0;border-top:1px solid #dce8f8;font-size:11px}}.metric-card li{{display:flex;justify-content:space-between;gap:7px}}.metric-card li span{{color:var(--muted)}}.metric-card li b{{text-align:right}}.advanced-grid{{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:12px;margin-bottom:12px}}.projection{{margin-top:12px}}.algorithm-table{{width:100%;border-collapse:collapse;font-size:10px}}.algorithm-table th,.algorithm-table td{{padding:6px 4px;text-align:left;border-bottom:1px solid #dce8f8;vertical-align:top}}.algorithm-table th{{color:var(--muted);font-weight:700}}.evidence{{width:100%;border-collapse:collapse}}.evidence th,.evidence td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}.evidence th{{width:104px;color:#36527b;background:#f8fbff}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.list{{margin:0;padding-left:19px}}.list li{{margin:6px 0}}.events{{width:100%;border-collapse:collapse}}.events th,.events td{{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}}.events th{{color:var(--muted);font-size:11px}}.severity{{font-size:11px;font-weight:800;border-radius:999px;padding:3px 7px;background:#e8eef6;color:#52637d}}.severity.警告{{background:#fef3c7;color:var(--warn)}}.severity.严重{{background:#fee2e2;color:var(--bad)}}.empty{{color:var(--muted);text-align:center}}footer{{padding:16px 38px;background:#f8fafc;color:var(--muted);font-size:11px}}@media(max-width:760px){{main{{margin:0;padding:0}}.report{{border-radius:0}}header,.section{{padding-left:20px;padding-right:20px}}.grid,.columns,.advanced-grid{{grid-template-columns:1fr;padding-left:20px;padding-right:20px}}.notice{{margin-left:20px;margin-right:20px}}.metrics{{grid-template-columns:1fr 1fr}}footer{{padding-left:20px;padding-right:20px}}}}
</style>
<main><article class=report><header><p class=eyebrow>ENTERPRISE SSD · ANALYSIS REPORT</p><h1>企业级 SSD 稳定性与耐久测试分析报告</h1><p class=meta>生成时间（北京时间）：{_text(generated_at)}　|　分析算法：{_text(analysis['algorithm'])}</p></header>
<section class=grid><div class=card><h2>测试对象与配置</h2><dl class=info><div><dt>设备型号</dt><dd>{_text(task.get('device'))}</dd></div><div><dt>设备序列号</dt><dd>{_text(task.get('serial'))}</dd></div><div><dt>设备路径</dt><dd>{_text(task.get('path'))}</dd></div><div><dt>测试模式</dt><dd>{_text(task.get('mode'))}</dd></div><div><dt>测试策略</dt><dd>{_text(task.get('plan'))}</dd></div><div><dt>开始时间（北京时间）</dt><dd>{_text(task.get('started_at'), '未开始')}</dd></div><div><dt>压力参数</dt><dd>{_text(task.get('duration'))} 小时 / {_text(task.get('block_size'))} / QD {_text(task.get('queue_depth'))}</dd></div><div><dt>任务完成度</dt><dd>{_text(metrics['completion'].get('progress'))}% / {_text(metrics['completion'].get('status'))}</dd></div></dl></div>
<div class=card><h2>综合判定</h2><div class=score><div class='score-number {score_class}'>{analysis['score']}</div><div><p class=meta>稳定性综合评分（100 分制）</p><h3>{_text(analysis['conclusion'])}</h3><span class='risk {risk_class}'>{_text(analysis['risk_level'])}</span><p class=meta>{_text(analysis['risk_text'])}</p></div></div></div></section>
<p class=notice><b>判定说明：</b>{_text(analysis['mode_notice'])}</p>
<section class=section><h2>关键指标分析</h2><div class=metrics>{cards}</div></section>
{_advanced_algorithm_section(advanced)}
<section class=section><h2>算法判定依据</h2><table class=evidence><tbody>{evidence_rows}</tbody></table></section>
<section class='section columns'><div><h2>评分扣分项</h2><ul class=list>{deductions}</ul></div><div><h2>数据范围与限制</h2><ul class=list>{limitations}</ul></div></section>
<section class=section><h2>任务事件记录</h2><table class=events><thead><tr><th>时间（北京时间）</th><th>等级</th><th>事件内容</th></tr></thead><tbody>{_event_rows(task.get('events') or [])}</tbody></table></section>
<footer>评分方法：以 100 分为基础分，按任务完成度、温度阈值余量、P99 延迟、吞吐首尾变化/波动、介质健康度及事件等级进行可解释扣分。报告基于本任务已采集数据生成，不替代设备厂商规格或长期可靠性认证。</footer>
</article></main></html>"""
    return page.encode("utf-8")
