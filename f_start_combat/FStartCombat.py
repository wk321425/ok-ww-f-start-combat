import ctypes
import threading
import time

from ok import Logger, TriggerTask

logger = Logger.get_logger(__name__)

PLUGIN_VERSION = '1.0.1'


def _fix_imported_task_tab():
    """ok-script 2.1 起插件任务页继承 TasksPage, 名字写死成 "TasksPage", 和主任务页撞导航 key, 主窗口起不来。

    插件任务模块在建主窗口之前导入, 这里给它补个按插件区分的名字。两个插件都带这段, 只包一层。
    """
    try:
        from ok.ui.qt.plugins import imported_tabs
    except Exception:
        return  # 旧版没有这个模块
    cls = getattr(imported_tabs, 'ImportedTaskTab', None)
    if cls is None or getattr(cls.__init__, '_okww_unique_name', False):
        return
    original = cls.__init__

    def __init__(self, namespace, *args, **kwargs):
        original(self, namespace, *args, **kwargs)
        if self.objectName() == 'TasksPage':
            self.setObjectName(f'TasksPage_{namespace}')

    __init__._okww_unique_name = True
    cls.__init__ = __init__


_fix_imported_task_tab()

F_VK = 0x46
F_POLL_INTERVAL = 0.05
# 按下后超过「等待时间 + 这个数」还没用上就作废: 跑图、整理背包时按的 F 不该过一会儿突然开打
F_EXTRA_LIFETIME = 10.0
GRACE_LOG_INTERVAL = 10.0
GRACE_TARGET_INTERVAL = 0.3

# 共享状态挂在 CombatCheck 类上: 插件热更新会重新导入本模块, 模块变量清零, 老线程却还在跑
TS_ATTR = '_f_start_pressed_at'
WATCH_ATTR = '_f_start_watch_token'
TASK_ATTR = '_f_start_task'
ORIGINAL_ATTR = '_f_start_original'
OWNED_ATTR = '_f_start_owned'
# 下面三个是战斗任务的实例属性
CONSUMED_ATTR = '_f_start_consumed'
GRACE_ATTR = '_f_start_grace_since'
GRACE_LOG_ATTR = '_f_start_grace_log'

# 打补丁要用到的上游函数, 上游改名或删掉时插件不装, 只报错
PATCH_POINTS = {
    'src.combat.CombatCheck.CombatCheck': ('do_check_in_combat', 'has_target', 'target_enemy',
                                           'do_reset_to_false'),
    'src.task.AutoCombatTask.AutoCombatTask': ('enable', 'combat_end'),
}


def _classes():
    from src.combat.CombatCheck import CombatCheck
    from src.task.AutoCombatTask import AutoCombatTask
    return CombatCheck, AutoCombatTask


def _missing_patch_points():
    combat_check, auto_combat = _classes()
    owners = {'src.combat.CombatCheck.CombatCheck': combat_check,
              'src.task.AutoCombatTask.AutoCombatTask': auto_combat}
    return [f'{path}.{name}' for path, names in PATCH_POINTS.items() for name in names
            if not callable(getattr(owners[path], name, None))]


def _last_press(combat_task, cls):
    """最近一次物理 F 的时刻。部分自定义队伍代码自己也读 F, 写在实例的 _f_pressed_at 上, 取较新的。"""
    best = -1.0
    for value in (getattr(cls, TS_ATTR, -1.0), combat_task.__dict__.get('_f_pressed_at', -1.0)):
        try:
            best = max(best, float(value))
        except (TypeError, ValueError):
            pass
    return best


