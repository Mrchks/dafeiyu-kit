"""托盘右键面板：自己画的弹出窗口（代替系统菜单）。

画图在 trayart.py（Pillow），这里只管 Windows 窗口：分层窗口 + 每像素透明度，圆角和阴影都是真的；
按显示器缩放比例出图，所以高分屏下也清晰。窗口在自己的线程里收消息，托盘线程只发一条「显示」消息过来。
任何一步出错，tray.py 会退回系统菜单。
"""
from __future__ import annotations

import ctypes
import threading
import time
import winreg
from ctypes import wintypes as W

from PIL import Image, ImageChops

import trayart

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:
    shcore = None

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x00080000, 0x00000080, 0x00000008
WM_DESTROY, WM_ACTIVATE, WM_KEYDOWN, WM_TIMER = 0x0002, 0x0006, 0x0100, 0x0113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSELEAVE = 0x0200, 0x0201, 0x0202, 0x02A3
WM_APP_SHOW = 0x8000 + 1
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA, AC_SRC_ALPHA = 2, 1
TME_LEAVE = 2
VK_ESCAPE, VK_RETURN, VK_UP, VK_DOWN, VK_SPACE = 0x1B, 0x0D, 0x26, 0x28, 0x20
T_ANIM, T_TOGGLE, T_WATCH = 1, 2, 3
WM_SETCURSOR = 0x0020
IDC_ARROW, IDC_HAND = 32512, 32649


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG),
                ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("dwFlags", W.DWORD), ("hwndTrack", W.HWND), ("dwHoverTime", W.DWORD)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", W.HWND), ("message", W.UINT), ("wParam", W.WPARAM), ("lParam", W.LPARAM), ("time", W.DWORD),
                ("pt", W.POINT)]


def _sig(fn, res, *args):
    fn.restype, fn.argtypes = res, list(args)


