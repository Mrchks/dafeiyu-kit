"""日志按时间段查看、导出，以及启动前把太大的日志挪去归档。

三种日志记时间的方式不一样，这里逐行认时间：
- 控制台日志、AstrBot 自己的文件日志：每行有完整日期「2026-11-02 22:30:00」
- NapCat 输出：「11-02 22:19:39」，年份按文件修改时间推
- AstrBot 输出（astrbot.out.log）：只有「22:29:45」，日期从文件最后修改时间往前倒推，遇到跨零点退一天
没有时间的行（报错堆栈、启动横幅）归到上一行的时间。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

from core import LOG_DIR, WIN, astrbot_data, log, log_exc

ARCH = LOG_DIR / "归档"
EXPORT = LOG_DIR / "导出"
KEEP_DAYS = 30
ROTATE_MB = 5
NAMES = {"astrbot": "AstrBot", "napcat": "NapCat", "console": "控制台"}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RE_FULL = re.compile(r"^\W{0,3}(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
RE_MD = re.compile(r"^\W{0,3}(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
RE_T = re.compile(r"^\W{0,3}(\d{2}):(\d{2}):(\d{2})\.\d{3}\]")     # AstrBot 输出：[22:29:45.880]
RE_BOOT = re.compile(r"\[core\.main:\d+\]")                          # AstrBot 每次启动打印的第一行
DROP = 2 * 3600     # 时间往回跳超过 2 小时才算跨了零点（两个进程同时写会有小幅乱序）
PROBLEM = re.compile(r"error|错误|失败|failed|exception|traceback|not in the session allowlist|cannot find|warn|超时|掉线", re.I)
QQ = re.compile(r"(?<!\d)([1-9]\d)\d{2,7}(\d{2})(?!\d)")


# ---------------------------------------------------------------- 归档

def rotate(name: str) -> None:
    """启动 AstrBot / NapCat 前调用：输出日志超过 5MB 就挪去「归档」，归档只留 30 天。"""
    f = LOG_DIR / name
    try:
        if f.exists() and f.stat().st_size > ROTATE_MB * 1024 * 1024:
            ARCH.mkdir(parents=True, exist_ok=True)
            f.replace(ARCH / f"{f.stem}-{dt.datetime.now():%Y%m%d-%H%M%S}.log")
            log(f"日志 {name} 超过 {ROTATE_MB}MB，已挪到归档")
    except Exception:
        log_exc()   # 文件被占用就下次再挪，不影响启动
    try:
        cut = time.time() - KEEP_DAYS * 86400
        for p in ARCH.glob("*.log") if ARCH.exists() else []:
            if p.stat().st_mtime < cut:
                p.unlink()
    except Exception:
        log_exc()


# ---------------------------------------------------------------- 读文件、认时间

def _files(src: str) -> list[Path]:
    """按时间先后排好的日志文件。"""
    if src == "console":
        fs = [LOG_DIR / "console.old.log", LOG_DIR / "console.log"]
    elif src == "napcat":
        fs = sorted(ARCH.glob("napcat-*.log")) + [LOG_DIR / "napcat.log"]
    else:
        d = astrbot_data()
        own = sorted((d / "logs").glob("astrbot*.log"), key=lambda p: p.stat().st_mtime) if d and (d / "logs").exists() else []
        fs = own or (sorted(ARCH.glob("astrbot.out-*.log")) + [LOG_DIR / "astrbot.out.log"])
    return [p for p in fs if p.exists()]


def _read(p: Path) -> list[str]:
    with open(p, "rb") as f:
        return [ANSI.sub("", ln.decode("utf-8", "replace").rstrip("\r\n")) for ln in f]


def _stamp(lines: list[str], mtime: float) -> list[dt.datetime | None]:
    """每行的时间（认不出的先放 None）。"""
    end = dt.datetime.fromtimestamp(mtime)
    out: list[dt.datetime | None] = [None] * len(lines)
    tod_idx = []
    for i, ln in enumerate(lines):
        head = ln[:40]
        m = RE_FULL.match(head)
        if m:
            try:
                out[i] = dt.datetime(*map(int, m.groups()))
            except ValueError:
                pass
            continue
        m = RE_MD.match(head)
        if m:
            mo, d, h, mi, s = map(int, m.groups())
            y = end.year - (1 if mo > end.month else 0)
            try:
                out[i] = dt.datetime(y, mo, d, h, mi, s)
            except ValueError:
                pass
            continue
        m = RE_T.match(head)
        if m:
            h, mi, s = map(int, m.groups())
            if h < 24 and mi < 60 and s < 60:
                tod_idx.append((i, h * 3600 + mi * 60 + s))
    if tod_idx:
        _infer_days(lines, out, tod_idx, end)
    # 没时间的行跟上一行；开头几行跟第一个有时间的行
    first = next((t for t in out if t), end)
    cur = first
    for i, t in enumerate(out):
        if t:
            cur = t
        else:
            out[i] = cur
    return out


def _boot_times() -> list[dt.datetime]:
    """控制台日志里记的每次「启动 AstrBot」的完整时间，用来给只有时分秒的 AstrBot 输出定日期。"""
    res = []
    for p in (LOG_DIR / "console.old.log", LOG_DIR / "console.log"):
        try:
            for ln in _read(p) if p.exists() else []:
                if "启动 AstrBot" in ln:
                    m = RE_FULL.match(ln)
                    if m:
                        res.append(dt.datetime(*map(int, m.groups())))
        except Exception:
            log_exc()
    return sorted(res)


def _infer_days(lines, out, tod_idx, end: dt.datetime) -> None:
    """按每次启动分段：能和控制台记的启动时间对上的段，用那天往后推；对不上的接着上一段推；
    最前面对不上的几段从后一段（或文件修改时间）往前倒推。段内时间往回跳超过 2 小时算过了零点。"""
    segs, cur = [], []
    for i, tod in tod_idx:
        if cur and RE_BOOT.search(lines[i]):
            segs.append(cur); cur = []
        cur.append((i, tod))
    segs.append(cur)
    boots, used = _boot_times(), dt.datetime.min
    starts: list[dt.date | None] = []
    for sg in segs:
        t0, hit = sg[0][1], None
        if RE_BOOT.search(lines[sg[0][0]]):
            for b in boots:
                bt = b.hour * 3600 + b.minute * 60 + b.second
                if b > used and 0 <= (t0 - bt) % 86400 <= 300:
                    hit = b; break
        if hit:
            used = hit
            starts.append((hit + dt.timedelta(seconds=(t0 - (hit.hour * 3600 + hit.minute * 60 + hit.second)) % 86400)).date())
        else:
            starts.append(None)

    def forward(sg, day):
        last = None
        for i, tod in sg:
            if last is not None and tod < last - DROP:
                day += dt.timedelta(days=1)
            last = tod
            out[i] = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=tod)
        return day, last

    def backward(sg, day, nxt_tod):
        last = nxt_tod
        for i, tod in reversed(sg):
            if last is not None and tod > last + DROP:
                day -= dt.timedelta(days=1)
            last = tod
            out[i] = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=tod)
        return day, sg[0][1]

    first = next((k for k, d in enumerate(starts) if d), None)
    if first is None:                        # 一段都对不上：整份从文件修改时间往前倒推
        day, nxt = end.date(), None
        for sg in reversed(segs):
            day, nxt = backward(sg, day, nxt)
        return
    day, nxt = starts[first], segs[first][0][1]
    for k in range(first - 1, -1, -1):
        day, nxt = backward(segs[k], day, nxt)
    day, last = None, None
    for k in range(first, len(segs)):
        if starts[k]:
            day = starts[k]
        elif last is not None and segs[k][0][1] < last - DROP:
            day += dt.timedelta(days=1)
        day, last = forward(segs[k], day)


def _entries(src: str, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, str]]:
    res = []
    for p in _files(src):
        try:
            st = p.stat()
            if dt.datetime.fromtimestamp(st.st_mtime) < start:     # 最后一次写都在开始时间之前，整份跳过
                continue
            lines = _read(p)
            for t, ln in zip(_stamp(lines, st.st_mtime), lines):
                if start <= t <= end:
                    res.append((t, ln))
        except Exception:
            log_exc()
    return res


def _parse(s: str, default: dt.datetime) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(s).strip().replace(" ", "T"))
    except Exception:
        return default


def _range(start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now()
    a = _parse(start, now.replace(hour=0, minute=0, second=0, microsecond=0))
    b = _parse(end, now)
    if b.second == 0 and b.microsecond == 0:
        b = b.replace(second=59)            # 选到分钟，包含这一分钟
    return (a, b) if a <= b else (b, a)


def _lines(src: str, a: dt.datetime, b: dt.datetime, problems: bool, mask: bool) -> list[str]:
    out, day = [], None
    for t, ln in _entries(src, a, b):
        if problems and not PROBLEM.search(ln):
            continue
        if t.date() != day:
            day = t.date()
            out.append(f"──────── {day:%Y-%m-%d} ────────")
        out.append(QQ.sub(r"\1****\2", ln) if mask else ln)
    return out


def query(src: str, start: str, end: str, problems: bool = False, limit: int = 5000) -> dict:
    a, b = _range(start, end)
    lines = _lines(src, a, b, problems, False)
    total = len(lines)
    keep = lines[-limit:]
    if total > limit and not keep[0].startswith("────"):       # 截断后开头补上当天的日期分隔行
        head = next((ln for ln in reversed(lines[:total - limit]) if ln.startswith("────")), None)
        if head:
            keep = [head] + keep
    return {"lines": keep, "total": sum(1 for ln in lines if not ln.startswith("────")), "truncated": total > limit,
            "start": f"{a:%Y-%m-%d %H:%M}", "end": f"{b:%Y-%m-%d %H:%M}"}


def export(src: str, start: str, end: str, problems: bool = False, mask: bool = False) -> dict:
    a, b = _range(start, end)
    srcs = list(NAMES) if src == "all" else [src if src in NAMES else "astrbot"]
    EXPORT.mkdir(parents=True, exist_ok=True)
    span = f"{a:%Y%m%d-%H%M}_至_{b:%Y%m%d-%H%M}"
    head = lambda s: [f"# {NAMES[s]} 日志  {a:%Y-%m-%d %H:%M} 至 {b:%Y-%m-%d %H:%M}"
                      + ("  （仅异常）" if problems else "") + ("  （已隐去 QQ 号、群号）" if mask else ""),
                      f"# 导出于 {dt.datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    texts = {s: "\n".join(head(s) + (_lines(s, a, b, problems, mask) or ["（这段时间没有日志）"])) + "\n" for s in srcs}
    if len(srcs) == 1:
        path = EXPORT / f"日志-{NAMES[srcs[0]]}-{span}.txt"
        path.write_text(texts[srcs[0]], encoding="utf-8-sig")
    else:
        path = EXPORT / f"日志-全部-{span}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for s, t in texts.items():
                z.writestr(f"{NAMES[s]}.txt", ("﻿" + t).encode("utf-8"))
    size = path.stat().st_size
    log(f"导出日志：{path.name}（{size // 1024} KB）")
    reveal(path)
    return {"ok": True, "path": str(path), "name": path.name, "size": size}


def reveal(path: Path) -> None:
    """在资源管理器里打开文件夹并选中这个文件。"""
    try:
        if WIN:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
    except Exception:
        log_exc()


# NapCat 日志里大量是 QQ 内核的 MMKV 记录和二维码字符画，看问题时都是噪音
NAPCAT_NOISE = re.compile(r"^\s*\[[IWEDV]\]\s*<|^[\s\u2580-\u259f]+$")


def meaningful_tail(p: Path, n: int, scan_bytes: int = 3_000_000, noise: re.Pattern | None = NAPCAT_NOISE) -> list[str]:
    """读日志末尾 scan_bytes 字节，去掉颜色码和噪音行，返回最后 n 行有内容的记录（连续重复的行只留一行）。"""
    from core import decode
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - scan_bytes))
            data = f.read()
    except FileNotFoundError:
        return []
    out: list[str] = []
    for ln in decode(data).splitlines():
        ln = ANSI.sub("", ln).rstrip()
        if not ln.strip() or (noise and noise.search(ln)):
            continue
        if out and out[-1] == ln:
            continue
        out.append(ln)
    return out[-n:]