def start_f_watch(cls):
    """后台读物理 F 键, 按下的上升沿且游戏在前台时记时间。

    只读 GetAsyncKeyState 的高位: 低位整个进程共享、读一次就清零, 和别的读者会互相吃掉。
    ok-ww 自己发的键走 PostMessage, 不改变按键状态, 所以拾取、处决等脚本按的 F 不会记进来。
    """
    token = object()
    setattr(cls, WATCH_ATTR, token)
    if not hasattr(cls, TS_ATTR):
        setattr(cls, TS_ATTR, -1.0)

    def watch():
        try:
            get_key_state = ctypes.windll.user32.GetAsyncKeyState
            get_key_state.restype = ctypes.c_short
        except Exception as e:
            logger.error(f'F 开始战斗: 读不了键盘 ({e}), 按 F 开打不可用')
            return
        was_down = False
        while getattr(cls, WATCH_ATTR, None) is token:
            try:
                down = bool(get_key_state(F_VK) & 0x8000)
                if down and not was_down and _game_foreground(cls):
                    setattr(cls, TS_ATTR, time.time())
                    logger.debug('physical F pressed')
                was_down = down
                time.sleep(F_POLL_INTERVAL)
            except Exception:
                return

    threading.Thread(target=watch, name='f_start_combat_watch', daemon=True).start()


def stop_f_watch(cls):
    setattr(cls, WATCH_ATTR, None)


def _game_foreground(cls):
    task = getattr(cls, TASK_ATTR, None)
    try:
        hwnd = task.hwnd
        return hwnd is not None and hwnd.is_foreground()
    except Exception:
        return False


def _grace_seconds(combat_task):
    """F 开打后到第一次锁到目标之前的保护期已经撑了多久; 不在保护期返回 None。"""
    since = combat_task.__dict__.get(GRACE_ATTR, -1.0)
    if not combat_task._in_combat or since <= 0:
        return None
    return time.time() - since


def _clear_grace(combat_task):
    combat_task.__dict__[GRACE_ATTR] = -1.0


def _drop_pending_press(combat_task):
    combat_task.__dict__[CONSUMED_ATTR] = time.time()


def _try_force_combat(combat_task, cls):
    """按过 F 且等够了时间, 就把这次判定接管成「已进战斗」。"""
    task = getattr(cls, TASK_ATTR, None)
    if task is None or not task.enabled:
        return False
    pressed = _last_press(combat_task, cls)
    if pressed <= combat_task.__dict__.get(CONSUMED_ATTR, -1.0):
        return False
    waited = time.time() - pressed
    delay = task.f_start_delay
    if waited < delay:
        return False
    if waited > delay + F_EXTRA_LIFETIME:
        combat_task.__dict__[CONSUMED_ATTR] = pressed
        return False
    # 读不到队伍说明还在读条, 先不消耗这次按键, 下一帧再试
    if not combat_task.load_chars():
        return False
    combat_task.__dict__[CONSUMED_ATTR] = pressed
    if combat_task.is_open_world_auto_combat():
        # 大世界里捡东西、对话也按 F
        combat_task.log_info('F 开始战斗: 大世界里按的 F, 忽略')
        return False

    combat_task.has_lavitator = combat_task.find_one('edge_levitator', threshold=0.65)
    combat_task._in_combat = True
    now = time.time()
    combat_task.__dict__[GRACE_ATTR] = now
    combat_task.__dict__[GRACE_LOG_ATTR] = now
    # find_one 要排在中键前面: click() 结尾会 reset_scene(), 排后面就得多等一帧
    if combat_task.config.get('Auto Target', True):
        combat_task.middle_click()
    # 只对同一帧里接下来的 check_combat 有用; sleep_check 会先换帧重新判定, 那一次靠保护期撑住
    combat_task.scene.set_in_combat()
    combat_task.log_info(f'enter combat by physical F {combat_task.has_lavitator}')
    return True


def _wrap(owner, name, make_wrapper):
    """幂等地给 owner.name 套一层: 热更新重装时总是从最初的原函数重新包, 不会越叠越厚。"""
    current = getattr(owner, name)
    original = getattr(current, ORIGINAL_ATTR, current)
    owned = getattr(current, OWNED_ATTR, name in owner.__dict__)
    wrapper = make_wrapper(original)
    setattr(wrapper, ORIGINAL_ATTR, original)
    setattr(wrapper, OWNED_ATTR, owned)
    setattr(owner, name, wrapper)


