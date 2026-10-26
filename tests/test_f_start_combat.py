"""在 ok-ww 源码目录下运行: python <本文件> <ok-ww 源码目录> <.okscript 路径>"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

OK_WW = os.path.abspath(sys.argv[1])
OKSCRIPT = os.path.abspath(sys.argv[2])
del sys.argv[1:3]
os.chdir(OK_WW)
sys.path.insert(0, OK_WW)

from ok.core.plugins import import_plugin_class, prepare_local_plugin  # noqa: E402
from ok.core.script_packager import import_script  # noqa: E402
from src.combat.CombatCheck import CombatCheck  # noqa: E402
from src.scene.WWScene import WWScene  # noqa: E402
from src.task.AutoCombatTask import AutoCombatTask  # noqa: E402

IMPORT_BASE = tempfile.mkdtemp(prefix='f_start_test_')
ok, message, FOLDER = import_script(OKSCRIPT, import_base=IMPORT_BASE)
assert ok, message
MANIFEST = prepare_local_plugin(OKSCRIPT).manifest
PLUGIN_CLASS = import_plugin_class(FOLDER, MANIFEST['tasks'][0])
plugin = sys.modules[PLUGIN_CLASS.__module__]

ORIGINALS = {name: CombatCheck.__dict__[name] for name in
             ('do_check_in_combat', 'has_target', 'target_enemy', 'do_reset_to_false')}


class TestFStartCombatPlugin(unittest.TestCase):
    def setUp(self):
        self.scene = WWScene()
        executor = Mock(scene=self.scene, debug=False, trigger_tasks=[], onetime_tasks=[])
        self.plugin = PLUGIN_CLASS(executor, None)
        self.plugin.config = dict(self.plugin.default_config)
        self.plugin._enabled = True
        self.plugin._executor = executor
        with patch.object(plugin, 'start_f_watch'):
            self.plugin._try_install(quiet=False)
        self.assertTrue(self.plugin._installed)

        self.task = AutoCombatTask(executor, None)
        self.task.scene = self.scene
        self.task.config = dict(self.task.default_config)
        self.task.load_chars = Mock(return_value=True)
        self.task.find_one = Mock(return_value=None)
        self.task.middle_click = Mock()
        self.task.is_open_world_auto_combat = Mock(return_value=False)
        self.task.has_target = Mock(return_value=False)
        self.task.check_health_bar = Mock(return_value=False)
        self.task.check_f_break = Mock()
        self.task.get_current_char = Mock(return_value=None)
        self.task.next_frame = Mock()
        self.task.target_enemy_time_out = 0.05

    def tearDown(self):
        self.plugin.on_destroy()
        CombatCheck._f_start_pressed_at = -1.0

    def press_f(self, seconds_ago=0.0):
        CombatCheck._f_start_pressed_at = time.time() - seconds_ago

    def check(self):
        self.scene.reset()
        return self.task.do_check_in_combat(False)

    def test_press_starts_combat_once(self):
        self.press_f(0.1)
        self.assertTrue(self.check())
        self.assertTrue(self.task._in_combat)
        self.task.middle_click.assert_called_once()

        self.task._in_combat = False
        self.assertFalse(self.check())

    def test_no_press_keeps_original_rules(self):
        self.assertFalse(self.check())
        self.assertFalse(self.task._in_combat)

    def test_waits_for_delay(self):
        self.plugin.config[PLUGIN_CLASS.DELAY_KEY] = 2.0
        self.press_f(0.5)
        self.assertFalse(self.check())
        CombatCheck._f_start_pressed_at -= 2.0
        self.assertTrue(self.check())

    def test_stale_press_is_dropped(self):
        self.press_f(plugin.F_EXTRA_LIFETIME + 1)
        self.assertFalse(self.check())

    def test_press_waits_until_team_is_loaded(self):
        self.press_f(0.1)
        self.task.load_chars.return_value = None
        self.assertFalse(self.check())
        self.task.load_chars.return_value = True
        self.assertTrue(self.check())

    def test_open_world_press_is_ignored(self):
        self.task.is_open_world_auto_combat.return_value = True
        self.press_f(0.1)
        self.assertFalse(self.check())
        self.task.is_open_world_auto_combat.return_value = False
        self.assertFalse(self.check())

    def test_plugin_switched_off(self):
        self.plugin._enabled = False
        self.press_f(0.1)
        self.assertFalse(self.check())

    def test_auto_target_off_does_not_middle_click(self):
        self.task.config['Auto Target'] = False
        self.press_f(0.1)
        self.assertTrue(self.check())
        self.task.middle_click.assert_not_called()

    def test_grace_keeps_combat_without_blocking(self):
        self.press_f(0.1)
        self.assertTrue(self.check())
        start = time.time()
        for _ in range(3):
            self.assertTrue(self.check())
        self.assertLess(time.time() - start, 0.04)
        self.assertTrue(self.task._in_combat)

    def test_grace_limit_falls_back_to_original_rules(self):
        self.plugin.config[PLUGIN_CLASS.GRACE_KEY] = 1.0
        self.press_f(0.1)
        self.assertTrue(self.check())
        self.task._f_start_grace_since -= 2.0
        self.assertFalse(self.check())
        self.assertFalse(self.task._in_combat)

    def test_grace_ends_after_first_lock(self):
        # has_target 要走类上的补丁层才会结束保护期, 所以这里换掉的是被包的那个原函数
        self.plugin.on_destroy()
        del self.task.has_target
        self.task.locked = False
        with patch.object(CombatCheck, 'has_target', lambda task, *args, **kwargs: task.locked):
            with patch.object(plugin, 'start_f_watch'):
                self.plugin._try_install(quiet=False)
            self.press_f(0.1)
            self.assertTrue(self.check())
            self.task.locked = True
            self.assertTrue(self.check())
            self.task.locked = False
            self.assertFalse(self.check())

    def test_combat_end_drops_presses(self):
        self.press_f(0.1)
        self.task.combat_end()
        self.assertFalse(self.check())

    def test_enable_drops_earlier_presses(self):
        self.press_f(0.1)
        self.task._enabled = False
        self.task.enable()
        self.assertFalse(self.check())

    def test_uninstall_restores_upstream(self):
        self.plugin.on_destroy()
        for name, original in ORIGINALS.items():
            self.assertIs(CombatCheck.__dict__[name], original)
        self.assertNotIn('enable', AutoCombatTask.__dict__)
        self.assertNotIn('combat_end', AutoCombatTask.__dict__)

    def test_reinstall_does_not_stack(self):
        with patch.object(plugin, 'start_f_watch'):
            plugin.install_patch()
        self.assertIs(CombatCheck.do_check_in_combat._f_start_original, ORIGINALS['do_check_in_combat'])

    def test_refuses_when_upstream_has_native_feature(self):
        self.plugin.on_destroy()
        CombatCheck.should_start_combat_by_key = lambda self: False
        try:
            other = PLUGIN_CLASS(self.plugin.executor, None)
            other.config = dict(other.default_config)
            other._executor = self.plugin.executor
            other._try_install(quiet=False)
            self.assertFalse(other._installed)
            self.assertIs(CombatCheck.__dict__['do_check_in_combat'], ORIGINALS['do_check_in_combat'])
        finally:
            del CombatCheck.should_start_combat_by_key

    def test_refuses_second_copy(self):
        self.plugin.on_destroy()
        other = PLUGIN_CLASS(self.plugin.executor, None)
        other.config = dict(other.default_config)
        other._executor = self.plugin.executor
        self.plugin.executor.trigger_tasks = [self.plugin, other]
        other._try_install(quiet=False)
        self.assertFalse(other._installed)


class TestWatcher(unittest.TestCase):
    def test_foreground_errors_count_as_background(self):
        CombatCheck._f_start_task = Mock(hwnd=Mock(is_foreground=Mock(side_effect=RuntimeError)))
        try:
            self.assertFalse(plugin._game_foreground(CombatCheck))
        finally:
            CombatCheck._f_start_task = None


if __name__ == '__main__':
    with patch('ok.task.TaskExecutor.TaskExecutor.init_default_ocr', lambda self: None):
        unittest.main(verbosity=2)
