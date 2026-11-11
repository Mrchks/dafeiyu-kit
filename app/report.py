"""「生成问题报告」：把诊断结果、各组件版本、最近一次操作和最近的日志打成一个压缩包，方便发给会的人看。

QQ 号、群号一律只留头两位和末两位；密钥、密码、令牌整项去掉；日志里的 key、Windows 用户名和后台账号也打码。
文件放在 logs/导出/问题报告-年月日-时分.zip，生成后在资源管理器里选中。
"""
from __future__ import annotations

import datetime as dt
import json
import ntpath
import os
import platform
import re
import zipfile

import logtools
import services
from core import LOG_DIR, VERSION, astrbot_data, load_state, log, log_exc, tail_lines

SECRET_KEY = re.compile(r"key|token|pass|secret|cred|cookie|sign", re.I)
SECRET_VAL = [
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "sk-****"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1****"),
    (re.compile(r"(?i)((?:api_?key|token|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"), r"\1****"),
]
USER_PATH = re.compile(
    r'''(\b[A-Z]:[\\/]+Users[\\/]+)(?:[^\\/:*?"<>|\r\n]+(?=[\\/])|[^\\/:*?"<>|\r\n]+?(?=["'\r\n]|$))''', re.I)
GENERIC_NAMES = frozenset({
    "admin", "administrator", "user", "users", "root", "test", "default", "public", "pc", "owner",
    "astrbot", "napcat", "dafeiyu",
})


def _mask_identity(s: str, backend_names: tuple[str, ...] = ()) -> str:
    """保留路径分隔符（含 JSON 转义），区分用户目录名、当前用户名和后台账号。"""
    s = USER_PATH.sub(r"\1<用户>", s)
    users = {os.environ.get("USERNAME", ""),
             ntpath.basename(os.environ.get("USERPROFILE", "").rstrip("\\/"))} - {""}
    for names, placeholder in ((set(backend_names) - {""}, "<后台账号>"), (users, "<用户>")):
        # 路径已经单独打码；全文替换跳过通用词和短名字，保留配置字段及组件名称。
        names = {name for name in names if len(name) >= 3 and name.casefold() not in GENERIC_NAMES}
        if not names:
            continue
        pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
        # 不替换更长单词里的同名片段；已打码的占位符再次处理时保持不变。
        s = re.sub(r"<用户>|<后台账号>|(?<!\w)(?:" + pattern + r")(?!\w)",
                   lambda m: m[0] if m[0] in ("<用户>", "<后台账号>") else placeholder, s, flags=re.I)
    return s


def mask(s: str, backend_names: tuple[str, ...] = ()) -> str:
    for rx, rep in SECRET_VAL:          # 先去密钥，再给号码打码（否则号码规则会先截断密钥）
        s = rx.sub(rep, s)
    return logtools.QQ.sub(r"\1****\2", _mask_identity(s, backend_names))


NC_STATE = {"online": "在线", "need_qr": "登录过期，要扫码", "connecting": "启动了但没连上 AstrBot", "stopped": "没在跑"}
TIMEY = re.compile(r"time|_at$|^at$|date|port|size|bytes|limit|budget|seconds|ms$", re.I)


def _clean(v, key: str = "", backend_names: tuple[str, ...] = ()):
    """去掉密钥类字段、给号码打码（递归）。"""
    if isinstance(v, dict):
        return {k: ("（已隐去）" if SECRET_KEY.search(str(k)) and x not in ("", None, [], {}) else _clean(x, str(k), backend_names))
                for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x, key, backend_names) for x in v]
    if isinstance(v, str):
        return mask(v, backend_names)
    if isinstance(v, int) and not isinstance(v, bool) and not TIMEY.search(key) and 9999 < v < 10**12:
        m = mask(str(v), backend_names)
        return m if m != str(v) else v
    return v


def _checks(s: dict) -> tuple[str, list[dict]]:
    try:
        if s.get("setup_done"):
            import manage
            return "故障诊断", manage.diagnose()
        import installer
        return "环境检查", installer.check_env(s.get("home") or str(LOG_DIR.parent))
    except Exception as e:  # noqa: BLE001
        log_exc()
        return "检查", [{"ok": False, "title": "检查本身出错了", "detail": str(e), "fix": ""}]


def _versions(s: dict) -> list[str]:
    out = [f"整合包：{VERSION}"]
    try:
        import versions
        st = versions.status()
        if st.get("repo"):
            out.append(f"整合包版本库：{st.get('head', '')}（{st.get('branch') or '停在旧版本'}）"
                       + ("（有本地改动）" if st.get("dirty") else ""))
    except Exception:
        pass
    try:
        import updater
        for name, fn in (("AstrBot", updater.ab_current), ("NapCat", updater.nc_current)):
            out.append(f"{name}：{fn() or '未安装'}")
    except Exception:
        pass
    out.append(f"Windows：{platform.system()} {platform.release()}（{platform.version()}）")
    out.append(f"Python（控制台）：{platform.python_version()}")
    return out


