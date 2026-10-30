# -*- coding: utf-8 -*-
"""输入法中/英切换（imemanage）回归护栏（2026-10-30）。

背景：矫正界面按 Esc / Ctrl+[ 进 NORMAL 后要接着敲 j/k/h/l，中文输入法会把这些字母
当拼音吞掉。浏览器无法切输入法，所以由本地服务端调 Win32 接口——但用户明确要求
**只切输入法内部的中/英模式**（搜狗/百度/微软拼音），**不能**换成「美式键盘」。

覆盖的不变量：
  I1 只走 IMM32 转换模式（WM_IME_CONTROL + IMC_GET/SETCONVERSIONMODE），
     代码里不得出现 WM_INPUTLANGCHANGEREQUEST / LoadKeyboardLayout（换键盘布局）。
  I2 已经是英文模式 → 什么都不做，绝不把英文切回中文。
  I3 读不到状态 → 不盲切（盲按 Shift 可能反向切换）。
  I4 转换模式消息不生效时退化到输入法自己的 Shift 热键，并且必须回读校验。
  I5 任何异常/缺环境都返回 (False, 原因)，不抛异常（按键不能被拖累）。

默认全部用假 api 注入，不触碰本机输入法；`PTOE_IME_LIVE=1` 时才额外读一次真实状态。
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import imemanage  # noqa: E402


def read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


class FakeIme:
    """假 Win32 封装：模拟「读得/读不到状态、写生效/不生效、热键生效/不生效」。"""

    def __init__(self, mode=imemanage.IME_CMODE_NATIVE, *, fg=0x11, imewnd=0x22,
                 read_ok=True, set_applies=True, toggle_applies=True):
        self.mode = mode
        self.fg = fg
        self.imewnd = imewnd
        self.read_ok = read_ok
        self.set_applies = set_applies
        self.toggle_applies = toggle_applies
        self.calls = []

    def foreground_window(self):
        self.calls.append(('foreground_window',))
        return self.fg

    def ime_window(self, hwnd):
        self.calls.append(('ime_window', hwnd))
        return self.imewnd

    def conversion_mode(self, imewnd):
        self.calls.append(('get', imewnd))
        return self.mode if self.read_ok else None

    def set_conversion_mode(self, imewnd, mode):
        self.calls.append(('set', imewnd, mode))
        if self.set_applies:
            self.mode = mode
        return True

    def send_toggle_hotkey(self):
        self.calls.append(('toggle',))
        if self.toggle_applies:
            self.mode = (imemanage.IME_CMODE_ALPHANUMERIC
                         if imemanage._is_chinese_mode(self.mode)
                         else imemanage.IME_CMODE_NATIVE)
        return True

    def kinds(self):
        return [c[0] for c in self.calls]


class ImeSourceTest(unittest.TestCase):
    """I1：实现必须只用「转换模式」消息，不允许切键盘布局/输入语言。"""

    def test_only_conversion_mode_no_layout_switch(self):
        src = read(os.path.join(ROOT, 'imemanage.py'))
        self.assertIn('_WM_IME_CONTROL = 0x0283', src)
        self.assertIn('_IMC_GETCONVERSIONMODE = 0x0001', src)
        self.assertIn('_IMC_SETCONVERSIONMODE = 0x0002', src)
        self.assertIn('IME_CMODE_ALPHANUMERIC = 0x0000', src)
        self.assertIn('IME_CMODE_NATIVE = 0x0001', src)
        # 用户明确不要「切到美式键盘」：这两条 API 一个都不许出现
        for bad in ('WM_INPUTLANGCHANGEREQUEST', 'LoadKeyboardLayout',
                    'ImmLoadLayout', 'ActivateKeyboardLayout'):
            self.assertNotIn(bad, src, f'{bad} 会把输入法换成美式键盘，禁止使用')

    def test_correctmanage_delegates_and_drops_layout_switch(self):
        src = read(os.path.join(ROOT, 'correctmanage.py'))
        self.assertIn('def ime_to_english(', src)
        self.assertIn('return imemanage.ime_to_english()', src)
        for bad in ('WM_INPUTLANGCHANGEREQUEST', 'LoadKeyboardLayout'):
            self.assertNotIn(bad, src)

    def test_correctmanage_ime_to_english_calls_module(self):
        import correctmanage

        with mock.patch.object(imemanage, 'ime_to_english',
                               return_value=(True, 'already')) as m:
            self.assertEqual(correctmanage.ime_to_english(), (True, 'already'))
        m.assert_called_once_with()


class ImeSwitchTest(unittest.TestCase):
    """I2–I5：决策与校验逻辑（假 api，不碰本机输入法）。"""

    def setUp(self):
        # 这些用例只在逻辑层跑，平台判断必须放行到 api 分支
        self._plat = mock.patch.object(imemanage.sys, 'platform', 'win32')
        self._plat.start()
        self.addCleanup(self._plat.stop)

    def test_already_english_does_nothing(self):
        api = FakeIme(mode=imemanage.IME_CMODE_ALPHANUMERIC)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertTrue(ok)
        self.assertIn('已是英文', msg)
        # 关键：既没写模式也没按热键，绝不能把英文切回中文
        self.assertEqual(api.kinds(), ['foreground_window', 'ime_window', 'get'])

    def test_chinese_to_english_via_conversion_mode(self):
        api = FakeIme()
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertTrue(ok)
        self.assertIn('已切换', msg)
        self.assertFalse(imemanage._is_chinese_mode(api.mode))
        # 写入的必须是 IME_CMODE_ALPHANUMERIC（英文），且没有退化到热键
        self.assertIn(('set', api.imewnd, imemanage.IME_CMODE_ALPHANUMERIC), api.calls)
        self.assertNotIn('toggle', api.kinds())

    def test_fullshape_native_still_counts_as_chinese(self):
        # 实测搜狗/百度上报 0x401（NATIVE|SYMBOL），必须识别成中文而不是「已是英文」
        api = FakeIme(mode=0x401)
        ok, _msg = imemanage.ime_to_english(api=api)
        self.assertTrue(ok)
        self.assertFalse(imemanage._is_chinese_mode(api.mode))

    def test_falls_back_to_shift_hotkey_when_message_ignored(self):
        api = FakeIme(set_applies=False, toggle_applies=True)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertTrue(ok)
        self.assertIn('Shift', msg)
        self.assertIn('toggle', api.kinds())
        self.assertFalse(imemanage._is_chinese_mode(api.mode))

    def test_reports_failure_when_nothing_works(self):
        api = FakeIme(set_applies=False, toggle_applies=False)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertFalse(ok)
        self.assertIn('未响应', msg)

    def test_unreadable_mode_never_blind_toggles(self):
        # I3：读不到状态时按 Shift 可能把英文切回中文 → 必须什么都不做
        api = FakeIme(read_ok=False)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertFalse(ok)
        self.assertIn('读不到', msg)
        self.assertNotIn('set', api.kinds())
        self.assertNotIn('toggle', api.kinds())

    def test_no_foreground_window(self):
        api = FakeIme(fg=0)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertFalse(ok)
        self.assertIn('前台窗口', msg)

    def test_no_ime_window(self):
        api = FakeIme(imewnd=0)
        ok, msg = imemanage.ime_to_english(api=api)
        self.assertFalse(ok)
        self.assertIn('输入法窗口', msg)

    def test_explicit_hwnd_wins_over_foreground(self):
        api = FakeIme()
        imemanage.ime_to_english(hwnd=0x99, api=api)
        self.assertEqual(api.calls[0], ('ime_window', 0x99))

    def test_api_exception_is_swallowed(self):
        class Boom:
            def foreground_window(self):
                raise RuntimeError('boom')

        ok, msg = imemanage.ime_to_english(api=Boom())
        self.assertFalse(ok)
        self.assertIn('boom', msg)

    def test_non_windows_returns_fast(self):
        with mock.patch.object(imemanage.sys, 'platform', 'linux'):
            ok, msg = imemanage.ime_to_english(api=FakeIme())
        self.assertFalse(ok)
        self.assertIn('Windows', msg)

    def test_conversion_mode_is_none_without_api(self):
        # 真环境/非 Windows 都不得抛异常：读不到就是 None
        with mock.patch.object(imemanage, '_default_api', return_value=None):
            self.assertIsNone(imemanage.conversion_mode())


@unittest.skipUnless(os.environ.get('PTOE_IME_LIVE') == '1',
                     '默认不碰本机输入法；PTOE_IME_LIVE=1 才读真实状态')
class ImeLiveTest(unittest.TestCase):
    """真机冒烟：只读状态，绝不改本机输入法。"""

    def test_conversion_mode_readable_or_none(self):
        mode = imemanage.conversion_mode()
        self.assertTrue(mode is None or isinstance(mode, int))


if __name__ == '__main__':
    unittest.main(verbosity=2)
