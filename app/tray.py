"""右下角托盘图标：颜色表示在不在线，右键弹出面板（trayui.py，自己画的；画不出来就退回系统菜单），系统通知。
依赖 pystray + Pillow（随包带）。"""

from __future__ import annotations

import os
import threading
import time

import services
from core import UI_DIR, VERSION, load_state, log, log_exc, open_console_window, update_state

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # 没有依赖就不显示托盘，其他功能照常
    pystray = None

_icon = None
_state = {"key": None, "text": "正在读取状态…", "any": False, "name": "", "word": "正在读取状态", "color": "gray", "qq": ""}
_flyout = None


_base = None


def _img(color: tuple[int, int, int]):
    """256px 出图（存成多尺寸图标，系统按缩放比例挑合适的那张），右下角状态圆点带白边。"""
    global _base
    if _base is None:
        _base = Image.open(UI_DIR / "icon.png").convert("RGBA").resize((256, 256), Image.LANCZOS)
    big = _base.copy().resize((1024, 1024), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    d.ellipse([620, 620, 1012, 1012], fill=(255, 255, 255, 255))
    d.ellipse([668, 668, 964, 964], fill=color + (255,))
    return big.resize((256, 256), Image.LANCZOS)


# 与控制台界面的状态色一致
GREEN, YELLOW, RED, GRAY = (22, 163, 74), (217, 119, 6), (220, 38, 38), (161, 161, 170)


def _dark_menus() -> None:
    """右键菜单跟随系统深浅色（Win10 1903 起支持；不支持就保持浅色）。要在建托盘窗口之前调用。"""
    try:
        import ctypes
        ux = ctypes.WinDLL("uxtheme")
        set_mode = ux[135]           # SetPreferredAppMode
        set_mode.argtypes = [ctypes.c_int]
        set_mode(1)                  # AllowDark：系统是深色就用深色
        ux[136]()                    # FlushMenuThemes
    except Exception:
        pass


def _refresh_menu() -> None:
    if _icon is not None:
        try:
            _icon.update_menu()
        except Exception:
            log_exc()


def _act(name, fn):
    def go(icon, item):
        services.run_job(name, fn)
    return go


def _toggle(key):
    def go(icon, item):
        s = load_state()
        update_state(**{key: not s.get(key, True)})
        _refresh_menu()
    return go


def _checked(key):
    return lambda item: bool(load_state().get(key, True))


def notify(title: str, text: str) -> None:
    if _icon is not None:
        try:
            _icon.notify(text, title)
        except Exception:
            log_exc()


def _quit(icon, item):
    log("从托盘退出控制台（bot 保持在线）")
    icon.stop()
    os._exit(0)


def _restart_astrbot():
    services.stop_astrbot()
    return services.start_astrbot()


def _restart_napcat():
    services.stop_napcat()
    time.sleep(3)
    return services.start_napcat()


def _tray_class():
    """在 pystray 的 Windows 托盘上改两处：右键弹自己的面板；图标按托盘实际尺寸载入（高分屏不发虚）。"""
    base = pystray.Icon
    try:
        from pystray._util import serialized_image
        from pystray._util import win32 as pw
    except Exception:
        return base

    class Tray(base):
        def _on_notify(self, wparam, lparam):
            if lparam == pw.WM_RBUTTONUP and _flyout is not None and _flyout.ok:
                _flyout.show()
                return
            return super()._on_notify(wparam, lparam)

        def _assert_icon_handle(self):
            if self._icon_handle:
                return
            try:
                import ctypes
                u = ctypes.windll.user32
                try:
                    size = u.GetSystemMetricsForDpi(49, u.GetDpiForSystem())     # SM_CXSMICON
                except Exception:
                    size = u.GetSystemMetrics(49)
                with serialized_image(self.icon, "ICO") as path:
                    self._icon_handle = pw.LoadImage(None, path, pw.IMAGE_ICON, size, size, pw.LR_LOADFROMFILE)
            except Exception:
                self._icon_handle = None
            if not self._icon_handle:
                super()._assert_icon_handle()
    return Tray


def _flyout_state() -> dict:
    s = load_state()
    return {"name": _state["name"] or services.bot_name(), "word": _state["word"], "color": _state["color"],
            "qq": _state["qq"] if _state["color"] == "green" else "", "version": VERSION, "any": _state["any"],
            "autostart": s.get("autostart", True), "watchdog": s.get("watchdog", True), "notify": s.get("notify", True)}


def _flyout_act(key: str) -> bool:
    """面板里点了一项。返回 True 表示面板留着（开关），否则面板收起。"""
    if key in ("autostart", "watchdog", "notify"):
        update_state(**{key: not load_state().get(key, True)})
        _refresh_menu()
        return True
    if key == "open":
        threading.Thread(target=open_console_window, daemon=True).start()
    elif key == "start_all":
        services.run_job("全部启动", services.start_all)
    elif key == "stop_all":
        services.run_job("全部停止", services.stop_all)
    elif key == "restart_astrbot":
        services.run_job("重启 AstrBot", _restart_astrbot)
    elif key == "restart_napcat":
        services.run_job("重启 NapCat", _restart_napcat)
    elif key == "quit":
        threading.Timer(0.2, lambda: _quit(_icon, None)).start()     # 等面板收起再退
    return False


def run() -> None:
    global _icon, _flyout
    if pystray is None:
        log("托盘依赖没装上，跳过托盘")
        return
    M, Item = pystray.Menu, pystray.MenuItem
    menu = M(
        Item(lambda it: _state["text"], None, enabled=False),
        M.SEPARATOR,
        Item("打开控制台", lambda i, it: open_console_window(), default=True),
        Item("全部启动", _act("全部启动", services.start_all), visible=lambda it: not _state["any"]),
        Item("全部停止", _act("全部停止", services.stop_all), visible=lambda it: _state["any"]),
        Item("重启", M(
            Item("重启 AstrBot", _act("重启 AstrBot", _restart_astrbot)),
            Item("重启 NapCat", _act("重启 NapCat", _restart_napcat)),
        )),
        M.SEPARATOR,
        Item("选项", M(
            Item("开机自启", _toggle("autostart"), checked=_checked("autostart")),
            Item("掉线自动重启", _toggle("watchdog"), checked=_checked("watchdog")),
            Item("系统通知", _toggle("notify"), checked=_checked("notify")),
        )),
        M.SEPARATOR,
        Item("退出控制台（bot 保持在线）", _quit),
    )
    try:
        import trayui
        trayui.set_dpi_aware()
        _flyout = trayui.Flyout(_flyout_state, _flyout_act)
        _flyout.ready.wait(5)
        if not _flyout.ok:
            log("托盘面板没建起来，右键改用系统菜单")
    except Exception:
        log_exc()
        _flyout = None
    _dark_menus()
    _icon = _tray_class()("dafeiyu", _img(GRAY), "bot 控制台", menu)
    services.notify_hook = notify

    def updater():
        last = None
        while True:
            try:
                st = services.status_cached(max_age=3)
                name = (st.get("brief") or {}).get("bot_name") or services.bot_name()
                nc, ab = st["napcat"], st["astrbot"]
                if not st["setup_done"]:
                    col, word = GRAY, "还没装完"
                elif nc["state"] == "online" and ab["running"]:
                    col, word = GREEN, "在线"
                elif nc["state"] == "need_qr":
                    col, word = YELLOW, "需要扫码登录"
                elif ab.get("starting") or (st["job"].get("running") and "启动" in (st["job"].get("name") or "")):
                    col, word = YELLOW, "正在启动"
                elif nc["running"] or ab["running"]:
                    col, word = YELLOW, "部分运行"
                else:
                    col, word = RED, "离线"
                qq = nc.get("qq") or ""
                text = f"{'●'} {name} · {word}" + (f" · QQ {qq}" if qq and col == GREEN else "")
                any_running = bool(nc["running"] or ab["running"])
                if col != last:
                    _icon.icon = _img(col)
                    last = col
                _icon.title = f"{name}：{word}"
                key = (text, any_running)
                _state.update(name=name, word=word, color={GREEN: "green", YELLOW: "yellow", RED: "red"}.get(col, "gray"), qq=qq)
                if key != _state["key"]:
                    _state.update(key=key, text=text, any=any_running)
                    _refresh_menu()
            except Exception:
                log_exc()
            time.sleep(3)

    threading.Thread(target=updater, daemon=True).start()
    _icon.run()