_sig(user32.DefWindowProcW, LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_sig(user32.RegisterClassExW, W.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
_sig(user32.UpdateLayeredWindow, W.BOOL, W.HWND, W.HDC, ctypes.POINTER(W.POINT), ctypes.POINTER(W.SIZE), W.HDC,
     ctypes.POINTER(W.POINT), W.COLORREF, ctypes.POINTER(BLENDFUNCTION), W.DWORD)
_sig(user32.GetDC, W.HDC, W.HWND)
_sig(user32.ReleaseDC, ctypes.c_int, W.HWND, W.HDC)
_sig(user32.ShowWindow, W.BOOL, W.HWND, ctypes.c_int)
_sig(user32.SetForegroundWindow, W.BOOL, W.HWND)
_sig(user32.SetTimer, ctypes.c_size_t, W.HWND, ctypes.c_size_t, W.UINT, W.LPVOID)
_sig(user32.KillTimer, W.BOOL, W.HWND, ctypes.c_size_t)
_sig(user32.GetCursorPos, W.BOOL, ctypes.POINTER(W.POINT))
_sig(user32.MonitorFromPoint, W.HMONITOR, W.POINT, W.DWORD)
_sig(user32.GetMonitorInfoW, W.BOOL, W.HMONITOR, ctypes.POINTER(MONITORINFO))
_sig(user32.TrackMouseEvent, W.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
_sig(user32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_sig(user32.GetMessageW, W.BOOL, ctypes.POINTER(MSG), W.HWND, W.UINT, W.UINT)
_sig(user32.TranslateMessage, W.BOOL, ctypes.POINTER(MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(MSG))
_sig(user32.LoadCursorW, W.HANDLE, W.HINSTANCE, W.LPVOID)
_sig(user32.SetCursor, W.HANDLE, W.HANDLE)
_sig(kernel32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
_sig(user32.GetForegroundWindow, W.HWND)
_sig(user32.IsWindowVisible, W.BOOL, W.HWND)
_sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
if shcore:
    _sig(shcore.GetDpiForMonitor, ctypes.c_long, W.HMONITOR, ctypes.c_int, ctypes.POINTER(W.UINT), ctypes.POINTER(W.UINT))
_sig(gdi32.CreateCompatibleDC, W.HDC, W.HDC)
_sig(gdi32.CreateDIBSection, W.HBITMAP, W.HDC, ctypes.POINTER(BITMAPINFOHEADER), W.UINT, ctypes.POINTER(ctypes.c_void_p),
     W.HANDLE, W.DWORD)
_sig(gdi32.SelectObject, W.HGDIOBJ, W.HDC, W.HGDIOBJ)
_sig(gdi32.DeleteObject, W.BOOL, W.HGDIOBJ)
_sig(gdi32.DeleteDC, W.BOOL, W.HDC)


def set_dpi_aware() -> None:
    """每台显示器各自按缩放比例出图：托盘图标、菜单、通知都不再被系统放大发虚。越早调用越好。"""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):     # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:
        if shcore and shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _scale_at(pt: W.POINT) -> tuple[float, W.RECT]:
    mon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    dpi = 96
    try:
        dx, dy = W.UINT(), W.UINT()
        if shcore and shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            dpi = dx.value
    except Exception:
        pass
    return dpi / 96, mi.rcWork


def _theme() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


_logged = set()


def _log_once(where: str) -> None:
    """面板里出错只记一次日志（同一处反复出错不刷屏），面板本身不崩。"""
    if where in _logged:
        return
    _logged.add(where)
    try:
        from core import log_exc
        log_exc()
    except Exception:
        pass


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


class Flyout:
    """state_fn() 返回面板要显示的状态；act(键) 执行一项操作，返回 True 表示点完不收起（开关）。"""

    def __init__(self, state_fn, act):
        self.state_fn, self.act = state_fn, act
        self.hwnd = None
        self.ready = threading.Event()
        self.ok = False
        self.vis = False
        self.hover = None
        self.pressed = None
        self.hits: list = []
        self.keys: list = []
        self.scale, self.theme = 1.0, "light"
        self.st: dict = {}
        self.anim = None          # (开始时间, 时长, 方向 1 出现 / -1 收起)
        self.toggle_anim = None   # (键, 开始时间)
        self.base = (0, 0)        # 窗口最终位置（物理像素）
        self.slide = 1            # 1：从下往上浮；-1：从上往下
        self.img = None
        self.data = b""
        self.closed_at = 0.0
        self.was_fg = False
        threading.Thread(target=self._loop, name="tray-flyout", daemon=True).start()

    # ------------------------------------------------------------ 线程与窗口
    def _loop(self):
        try:
            self._proc = WNDPROC(self._wndproc)       # 留着引用，免得被回收
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=hinst,
                             lpszClassName="DafeiyuTrayFlyout", hCursor=user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW)))
            user32.RegisterClassExW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST, "DafeiyuTrayFlyout", "托盘面板",
                                               WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
            self.ok = bool(self.hwnd)
        except Exception:
            _log_once("create")
            self.ok = False
        self.ready.set()
        if not self.ok:
            return
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def visible(self) -> bool:
        return bool(self.hwnd and user32.IsWindowVisible(self.hwnd))

    def show(self):
        """托盘线程调用：让面板线程弹出面板。"""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_SHOW, 0, 0)

    # ------------------------------------------------------------ 出图
    def _render(self, alpha: float = 1.0):
        st = dict(self.st)
        if self.toggle_anim:
            key, t0 = self.toggle_anim
            st["_anim"] = {key: min(1.0, (time.perf_counter() - t0) / 0.2)}
        self.img, hits = trayart.render(st, self.theme, self.scale, hover=self.hover or self.pressed)
        r, g, b, a = self.img.split()
        # 每像素透明要求颜色先乘上透明度；出一次图算一次，动画帧直接复用
        r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
        self.data = Image.merge("RGBA", (b, g, r, a)).tobytes()
        self.hits = hits
        self.keys = [h[0] for h in hits]

    def _blit(self, alpha: float, dy: float):
        w, h = self.img.size
        data = self.data
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem, hbmp)
        ctypes.memmove(bits, data, len(data))
        pos = W.POINT(self.base[0], self.base[1] + round(dy))
        size, src = W.SIZE(w, h), W.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, round(255 * alpha))), AC_SRC_ALPHA)
        done = user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(pos), ctypes.byref(size), mem, ctypes.byref(src), 0,
                                          ctypes.byref(blend), ULW_ALPHA)
        err = ctypes.get_last_error()
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        if not done:
            raise OSError(f"UpdateLayeredWindow 失败，错误码 {err}")

    def _frame(self):
        """动画进行中按当前进度出一帧；没有动画就按完全显示出一帧。"""
        if self.anim:
            t0, dur, dirn = self.anim
            p = min(1.0, (time.perf_counter() - t0) / dur)
            e = _ease_out(p) if dirn > 0 else 1 - p
            self._blit(e, (1 - e) * 8 * self.scale * self.slide)
            return p >= 1
        self._blit(1.0, 0)
        return True

    # ------------------------------------------------------------ 打开、关闭
    def _open(self):
        pt = W.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self.scale, work = _scale_at(pt)
        self.theme = _theme()
        self.st = self.state_fn()
        self.hover = self.pressed = None
        self.toggle_anim = None
        self._render()
        sh = round(trayart.SHADOW * self.scale)
        w, h = self.img.size[0] - 2 * sh, self.img.size[1] - 2 * sh       # 面板本身（不含阴影）
        gap = round(10 * self.scale)
        x = max(work.left + gap, min(pt.x - w // 2, work.right - w - gap))
        if pt.y >= work.bottom - 2:                 # 任务栏在下面（最常见）
            y, self.slide = work.bottom - h - gap, 1
        elif pt.y <= work.top + 2:                  # 任务栏在上面
            y, self.slide = work.top + gap, -1
        else:                                       # 任务栏在左右两边
            y, self.slide = max(work.top + gap, min(pt.y - h + gap, work.bottom - h - gap)), 1
            if pt.x >= work.right - 2:
                x = work.right - w - gap
            elif pt.x <= work.left + 2:
                x = work.left + gap
        self.base = (x - sh, y - sh)
        self.anim = (time.perf_counter(), 0.16, 1)
        self._frame()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetForegroundWindow(self.hwnd)
        self.vis = True
        user32.SetTimer(self.hwnd, T_ANIM, 10, None)
        self.was_fg = False
        user32.SetTimer(self.hwnd, T_WATCH, 120, None)
        tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, self.hwnd, 0)
        user32.TrackMouseEvent(ctypes.byref(tme))

    def _close(self):
        if not self.vis or (self.anim and self.anim[2] < 0):
            return
        self.anim = (time.perf_counter(), 0.12, -1)
        user32.SetTimer(self.hwnd, T_ANIM, 10, None)

    def _hidden(self):
        user32.ShowWindow(self.hwnd, SW_HIDE)
        user32.KillTimer(self.hwnd, T_WATCH)
        self.closed_at = time.perf_counter()
        self.vis = False
        self.anim = None

    # ------------------------------------------------------------ 交互
    def _hit(self, lparam):
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        sh = trayart.SHADOW
        lx, ly = x / self.scale - sh, y / self.scale - sh
        for key, x0, y0, x1, y1 in self.hits:
            if x0 <= lx <= x1 and y0 <= ly <= y1:
                return key
        return None

    def _inside(self) -> bool:
        pt = W.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        sh = round(trayart.SHADOW * self.scale)
        x0, y0 = self.base[0] + sh, self.base[1] + sh
        return x0 <= pt.x <= x0 + self.img.size[0] - 2 * sh and y0 <= pt.y <= y0 + self.img.size[1] - 2 * sh

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND if key else IDC_ARROW)))
            self._render()
            if not self.anim:
                self._frame()

    def _do(self, key):
        if not key:
            return
        keep = False
        try:
            keep = bool(self.act(key))
        except Exception:
            _log_once("act-" + key)
        if keep:                                  # 开关：原地切换，圆钮滑过去
            self.st = self.state_fn()
            self.toggle_anim = (key, time.perf_counter())
            user32.SetTimer(self.hwnd, T_TOGGLE, 10, None)
        else:
            self._close()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_SHOW:
                closing = self.anim and self.anim[2] < 0
                if self.vis and not closing:
                    self._close()
                elif not closing and time.perf_counter() - self.closed_at > 0.3:   # 刚因为点了托盘而收起的，不马上又弹出来
                    self._open()
                return 0
            if msg == WM_SETCURSOR:
                user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND if self.hover else IDC_ARROW)))
                return 1
            if msg == WM_TIMER:
                if wp == T_ANIM:
                    if self._frame():
                        user32.KillTimer(hwnd, T_ANIM)
                        if self.anim and self.anim[2] < 0:
                            self._hidden()
                        self.anim = None
                elif wp == T_WATCH:
                    # 有时系统不让面板拿到焦点，就收不到「切走了」的消息；这里兜底：点了面板外面或焦点到了别处就收起
                    fg = user32.GetForegroundWindow() == hwnd
                    self.was_fg = self.was_fg or fg
                    clicked = (user32.GetAsyncKeyState(0x01) & 0x8000) or (user32.GetAsyncKeyState(0x02) & 0x8000)
                    if (self.was_fg and not fg) or (clicked and not self._inside()):
                        self._close()
                elif wp == T_TOGGLE:
                    self._render()
                    if not self.anim:
                        self._frame()
                    if time.perf_counter() - self.toggle_anim[1] >= 0.2:
                        user32.KillTimer(hwnd, T_TOGGLE)
                        self.toggle_anim = None
                return 0
            if msg == WM_MOUSEMOVE:
                tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                user32.TrackMouseEvent(ctypes.byref(tme))
                self._set_hover(self._hit(lp))
                return 0
            if msg == WM_MOUSELEAVE:
                self.pressed = None
                self._set_hover(None)
                return 0
            if msg == WM_LBUTTONDOWN:
                self.pressed = self._hit(lp)
                return 0
            if msg == WM_LBUTTONUP:
                key = self._hit(lp)
                was, self.pressed = self.pressed, None
                if key and key == was:
                    self._do(key)
                return 0
            if msg == WM_ACTIVATE:
                if (wp & 0xFFFF) == 0:              # 点到别处、切到别的窗口：收起
                    self._close()
                else:
                    self.was_fg = True
                return 0
            if msg == WM_KEYDOWN:
                if wp == VK_ESCAPE:
                    self._close()
                elif wp in (VK_UP, VK_DOWN) and self.keys:
                    i = self.keys.index(self.hover) if self.hover in self.keys else (-1 if wp == VK_DOWN else 0)
                    self._set_hover(self.keys[(i + (1 if wp == VK_DOWN else -1)) % len(self.keys)])
                elif wp in (VK_RETURN, VK_SPACE) and self.hover:
                    self._do(self.hover)
                return 0
        except Exception:
            _log_once(f"msg{msg}")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)
