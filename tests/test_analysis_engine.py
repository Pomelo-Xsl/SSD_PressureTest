import unittest

from analysis_engine import analyze_task
from report_builder import build_report


def task_template(**changes):
    task = {
        "id": "task-001",
        "device": "Enterprise NVMe <test>",
        "serial": "SN-001",
        "path": "/dev/nvme1n1",
        "plan": "24 小时稳定性验证",
        "duration": 24,
        "block_size": "4K",
        "queue_depth": 64,
        "threshold_temp": 70,
        "mode": "真实 fio 裸盘",
        "status": "已完成",
        "progress": 100,
        "started_at": "2026-07-28 12:00:00",
        "samples": [
            {"temperature": 42, "p99": 4.0, "throughput": 2500, "health": 98},
            {"temperature": 43, "p99": 4.5, "throughput": 2480, "health": 98},
            {"temperature": 44, "p99": 4.2, "throughput": 2460, "health": 98},
            {"temperature": 45, "p99": 4.8, "throughput": 2450, "health": 98},
        ],
        "events": [{"time": "2026-07-28 12:00:00", "severity": "信息", "text": "测试完成"}],
    }
    task.update(changes)
    return task


class AnalysisEngineTests(unittest.TestCase):
    def test_completed_healthy_real_task_passes(self):
        report = analyze_task(task_template())
        self.assertEqual(report["conclusion"], "通过")
        self.assertEqual(report["risk_level"], "低风险")
        self.assertGreaterEqual(report["score"], 85)
        self.assertEqual(report["metrics"]["temperature"]["max"], 45.0)

    def test_overtemperature_and_critical_event_fail(self):
        task = task_template(
            samples=[
                {"temperature": 71, "p99": 60, "throughput": 2500, "health": 78},
                {"temperature": 74, "p99": 75, "throughput": 1600, "health": 78},
                {"temperature": 76, "p99": 90, "throughput": 1400, "health": 77},
            ],
            events=[{"time": "2026-07-28 12:10:00", "severity": "严重", "text": "温度超限"}],
        )
        report = analyze_task(task)
        self.assertEqual(report["conclusion"], "不通过")
        self.assertEqual(report["risk_level"], "高风险")
        self.assertLess(report["score"], 65)
        self.assertGreater(report["metrics"]["temperature"]["max"], 70)

    def test_incomplete_task_cannot_receive_final_acceptance(self):
        report = analyze_task(task_template(status="已中断", progress=27))
        self.assertEqual(report["conclusion"], "测试未完整结束，不能出具最终验收结论")
        self.assertEqual(report["risk_level"], "未定")

    def test_demo_report_is_explicitly_not_real_performance_acceptance(self):
        report = analyze_task(task_template(mode="安全演示"))
        self.assertEqual(report["conclusion"], "演示流程通过")
        self.assertTrue(report["limitations"])
        self.assertIn("安全演示", report["mode_notice"])

    def test_advanced_algorithms_detect_anomaly_and_compare_baseline(self):
        samples = [
            {"temperature": 40 + index, "p99": 4 + index * 0.1, "throughput": 2500 - index * 5, "health": 98}
            for index in range(7)
        ]
        samples.append({"temperature": 59, "p99": 31, "throughput": 1700, "health": 98})
        task = task_template(samples=samples, progress=100)
        history = [
            task_template(id=f"history-{index}", samples=[
                {"temperature": 42, "p99": 4.2, "throughput": 2500 + index * 10, "health": 98}
                for _ in range(8)
            ])
            for index in range(3)
        ]
        report = analyze_task(task, history)
        self.assertGreater(report["advanced"]["anomalies"]["latency"]["count"], 0)
        self.assertTrue(report["advanced"]["historical_baseline"]["available"])
        self.assertLess(report["advanced"]["historical_baseline"]["throughput_delta_pct"], 0)
        self.assertIn("advanced", report["evidence"])

    def test_report_escapes_device_content(self):
        task = task_template()
        analysis = analyze_task(task)
        html = build_report(task, analysis, "2026-07-28 12:30:00").decode("utf-8")
        self.assertIn("Enterprise NVMe &lt;test&gt;", html)
        self.assertIn("稳定性综合评分", html)
        self.assertIn("算法判定依据", html)
        self.assertIn("高级算法诊断", html)


if __name__ == "__main__":
    unittest.main()