def _summary(s: dict, title: str, checks: list[dict], job: dict, backend_names: tuple[str, ...] = ()) -> str:
    L = [f"大肥鱼整合包 · 问题报告", f"生成于 {dt.datetime.now():%Y-%m-%d %H:%M:%S}", "",
         "QQ 号、群号只保留头两位和末两位，密钥和密码已去掉。",
         "用户目录中的用户名已隐去；其他位置的个人用户名和后台账号已打码，通用名称和少于 3 个字符的名字保留。",
         "", "【版本】"]
    L += ["  " + x for x in _versions(s)]
    head, L = L, []                     # 版本号里的数字不打码
    L += ["", f"【安装】", f"  装好了：{'是' if s.get('setup_done') else '否（向导停在「' + str(s.get('setup_step', '')) + '」）'}",
          f"  安装方式：{s.get('mode', '')}", f"  安装位置：{s.get('home', '')}"]
    try:
        st = services.status()
        ab, nc = st.get("astrbot") or {}, st.get("napcat") or {}
        L += ["", "【现在的状态】", f"  AstrBot：{'在运行' if ab.get('running') else '没在跑'}" + (f"（端口 {ab['port']}）" if ab.get("port") else ""),
              f"  NapCat：{NC_STATE.get(nc.get('state', ''), nc.get('state', ''))}"]
    except Exception:
        pass
    L += ["", f"【{title}】"]
    for c in checks:
        L.append(f"  {'✓' if c.get('ok') else ('!' if c.get('warn') or c.get('warn_only') else '✗')} {c.get('title', '')}：{c.get('detail', '')}")
        if not c.get("ok") and c.get("fix"):
            L.append(f"      处理：{c['fix']}")
    if job.get("name"):
        L += ["", "【最近一次操作】", f"  {job['name']}：{'成功' if job.get('ok') else '失败'} · {job.get('msg', '')}",
              f"  时间：{dt.datetime.fromtimestamp(job.get('at') or 0):%Y-%m-%d %H:%M:%S}"]
        t = job.get("trouble") or {}
        if t:
            L += [f"  判断：{t.get('title', '')}", f"  原因：{t.get('cause', '')}"]
            L += [f"    {i}. {x}" for i, x in enumerate(t.get("steps") or [], 1)]
    return "\n".join(head) + "\n" + mask("\n".join(L), backend_names) + "\n"


def build(context: dict | None = None, reveal: bool = True) -> dict:
    """context：界面上正在看的问题卡（可选），一并写进报告。reveal：生成后在资源管理器里选中（问题反馈里不弹）。"""
    s = load_state()
    # 先读取两处后台登录名，保证纯数字账号也在号码打码之前被识别。
    names = [s.get("dashboard_user")]
    config, config_loaded = None, False
    d = astrbot_data()
    if d:
        f = d / "cmd_config.json"
        try:
            if f.exists():
                config = json.loads(f.read_text(encoding="utf-8-sig"))
                config_loaded = True
                dashboard = config.get("dashboard") if isinstance(config, dict) else None
                if isinstance(dashboard, dict):
                    names.append(dashboard.get("username"))
        except Exception:
            log_exc()
    backend_names = tuple(name for name in names if isinstance(name, str) and name)
    title, checks = _checks(s)
    job = dict(services.job)
    if context and context.get("title"):
        job.setdefault("trouble", None)
        job = {**job, "trouble": job.get("trouble") or context}
    out = logtools.EXPORT
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"问题报告-{dt.datetime.now():%Y%m%d-%H%M}.zip"
    files = {"01-概况.txt": _summary(s, title, checks, job, backend_names)}
    if job.get("lines"):
        files["02-最近一次操作的输出.txt"] = mask("\n".join(job["lines"]), backend_names) + "\n"
    st = {k: v for k, v in s.items() if k not in ("wizard",)}
    files["03-设置（已脱敏）.json"] = json.dumps(_clean(st, backend_names=backend_names), ensure_ascii=False, indent=2)
    logs = [("04-控制台日志.txt", LOG_DIR / "console.log", 800),
            ("05-AstrBot日志.txt", services.astrbot_log_file(), 1200)]
    for name, p, n in logs:
        try:
            lines = [logtools.ANSI.sub("", ln) for ln in tail_lines(p, n)]
        except Exception:
            lines = []
        files[name] = (f"# {p.name} 最后 {n} 行\n" + mask("\n".join(lines) or "（没有日志）", backend_names)) + "\n"
    # NapCat 日志大半是 QQ 内核记录和二维码字符画：只留有内容的行，往前多看一些，才找得到掉线那一刻
    try:
        nlines = logtools.meaningful_tail(LOG_DIR / "napcat.log", 800)
    except Exception:
        nlines = []
    ntext = re.sub(r"(txz\.qq\.com/p\?k=)[^&\s]+", r"\1***", "\n".join(nlines))   # 登录二维码链接不外传
    files["06-NapCat日志.txt"] = ("# napcat.log 最后 800 条有效记录（已去掉 QQ 内核记录和二维码图案）\n"
                                 + mask(ntext or "（没有日志）", backend_names)) + "\n"
    evs = (s.get("offline_events") or [])[-20:]
    if evs:
        files["08-掉线记录.txt"] = "\n".join(
            f"{dt.datetime.fromtimestamp(e.get('at') or 0):%Y-%m-%d %H:%M:%S}  {e.get('reason', '')}"
            + (f"（{dt.datetime.fromtimestamp(e['back_at']):%H:%M:%S} 恢复）" if e.get("back_at") else "（未恢复）")
            for e in evs) + "\n"
    if config_loaded:
        files["07-AstrBot配置（已脱敏）.json"] = json.dumps(
            _clean(config, backend_names=backend_names), ensure_ascii=False, indent=2)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            # 版本段、掉线记录和 JSON 字段名也要隐去身份信息，保留原有号码处理规则。
            z.writestr(n, ("﻿" + _mask_identity(t, backend_names)).encode("utf-8"))
    size = path.stat().st_size
    log(f"生成问题报告：{path.name}（{size // 1024} KB）")
    if reveal:
        logtools.reveal(path)
    return {"ok": True, "path": str(path), "name": path.name, "size": size}
