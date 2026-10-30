"""输入法（IME）中/英文模式切换——Windows only。

矫正界面（`ui/app.js` → `initPtoeVimMode`）在 `Esc` / `Ctrl+[` 从 INSERT 进入
NORMAL 后，接着敲的是 `j/k/h/l` 这类英文键；若输入法仍停在中文模式，字母会被输入法
当成拼音吞掉（表现：普通模式按 `j` 没反应，或蹦出拼音候选框）。浏览器 JS 没有任何
切换输入法的 API，只能由本地服务端（`correctmanage` 的 `POST /api/ime_english`）
调用 Win32 接口完成，见 `correctmanage.ime_to_english`。

用户要求（2026-10-30）：只在**当前输入法内部**把「中文模式」切成「英文模式」，
**不切换键盘布局 / 输入语言**（不加载或激活别的键盘布局，也不请求把输入语言切到
en-US），所以不会把搜狗输入法 / 百度输入法 / 微软拼音换成「美式键盘」。

实现要点：

  1. 定位输入法窗口：`GetForegroundWindow` → `ImmGetDefaultIMEWnd`。
     浏览器（Chrome / Edge / Thorium 等）用 TSF 输入法，**不能**用 `ImmGetContext`：
     输入法上下文是线程相关资源，跨进程只会拿到 NULL（实测 Chrome 窗口恒为 NULL）。
     `ImmGetDefaultIMEWnd` + `WM_IME_CONTROL` 是跨进程唯一可用的 IMM32 通道，搜狗、
     百度、微软拼音都提供这层兼容实现。

  2. 读状态：`SendMessageW(imewnd, WM_IME_CONTROL, IMC_GETCONVERSIONMODE, 0)`，
     `mode & IME_CMODE_NATIVE` 为真即「中文模式」；返回 `-1` 表示这次查询没人处理。

  3. 切英文：`IMC_SETCONVERSIONMODE` 写入 `IME_CMODE_ALPHANUMERIC`（0）。只改转换
     模式，不动输入语言/键盘布局；写完**回读校验**。

  4. 回读仍是中文（个别输入法版本不认这条消息）→ 退化为模拟输入法自己的「中/英
     切换」热键（三家出厂默认都是 `Shift`），再回读校验。

  5. 已经是英文模式 → 直接返回成功、什么也不做（绝不把英文切回中文）。

  6. 拿不到输入法窗口或读不到状态 → **不做盲切**（盲切可能把英文切回中文），
     返回 (False, 原因)，调用方静默忽略。

调用方约定：`ime_to_english()` **不抛异常**；非 Windows / 缺 ctypes / 任何 API 失败
都返回 `(False, 原因)`，绝不影响按键本身。
"""

import sys
import time

# IMM32 常量（imm.h）
IME_CMODE_ALPHANUMERIC = 0x0000  # 英文/半角
IME_CMODE_NATIVE = 0x0001        # 中文（本地语言）模式
_WM_IME_CONTROL = 0x0283
_IMC_GETCONVERSIONMODE = 0x0001
_IMC_SETCONVERSIONMODE = 0x0002

# 退化路径：模拟输入法自身的中英文切换热键（搜狗/百度/微软拼音默认都是 Shift）
_VK_SHIFT = 0x10
_SCAN_SHIFT = 0x2A
_KEYEVENTF_KEYUP = 0x0002
_HOTKEY_SETTLE = 0.06  # 等输入法把热键处理完再回读

_API = None
_API_TRIED = False


class _Win32Ime:
    """ctypes 薄封装：只暴露 ime_to_english 需要的几个调用（便于测试替换）。"""

    def __init__(self, user32, imm32):
        self._user32 = user32
        self._imm32 = imm32

    def foreground_window(self):
        return int(self._user32.GetForegroundWindow() or 0)

    def ime_window(self, hwnd):
        return int(self._imm32.ImmGetDefaultIMEWnd(hwnd) or 0)

    def conversion_mode(self, imewnd):
        """返回当前转换模式；拿不到（无人处理）返回 None。"""
        try:
            r = int(self._user32.SendMessageW(
                imewnd, _WM_IME_CONTROL, _IMC_GETCONVERSIONMODE, 0))
        except Exception:  # noqa: BLE001  查询失败一律当「不可用」
            return None
        if r < 0 or r == 0xFFFFFFFF:
            return None
        return r

    def set_conversion_mode(self, imewnd, mode):
        """写入转换模式；没人处理返回 False（真正的结果由回读校验判定）。"""
        try:
            r = int(self._user32.SendMessageW(
                imewnd, _WM_IME_CONTROL, _IMC_SETCONVERSIONMODE, int(mode)))
        except Exception:  # noqa: BLE001
            return False
        return r != -1

    def send_toggle_hotkey(self):
        """模拟按一下 Shift（输入法自己的中/英切换热键）。"""
        try:
            self._user32.keybd_event(_VK_SHIFT, _SCAN_SHIFT, 0, 0)
            time.sleep(0.02)
            self._user32.keybd_event(_VK_SHIFT, _SCAN_SHIFT, _KEYEVENTF_KEYUP, 0)
            time.sleep(_HOTKEY_SETTLE)
            return True
        except Exception:  # noqa: BLE001
            return False