def _unwrap(owner, name):
    current = owner.__dict__.get(name)
    if current is None or not hasattr(current, ORIGINAL_ATTR):
        return
    if getattr(current, OWNED_ATTR, True):
        setattr(owner, name, getattr(current, ORIGINAL_ATTR))
    else:
        # 原来是继承来的, 删掉覆盖层就回到父类的实现
        delattr(owner, name)


def install_patch():
    combat_check, auto_combat = _classes()

    def check_wrapper(original):
        def do_check_in_combat(self, *args, **kwargs):
            try:
                if isinstance(self, auto_combat) and not self._in_combat and _try_force_combat(self, combat_check):
                    return True
            except Exception as e:
                logger.error('F 开始战斗: 判定出错, 这一次按原规则走', e)
            return original(self, *args, **kwargs)

        return do_check_in_combat

    def has_target_wrapper(original):
        def has_target(self, *args, **kwargs):
            got = original(self, *args, **kwargs)
            if got:
                _clear_grace(self)
            return got

        return has_target

    def target_enemy_wrapper(original):
        def target_enemy(self, wait=True, *args, **kwargs):
            try:
                held = _grace_seconds(self) if wait else None
                if held is not None:
                    limit = _grace_limit(combat_check)
                    if limit <= 0 or held < limit:
                        # 保护期内只补点一下中键就放行, 不卡原来那 3 秒找怪, 宏照常出招
                        self.middle_click(interval=GRACE_TARGET_INTERVAL)
                        _log_holding(self, held)
                        return True
                    _clear_grace(self)
                    self.log_info(f'F 开始战斗: {limit:.0f} 秒还没锁到目标, 按原规则找怪')
            except Exception as e:
                logger.error('F 开始战斗: 保护期出错, 按原规则找怪', e)
            got = original(self, wait, *args, **kwargs)
            if got:
                _clear_grace(self)
            return got

        return target_enemy

    def reset_wrapper(original):
        def do_reset_to_false(self, *args, **kwargs):
            _clear_grace(self)
            return original(self, *args, **kwargs)

        return do_reset_to_false

    def enable_wrapper(original):
        def enable(self, *args, **kwargs):
            # 打开自动战斗之前按的 F 不算
            _drop_pending_press(self)
            return original(self, *args, **kwargs)

        return enable

    def combat_end_wrapper(original):
        def combat_end(self, *args, **kwargs):
            # 这场战斗里和之前按的 F(处决、捡东西) 不能再开一场
            _drop_pending_press(self)
            _clear_grace(self)
            return original(self, *args, **kwargs)

        return combat_end

    _wrap(combat_check, 'do_check_in_combat', check_wrapper)
    _wrap(combat_check, 'has_target', has_target_wrapper)
    _wrap(combat_check, 'target_enemy', target_enemy_wrapper)
    _wrap(combat_check, 'do_reset_to_false', reset_wrapper)
    _wrap(auto_combat, 'enable', enable_wrapper)
    _wrap(auto_combat, 'combat_end', combat_end_wrapper)


def uninstall_patch():
    combat_check, auto_combat = _classes()
    for name in PATCH_POINTS['src.combat.CombatCheck.CombatCheck']:
        _unwrap(combat_check, name)
    for name in PATCH_POINTS['src.task.AutoCombatTask.AutoCombatTask']:
        _unwrap(auto_combat, name)


def _grace_limit(cls):
    task = getattr(cls, TASK_ATTR, None)
    return task.grace_limit if task is not None else 0.0


def _log_holding(combat_task, held):
    now = time.time()
    if now - combat_task.__dict__.get(GRACE_LOG_ATTR, 0.0) < GRACE_LOG_INTERVAL:
        return
    combat_task.__dict__[GRACE_LOG_ATTR] = now
    combat_task.log_info(f'F 开始战斗: 还没锁到目标, 不判脱战({held:.0f}s)')


