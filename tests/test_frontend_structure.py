import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendStructureTests(unittest.TestCase):

    def setUp(self):
        self.html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        self.script = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
        self.server = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.styles = (ROOT / 'static' / 'styles.css').read_text(encoding='utf-8')

    def test_navigation_targets_are_separate_work_pages(self):
        for page in ('dashboard', 'monitoring', 'devices', 'strategy', 'tasks', 'operations'):
            self.assertIn('data-page="{0}"'.format(page), self.html)
            self.assertIn('href="#{0}"'.format(page), self.html)
        self.assertEqual(self.html.count('class="app-page"'), 6)

    def test_device_logs_and_task_governance_have_dedicated_page_content(self):
        self.assertIn('NVMe 盘片日志与扩展 SMART', self.html)
        self.assertIn('实时监测', self.html)
        self.assertIn('任务记录', self.html)
        self.assertIn('运维管理', self.html)
        self.assertIn('id="deviceCards"', self.html)
        self.assertIn('id="tasksBody"', self.html)

    def test_hash_router_exposes_explicit_navigation(self):
        self.assertIn('function applyRoute()', self.script)
        self.assertIn('function navigateTo(page)', self.script)
        self.assertIn("window.addEventListener('hashchange'", self.script)

    def test_alert_acknowledgement_uses_local_update_before_background_sync(self):
        start = self.script.index('async function acknowledgeAlert')
        end = self.script.index('async function loadOperations', start)
        acknowledgement = self.script[start:end]
        self.assertIn('renderOperationsFromState()', acknowledgement)
        self.assertIn("api('/api/alerts').then", acknowledgement)
        self.assertNotIn('refresh()', acknowledgement)

    def test_terminal_tasks_offer_a_delete_action(self):
        self.assertIn("['已完成','已停止','失败','已中断'].includes(t.status)", self.script)
        self.assertIn('function openTaskDelete', self.script)
        self.assertIn('oncontextmenu="openTaskDelete', self.script)
        self.assertNotIn('onclick="deleteTask', self.script)
        self.assertNotIn('function beginTaskSwipe', self.script)
        self.assertNotIn('function endTaskSwipe', self.script)
        self.assertIn("/api/tasks/${id}/delete", self.script)

    def test_runtime_assets_disable_stale_browser_cache(self):
        self.assertIn("Cache-Control', 'no-store, max-age=0, must-revalidate", self.server)
        self.assertIn("'/app.js'", self.server)

    def test_task_table_fills_page_without_horizontal_scroll(self):
        self.assertIn('.task-panel .table-wrap{overflow:visible}', self.styles)
        self.assertIn('.task-panel table{min-width:0;table-layout:fixed}', self.styles)
        self.assertIn("td:nth-child(6){width:12%}", self.styles)
        self.assertIn("td:nth-child(7){width:6%;white-space:nowrap}", self.styles)
        self.assertIn('.task-panel .progress{width:78px', self.styles)

    def test_task_delete_has_no_visible_hint(self):
        self.assertNotIn('右键删除此历史任务', self.script)


if __name__ == '__main__':
    unittest.main()