def _default_api():
    """惰性构建 Win32 封装；非 Windows / 缺 ctypes 返回 None（只尝试一次）。"""
    global _API, _API_TRIED
    if _API_TRIED:
        return _API
    _API_TRIED = True
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        imm32 = ctypes.WinDLL("imm32", use_last_error=True)

        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []
        # SendMessageW 返回 LRESULT（指针宽度），必须按 c_ssize_t 声明，否则 64 位下
        # 返回值被截断成 int，-1/大值判断会失真。
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.keybd_event.restype = None
        user32.keybd_event.argtypes = [
            wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.WPARAM]

        imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
        imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]

        _API = _Win32Ime(user32, imm32)
    except Exception:  # noqa: BLE001  接口不可用时静默降级
        _API = None
    return _API


def _is_chinese_mode(mode):
    """转换模式里带 IME_CMODE_NATIVE 即中文模式。"""
    try:
        return bool(int(mode) & IME_CMODE_NATIVE)
    except (TypeError, ValueError):
        return False


def conversion_mode(hwnd=None, api=None):
    """读前台窗口（或指定窗口）的输入法转换模式；拿不到返回 None。

    供排障/测试用：非 Windows 或窗口没有输入法都返回 None。
    """
    if api is None:
        api = _default_api()
    if api is None:
        return None
    try:
        win = int(hwnd or api.foreground_window() or 0)
        if not win:
            return None
        imewnd = api.ime_window(win)
        if not imewnd:
            return None
        return api.conversion_mode(imewnd)
    except Exception:  # noqa: BLE001
        return None


def ime_to_english(hwnd=None, api=None):
    """把前台窗口的输入法从中文模式切到英文模式，返回 (是否已是英文, 说明)。

    `hwnd` 缺省取 `GetForegroundWindow()`（Esc 时就是矫正界面所在窗口）；
    `api` 仅测试注入，缺省用 `_default_api()` 的真实 Win32 封装。

    返回语义：True = 现在处于英文模式（本来就英文，或本次切换成功）；False = 没能
    切过去 / 环境不支持，说明文字给出原因。任何情况都不抛异常。
    """
    if sys.platform != "win32":
        return False, "仅支持 Windows"
    if api is None:
        api = _default_api()
    if api is None:
        return False, "Win32 输入法接口不可用"
    try:
        win = int(hwnd or api.foreground_window() or 0)
        if not win:
            return False, "没有前台窗口"
        imewnd = api.ime_window(win)
        if not imewnd:
            return False, "当前窗口没有输入法窗口"
        before = api.conversion_mode(imewnd)
        if before is None:
            # 读不到状态就不盲切：盲按 Shift 可能把英文又切回中文
            return False, "读不到输入法状态"
        if not _is_chinese_mode(before):
            return True, "输入法已是英文模式"
        # ① IMM32 转换模式：只改中/英，不动输入语言
        if api.set_conversion_mode(imewnd, IME_CMODE_ALPHANUMERIC):
            after = api.conversion_mode(imewnd)
            if after is not None and not _is_chinese_mode(after):
                return True, "已切换到英文模式"
        # ② 退化：模拟输入法自己的 Shift 切换热键，再回读
        if api.send_toggle_hotkey():
            after = api.conversion_mode(imewnd)
            if after is not None and not _is_chinese_mode(after):
                return True, "已切换到英文模式（Shift 热键）"
        return False, "输入法未响应切换"
    except Exception as e:  # noqa: BLE001  任何 API 异常都不该影响按键
        return False, str(e)