def _float_config(config, key, upper):
    try:
        return min(upper, max(0.0, float(config.get(key, 0) or 0)))
    except (TypeError, ValueError):
        return 0.0


class FStartCombatTask(TriggerTask):
    DELAY_KEY = 'F 之后等待(秒)'
    GRACE_KEY = '没锁到目标最多撑(秒)'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = 'F 开始战斗'
        self.description = ('在挑战/副本里按下 F 后, 不等锁定目标或血条出现就直接开始自动战斗。'
                            '大世界里按的 F 会被忽略。仅建议在矩阵/深塔/海墟等高难战斗本中使用')
        self.default_config['_enabled'] = True
        self.default_config.update({
            self.DELAY_KEY: 0.0,
            self.GRACE_KEY: 10.0,
        })
        self.config_description = {
            self.DELAY_KEY: '按下 F 到开始战斗之间等待的秒数, 用来对齐刷怪。0 = 按下就开始',
            self.GRACE_KEY: ('开打后怪还没出来时, 这段时间里不判脱战、不停下找怪, 宏照常出招; '
                             '超过就回到原规则(找 3 秒怪, 找不到算脱战)。0 = 一直撑到锁到目标'),
        }
        self.trigger_interval = 1
        self._installed = False
        self._failed_reason = None

    @property
    def f_start_delay(self):
        return _float_config(self.config, self.DELAY_KEY, 10.0)

    @property
    def grace_limit(self):
        return _float_config(self.config, self.GRACE_KEY, 600.0)

    def on_create(self):
        super().on_create()
        self._try_install(quiet=True)

    def run(self):
        # 必须返回假值: 返回真会让执行器重排触发任务并 reset_scene
        self._try_install(quiet=False)

    def on_destroy(self):
        if not self._installed:
            return
        try:
            combat_check, _ = _classes()
            if getattr(combat_check, TASK_ATTR, None) is self:
                uninstall_patch()
                stop_f_watch(combat_check)
                setattr(combat_check, TASK_ATTR, None)
            logger.info('F 开始战斗: 补丁已卸下')
        except Exception as e:
            logger.error('F 开始战斗: 卸载补丁出错', e)
        self._installed = False

    def _other_copy(self):
        executor = getattr(self, 'executor', None)
        for group in ('trigger_tasks', 'onetime_tasks'):
            for other in getattr(executor, group, None) or ():
                if other is not self and type(other).__name__ == type(self).__name__:
                    return other
        return None

    def _try_install(self, quiet):
        if self._installed or self._failed_reason:
            return
        try:
            combat_check, _ = _classes()
            if hasattr(combat_check, 'should_start_combat_by_key'):
                self._fail('主程序已经自带「按 F 开始战斗」, 插件不再生效, 请卸载插件')
                return
            missing = _missing_patch_points()
            if missing:
                self._fail(f'ok-ww 改了这些函数, 插件 v{PLUGIN_VERSION} 没装上, 请等插件更新: {missing}')
                return
            if self._other_copy() is not None:
                self._fail('还有另一份「F 开始战斗」(比如 ok_tasks/FStartCombatTask.py), 请删掉其中一份再重启')
                return
            setattr(combat_check, TASK_ATTR, self)
            install_patch()
            start_f_watch(combat_check)
        except Exception as e:
            if quiet:
                logger.debug(f'F 开始战斗: 启动时没装上, 稍后重试 ({e})')
            else:
                logger.error('F 开始战斗: 安装失败, 稍后重试', e)
            return
        self._installed = True
        logger.info(f'F 开始战斗 v{PLUGIN_VERSION}: 补丁已挂上')

    def _fail(self, reason):
        self._failed_reason = reason
        logger.error(f'F 开始战斗: {reason}')
