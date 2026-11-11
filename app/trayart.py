"""托盘弹出面板的绘制（纯 Pillow）：按当前缩放比例画成高清图，带圆角和柔和阴影。窗口部分在 trayui.py。"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W = 296            # 面板逻辑宽度
PAD = 8            # 阴影留白外的内边距
SHADOW = 18        # 阴影范围
RADIUS = 12

THEMES = {
    "light": dict(bg=(255, 255, 255), line=(231, 231, 228), text=(24, 24, 27), text2=(63, 63, 70), muted=(113, 113, 122),
                  faint=(161, 161, 170), hover=(242, 242, 240), primary=(24, 24, 27), on_primary=(255, 255, 255),
                  track=(214, 214, 209), good=(22, 163, 74), warn=(217, 119, 6), bad=(220, 38, 38), border=(0, 0, 0, 22),
                  shadow=(0, 0, 0, 70)),
    "dark": dict(bg=(28, 28, 31), line=(44, 44, 49), text=(237, 237, 239), text2=(201, 201, 206), muted=(139, 139, 147),
                 faint=(95, 95, 102), hover=(40, 40, 44), primary=(237, 237, 239), on_primary=(12, 12, 13),
                 track=(62, 62, 68), good=(74, 222, 128), warn=(240, 179, 90), bad=(248, 113, 113), border=(255, 255, 255, 26),
                 shadow=(0, 0, 0, 150)),
}

_FONT_CANDIDATES = {
    "regular": [r"C:\Windows\Fonts\msyh.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "medium": [r"C:\Windows\Fonts\msyhbd.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"],
    "bold": [r"C:\Windows\Fonts\msyhbd.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
}
_fonts: dict = {}
_shadows: dict = {}
_logos: dict = {}
_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "icon.png")


def logo(size: int):
    """面板头部的鱼图标（和窗口、托盘用的是同一张 icon.png），按像素尺寸缓存。读不到就返回 None。"""
    if size not in _logos:
        try:
            _logos[size] = Image.open(_LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            _logos[size] = None
    return _logos[size]


def font(weight: str, size: float):
    key = (weight, round(size))
    if key not in _fonts:
        for p in _FONT_CANDIDATES[weight]:
            if os.path.exists(p):
                idx = 2 if "NotoSansCJK" in p else 1     # 微软雅黑取「Microsoft YaHei UI」那一套，字距更紧凑
                _fonts[key] = ImageFont.truetype(p, round(size), index=idx)
                break
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


# 面板里的每一项：(种类, 键, 文字)
def items(st: dict) -> list[tuple]:
    rows = [("head", None, None), ("primary", "open", "打开控制台"), ("sep", None, None)]
    rows.append(("row", "stop_all", "全部停止") if st.get("any") else ("row", "start_all", "全部启动"))
    rows += [("row", "restart_astrbot", "重启 AstrBot"), ("row", "restart_napcat", "重启 NapCat"), ("sep", None, None),
             ("toggle", "autostart", "开机自启"), ("toggle", "watchdog", "掉线自动重启"), ("toggle", "notify", "系统通知"),
             ("sep", None, None), ("row", "quit", "退出控制台")]
    return rows


H = {"head": 64, "primary": 46, "sep": 9, "row": 34, "toggle": 34}


def layout(st: dict) -> tuple[int, list[tuple]]:
    """返回逻辑高度和 [(种类, 键, 文字, y, h)]。"""
    y, out = PAD, []
    for kind, key, text in items(st):
        h = H[kind]
        out.append((kind, key, text, y, h))
        y += h
    return y + PAD, out


def _icon(d: ImageDraw.ImageDraw, key: str, x: float, y: float, s: float, col, k: float):
    """16px 线性图标（描边 1.6），坐标以左上角为原点。"""
    w = max(1, round(1.6 * k))
    P = lambda *pts: [(x + px * s / 16, y + py * s / 16) for px, py in pts]
    if key in ("start_all",):
        d.polygon(P((5, 3.5), (13, 8), (5, 12.5)), outline=col, width=w)
    elif key == "stop_all":
        d.rounded_rectangle(P((3.5, 3.5), (12.5, 12.5)), radius=2 * k, outline=col, width=w)
    elif key in ("restart_astrbot", "restart_napcat"):
        d.arc([x + 2.5 * s / 16, y + 2.5 * s / 16, x + 13.5 * s / 16, y + 13.5 * s / 16], 40, 330, fill=col, width=w)
        d.line(P((13.2, 2.4), (13.2, 6.2), (9.4, 6.2)), fill=col, width=w, joint="curve")
    elif key == "autostart":
        d.rounded_rectangle(P((2.5, 3), (13.5, 11)), radius=1.5 * k, outline=col, width=w)
        d.line(P((6, 13.5), (10, 13.5)), fill=col, width=w)
    elif key == "watchdog":
        d.line(P((8, 2.5), (13, 4.5), (13, 8), (8, 13.5), (3, 8), (3, 4.5), (8, 2.5)), fill=col, width=w, joint="curve")
    elif key == "notify":
        d.line(P((4, 11), (4, 7), (8, 3), (12, 7), (12, 11), (13.2, 12), (2.8, 12), (4, 11)), fill=col, width=w, joint="curve")
        d.line(P((6.8, 14), (9.2, 14)), fill=col, width=w)
    elif key == "quit":
        d.line(P((8, 2.5), (8, 7.5)), fill=col, width=w)
        d.arc([x + 3 * s / 16, y + 3.5 * s / 16, x + 13 * s / 16, y + 13.5 * s / 16], 300, 240, fill=col, width=w)


def render(st: dict, theme: str = "light", scale: float = 1.0, hover: str | None = None, alpha: float = 1.0) -> tuple[Image.Image, list]:
    """画整块面板（含阴影），返回 RGBA 图和可点区域 [(键, x0, y0, x1, y1)]（逻辑像素，相对面板左上角，不含阴影）。"""
    T = THEMES[theme]
    k = scale
    lh, rows = layout(st)
    pw, ph = round(W * k), round(lh * k)
    sh = round(SHADOW * k)
    img = Image.new("RGBA", (pw + sh * 2, ph + sh * 2), (0, 0, 0, 0))
    # 阴影：模糊的圆角矩形，往下偏一点
    ck = (pw, ph, sh, theme)
    if ck not in _shadows:          # 阴影只跟尺寸和深浅色有关，算一次缓存起来（模糊比较慢，悬停重画时不再算）
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle([sh, sh + round(6 * k), sh + pw, sh + ph + round(4 * k)], radius=RADIUS * k, fill=T["shadow"])
        _shadows.clear()
        _shadows[ck] = shadow.filter(ImageFilter.GaussianBlur(sh / 2.2))
    img = Image.alpha_composite(img, _shadows[ck])
    panel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=RADIUS * k, fill=T["bg"] + (255,), outline=T["border"], width=max(1, round(k)))
    hits = []
    X0, X1 = 6 * k, pw - 6 * k
    for kind, key, text, y, h in rows:
        Y, Hh = y * k, h * k
        if kind == "head":
            name, word, color = st.get("name", "机器人"), st.get("word", ""), st.get("color", "gray")
            mx, my, ms = 14 * k, Y + 14 * k, 34 * k
            lg = logo(round(ms))
            if lg is not None:
                panel.alpha_composite(lg, (round(mx), round(my)))
            else:
                d.rounded_rectangle([mx, my, mx + ms, my + ms], radius=9 * k, fill=T["primary"])
            tx = mx + ms + 12 * k
            d.text((tx, my + 1 * k), name, font=font("bold", 14 * k), fill=T["text"], anchor="la")
            dot = T.get({"green": "good", "yellow": "warn", "red": "bad"}.get(color, "faint"), T["faint"])
            cy = my + 25 * k
            r = 3.5 * k
            d.ellipse([tx + 0.5 * k, cy - r, tx + 0.5 * k + 2 * r, cy + r], fill=dot)
            sub = word + (f" · QQ {st['qq']}" if st.get("qq") else "")
            d.text((tx + 2 * r + 7 * k, cy), sub, font=font("regular", 12 * k), fill=T["muted"], anchor="lm")
            if st.get("version"):
                d.text((pw - 16 * k, my + 2 * k), "v" + st["version"], font=font("regular", 11 * k), fill=T["faint"], anchor="ra")
        elif kind == "primary":
            bx0, by0, bx1, by1 = 12 * k, Y + 4 * k, pw - 12 * k, Y + Hh - 8 * k
            fill = T["primary"]
            if hover == key:
                fill = tuple(round(c * 0.86 + (255 if theme == "light" else 0) * 0.14) for c in fill)
            d.rounded_rectangle([bx0, by0, bx1, by1], radius=8 * k, fill=fill)
            d.text(((bx0 + bx1) / 2, (by0 + by1) / 2), text, font=font("medium", 13.5 * k), fill=T["on_primary"], anchor="mm")
            hits.append((key, 12, y + 4, W - 12, y + h - 8))
        elif kind == "sep":
            d.line([(14 * k, Y + Hh / 2), (pw - 14 * k, Y + Hh / 2)], fill=T["line"], width=max(1, round(k)))
        else:
            if hover == key:
                d.rounded_rectangle([X0, Y + 1 * k, X1, Y + Hh - 1 * k], radius=7 * k, fill=T["hover"])
            col = T["muted"] if key == "quit" else T["text2"]
            tx = 16 * k
            _icon(d, key, tx, Y + Hh / 2 - 8 * k, 16 * k, T["muted"], k)
            tx += 28 * k
            d.text((tx, Y + Hh / 2), text, font=font("regular", 13.5 * k), fill=col if kind == "row" else T["text2"], anchor="lm")
            if kind == "row" and key == "quit":
                d.text((pw - 16 * k, Y + Hh / 2), "机器人保持在线", font=font("regular", 11.5 * k), fill=T["faint"], anchor="rm")
            if kind == "toggle":
                on = bool(st.get(key))
                prog = st.get("_anim", {}).get(key, 1.0)          # 开关动画进度 0→1
                pos = (prog if on else 1 - prog)
                sw, shh = 32 * k, 18 * k
                sx1, sy0 = pw - 16 * k, Y + Hh / 2 - shh / 2
                sx0 = sx1 - sw
                tc = tuple(round(a + (b - a) * pos) for a, b in zip(T["track"], T["primary"]))
                d.rounded_rectangle([sx0, sy0, sx1, sy0 + shh], radius=shh / 2, fill=tc)
                kr = 7 * k
                kx = sx0 + 2 * k + kr + (sw - 4 * k - 2 * kr) * pos
                knob = T["on_primary"] if pos > .5 else (255, 255, 255)
                d.ellipse([kx - kr, sy0 + shh / 2 - kr, kx + kr, sy0 + shh / 2 + kr], fill=knob)
            hits.append((key, 6, y, W - 6, y + h))
    img.alpha_composite(panel, (sh, sh))
    if alpha < 1:
        a = img.getchannel("A").point(lambda v: round(v * alpha))
        img.putalpha(a)
    return img, hits
