"""接入已有的 AstrBot 和 NapCat：检测安装位置、原位接管、按勾选做增量配置、解除接入。

原则：只把路径写进 state.json，不移动、不复制、不覆盖用户数据；会改动的文件先备份；
「解除接入」只移除控制台自己的快捷方式、自启和状态，绝不删除用户原有的 AstrBot、NapCat 目录和数据。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from core import (ASTRBOT_VERSION, KIT, WIN, load_state, log, ps, read_json, update_state)

MIN_ASTRBOT = "4.26.0"      # 控制台依赖的 /api/v1 后台接口从 4.26.0 起齐全（逐项核对过 AstrBot 各版本的接口路由）
UNSUPPORTED = ("Docker 部署的 AstrBot", "非 Windows 系统", "非 Shell 版的 NapCat（例如 NapCat.Framework 或 Docker 版）")

# 「采用推荐的群聊和缓存设置」逐项列出：配置项 → 显示名称。默认人设不在此列（接入时不替换当前人设）。
RECOMMENDED = {
    "provider_settings.identifier": "识别消息发送者身份",
    "provider_settings.datetime_system_prompt": "在提示中附带当前时间",
    "provider_settings.computer_use_runtime": "计算机操作工具（推荐关闭）",
    "platform_settings.segmented_reply.enable": "分段回复",
    "platform_settings.segmented_reply.only_llm_result": "只对模型回复分段",
    "platform_settings.segmented_reply.split_mode": "分段方式",
    "platform_settings.segmented_reply.regex": "分段规则",
    "platform_settings.segmented_reply.split_words": "分段标点",
    "platform_settings.empty_mention_waiting": "仅 @ 时等待下一条消息",
    "provider_ltm_settings.active_reply.enable": "AstrBot 内置主动回复（推荐关闭，由插件负责）",
    "agent_runner.config.compression.max_turns": "聊天记录达到多少轮时截断（缓存优化）",
    "agent_runner.config.compression.trim_turns": "每次截断的轮数（缓存优化）",
}


def _v(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(s or ""))[:3]) or (0,)


def version_status(ver: str) -> dict:
    """低于最低版本：需要更新；高于整合包适配版本：允许接入，提示可能存在差异。"""
    if not ver:
        return {"level": "warn", "text": "未能读取版本号，接入后部分功能可能无法使用。"}
    if _v(ver) < _v(MIN_ASTRBOT):
        return {"level": "bad", "text": f"版本 {ver} 低于控制台所需的最低版本 {MIN_ASTRBOT}，控制台依赖的后台接口尚未提供。请先备份再更新 AstrBot。"}
    if _v(ver) > _v(ASTRBOT_VERSION):
        return {"level": "warn", "text": f"版本 {ver} 高于整合包适配的 {ASTRBOT_VERSION}，可以接入，个别功能可能存在差异。"}
    return {"level": "ok", "text": f"版本 {ver}，满足要求。"}


# ---------------------------------------------------------------- AstrBot

def _read_cfg(root: Path) -> dict:
    try:
        return read_json(root / "data" / "cmd_config.json")
    except Exception:
        return {}


def _astrbot_version(root: Path, py: Path | None) -> str:
    try:
        if (root / "pyproject.toml").is_file():
            m = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1)
        if py:
            for d in sorted(py.parent.parent.glob("Lib/site-packages/astrbot-*.dist-info")):
                m = re.match(r"astrbot-([\d.]+)", d.name)
                if m:
                    return m.group(1).rstrip(".")
    except Exception:
        pass
    return ""


def _find_python(root: Path) -> Path | None:
    for base in (root, root.parent):
        for name in ("venv", ".venv", "astrbot-venv", "env"):
            for exe in ("pythonw.exe", "python.exe") if WIN else ("python3", "python"):
                p = base / name / ("Scripts" if WIN else "bin") / exe
                if p.is_file():
                    return p
    return None


def _platform(cfg: dict) -> dict:
    for pl in cfg.get("platform", []) or []:
        if pl.get("type") == "aiocqhttp" and pl.get("enable", True):
            return pl
    return {}


def inspect_astrbot(root: str | Path, python: str | Path | None = None, source: str = "手动选择") -> dict:
    root = Path(root)
    if (root / "core" / "data" / "cmd_config.json").is_file():      # 选到了启动器的实例目录
        root = root / "core"
    if (root.name.lower() == "data") and (root / "cmd_config.json").is_file():
        root = root.parent
    out = {"root": str(root), "source": source, "ok": False, "problems": []}
    if not (root / "data" / "cmd_config.json").is_file():
        out["problems"].append("此文件夹中没有 data\\cmd_config.json，不是 AstrBot 的运行目录。请选择包含 data 文件夹的那一层。")
        return out
    py = Path(python) if python else None
    launcher = ".astrbot_launcher" in str(root).lower()
    if launcher and not py:
        cand = root.parent / "venv" / ("Scripts" if WIN else "bin") / ("pythonw.exe" if WIN else "python3")
        py = cand if cand.exists() else None
    py = py or _find_python(root)
    source_run = (root / "main.py").is_file() and (root / "astrbot").is_dir()
    cfg = _read_cfg(root)
    pl = _platform(cfg)
    ver = _astrbot_version(root, py)
    out.update({
        "mode": "launcher" if source_run else "pip", "kind": "启动器实例" if launcher else ("源码运行" if source_run else "pip 安装"),
        "python": str(py) if py else "", "cmd": ["main.py"] if source_run else ["-m", "astrbot.cli", "run"],
        "version": ver, "version_status": version_status(ver),
        "port": int(((cfg.get("dashboard") or {}).get("port")) or 6185),
        "dashboard_user": (cfg.get("dashboard") or {}).get("username", ""),
        "ws_port": int(pl.get("ws_reverse_port") or 0), "ws_token": pl.get("ws_reverse_token", ""), "has_qq_platform": bool(pl),
        "admins": [str(a) for a in cfg.get("admins_id", []) if str(a).isdigit()],
        "system_python": bool(py) and not (py.parent.parent / "pyvenv.cfg").is_file(),
    })
    if not py:
        out["problems"].append("未找到运行这个 AstrBot 的 Python。请确认它使用虚拟环境（venv）运行，或先启动 AstrBot 再重新检测。")
    if out["version_status"]["level"] == "bad":
        out["problems"].append(out["version_status"]["text"])
    out["ok"] = not out["problems"]
    return out


def _launcher_roots() -> list[Path]:
    base = Path.home() / ".astrbot_launcher" / "instances"
    return sorted(p / "core" for p in base.glob("*") if (p / "core" / "data" / "cmd_config.json").is_file()) if base.is_dir() else []


def _common_astrbot_roots() -> list[Path]:
    home = Path.home()
    bases = [home, home / "Desktop", home / "Documents", home / "Downloads", KIT.parent]
    if WIN:
        bases += [Path(f"{d}:\\") for d in "CDEF" if Path(f"{d}:\\").exists()]
    out = []
    for b in bases:
        for name in ("AstrBot", "astrbot", "AstrBot-master", "astrbot-main"):
            p = b / name
            if (p / "data" / "cmd_config.json").is_file():
                out.append(p)
    return out


def _processes() -> list[dict]:
    """正在运行的 AstrBot / NapCat 进程（可执行文件路径和命令行）。"""
    if not WIN:
        return []
    out = ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='NapCatWinBootMain.exe' or Name='QQ.exe'\" | "
             "ForEach-Object { $_.ProcessId.ToString() + \"`t\" + $_.Name + \"`t\" + $_.ExecutablePath + \"`t\" + $_.CommandLine }", timeout=20)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            rows.append({"pid": parts[0], "name": parts[1], "exe": parts[2], "cmd": parts[3]})
    return rows


def detect() -> dict:
    procs = _processes()
    found: dict[str, dict] = {}

    def add(root: Path, python=None, source=""):
        key = str(root).lower()
        if key in found:
            return
        found[key] = inspect_astrbot(root, python, source)

    for p in procs:
        if p["name"].lower().startswith("python") and re.search(r"astrbot|main\.py", p["cmd"], re.I):
            m = re.search(r'([A-Za-z]:\\[^"]*?)\\main\.py', p["cmd"])
            if m:
                add(Path(m.group(1)), p["exe"], "正在运行")
            else:
                py = Path(p["exe"])
                for guess in (py.parent.parent.parent / "astrbot", py.parent.parent.parent, py.parent.parent.parent / "core"):
                    if (guess / "data" / "cmd_config.json").is_file():
                        add(guess, p["exe"], "正在运行")
                        break
    for r in _launcher_roots():
        add(r, None, "启动器实例")
    for r in _common_astrbot_roots():
        add(r, None, "常见位置")
    napcats: dict[str, dict] = {}
    for p in procs:
        if p["exe"] and p["name"] in ("NapCatWinBootMain.exe", "QQ.exe"):
            for anc in Path(p["exe"]).parents:
                if re.fullmatch(r"NapCat\..*\.Shell", anc.name, re.I):
                    napcats.setdefault(str(anc).lower(), inspect_napcat(anc, "正在运行"))
                    break
    for d in _shell_dirs():
        napcats.setdefault(str(d).lower(), inspect_napcat(d, "常见位置"))
    ab = list(found.values())
    nc = list(napcats.values())
    for n in nc:                       # 标出与哪个 AstrBot 相连
        n["linked"] = [a["root"] for a in ab if a.get("ws_port") and a["ws_port"] in n.get("ws_ports", [])
                       and (not a.get("ws_token") or a["ws_token"] in n.get("ws_tokens", []))]
    return {"astrbot": ab, "napcat": nc, "min_version": MIN_ASTRBOT, "adapted": ASTRBOT_VERSION,
            "unsupported": list(UNSUPPORTED), "autostart": external_autostart()}


# ---------------------------------------------------------------- NapCat

def _shell_dirs() -> list[Path]:
    home = Path.home()
    roots = [KIT.parent, home / "Desktop", home / "Downloads", home / "Documents", home]
    if WIN:
        roots += [Path(f"{d}:\\") for d in "CDEF" if Path(f"{d}:\\").exists()]
    out, seen = [], set()

    def walk(d: Path, depth: int):
        if depth < 0:
            return
        try:
            for e in os.scandir(d):
                if not e.is_dir(follow_symlinks=False) or e.name.startswith((".", "$")) or e.name in ("Windows", "Program Files", "Program Files (x86)", "ProgramData", "AppData", "node_modules"):
                    continue
                p = Path(e.path)
                if re.fullmatch(r"NapCat\..*\.Shell", e.name, re.I) and (p / "NapCatWinBootMain.exe").is_file():
                    if str(p).lower() not in seen:
                        seen.add(str(p).lower())
                        out.append(p)
                    continue
                walk(p, depth - 1)
        except OSError:
            pass
    for r in roots:
        walk(r, 1 if len(r.parts) <= 1 else 3)
    return out


def inspect_napcat(shell: str | Path, source: str = "手动选择") -> dict:
    shell = Path(shell)
    if not re.fullmatch(r"NapCat\..*\.Shell", shell.name, re.I):
        inner = sorted(p for p in shell.glob("NapCat.*.Shell") if (p / "NapCatWinBootMain.exe").is_file())
        if inner:
            shell = inner[-1]
    out = {"dir": str(shell), "source": source, "ok": False, "problems": [], "ws_ports": [], "ws_tokens": [], "qq": ""}
    if not (shell / "NapCatWinBootMain.exe").is_file():
        out["problems"].append("此文件夹不是 Shell 版的 NapCat（没有 NapCatWinBootMain.exe）。请选择 NapCat.xxx.Shell 文件夹。")
        return out
    apps = sorted(shell.glob("versions/*/resources/app/napcat"))
    cfgd = apps[-1] / "config" if apps else None
    ver = ""
    try:
        f = apps[-1] / "napcat.mjs" if apps else None
        if f and f.is_file():
            m = re.search(r'"(\d+\.\d+\.\d+)"\s*\|\|\s*"1\.0\.0-dev"', f.read_text(encoding="utf-8", errors="replace"))
            ver = m.group(1) if m else ""
    except OSError:
        pass
    qqs = []
    if cfgd and cfgd.is_dir():
        for f in sorted(cfgd.glob("onebot11_*.json")):
            q = f.stem.split("_", 1)[1]
            if q.isdigit():
                qqs.append(q)
            try:
                net = read_json(f).get("network", {})
            except Exception:
                continue
            for c in net.get("websocketClients", []) or []:
                m = re.search(r":(\d+)", str(c.get("url", "")))
                if c.get("enable", True) and m:
                    out["ws_ports"].append(int(m.group(1)))
                    out["ws_tokens"].append(str(c.get("token", "")))
        try:
            w = read_json(cfgd / "webui.json")
            out["webui_port"] = int(w.get("port", 6099))
            out["qq"] = str(w.get("autoLoginAccount") or "")
        except Exception:
            pass
    out["qq"] = out["qq"] or (qqs[-1] if qqs else "")
    out.update({"version": ver, "config_dir": str(cfgd) if cfgd else "", "accounts": qqs})
    if not apps:
        out["problems"].append("NapCat 目录不完整（缺少 versions 文件夹），请先用 NapCat 自带的方式启动一次。")
    out["ok"] = not out["problems"]
    return out


# ---------------------------------------------------------------- 开机自启冲突

def external_autostart() -> list[str]:
    """启动器或其他程序设置的 AstrBot / NapCat 开机自启（只读查看，不修改）。"""
    if not WIN:
        return []
    hits = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if re.search(r"astrbot|napcat", f"{name} {val}", re.I) and "dafeiyu" not in f"{name} {val}".lower():
                    hits.append(name)
    except OSError:
        pass
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    if startup.is_dir():
        hits += [p.stem for p in startup.glob("*.lnk") if re.search(r"astrbot|napcat", p.stem, re.I) and "大肥鱼" not in p.stem]
    return hits


# ---------------------------------------------------------------- 选择文件夹（Windows 自带的文件夹选择窗口）

def pick_folder(kind: str) -> dict:
    title = "请选择 AstrBot 的运行目录（包含 data 文件夹）" if kind == "astrbot" else "请选择 NapCat.xxx.Shell 文件夹"
    script = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
              f"$d.Description = '{title}'; $d.ShowNewFolderButton = $false; "
              "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
              "if ($d.ShowDialog($f) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.SelectedPath }")
    path = ps(script, timeout=600).strip().splitlines()
    path = path[-1].strip() if path else ""
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "item": inspect_astrbot(path) if kind == "astrbot" else inspect_napcat(path)}


# ---------------------------------------------------------------- 推荐设置：逐项对比

def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _show(v) -> str:
    if v is None:
        return "未设置"
    if isinstance(v, bool):
        return "开启" if v else "关闭"
    if isinstance(v, list):
        return " ".join(map(str, v)) or "空"
    return str(v)


def recommended_diff(cfg: dict) -> list[dict]:
    from core import PACK_DIR
    base = json.loads((PACK_DIR / "settings" / "pack.json").read_text(encoding="utf-8-sig"))["base"]
    out = []
    for key, label in RECOMMENDED.items():
        if key not in base:
            continue
        cur, new = _get(cfg, key), base[key]
        if cur != new:
            out.append({"key": key, "label": label, "current": _show(cur), "recommended": _show(new)})
    return out


def apply_recommended(cfg: dict, keys: list[str]) -> dict:
    from core import PACK_DIR
    base = json.loads((PACK_DIR / "settings" / "pack.json").read_text(encoding="utf-8-sig"))["base"]
    cfg = copy.deepcopy(cfg)
    for k in keys:
        if k in RECOMMENDED and k in base:
            _set(cfg, k, base[k])
    return cfg


# ---------------------------------------------------------------- 接入

def files_to_backup(ab: dict, nc: dict | None) -> list[Path]:
    out = [Path(ab["root"]) / "data" / "cmd_config.json"]
    if nc and nc.get("config_dir"):
        cfgd = Path(nc["config_dir"])
        out += sorted(cfgd.glob("onebot11_*.json")) + ([cfgd / "webui.json"] if (cfgd / "webui.json").is_file() else [])
    return [p for p in out if p.is_file()]


def backup_files(paths: list[Path], dest_root: Path) -> Path:
    dest = dest_root / time.strftime("%Y%m%d-%H%M%S-接入前")
    for p in paths:
        rel = Path(p.drive.rstrip(":") or "root", *p.parts[1:]) if p.is_absolute() else p
        t = dest / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)
    (dest / "说明.txt").write_text("接入已有的 AstrBot 和 NapCat 之前自动备份的配置文件，按原路径存放。\n" +
                                   "\n".join(str(p) for p in paths) + "\n", encoding="utf-8")
    return dest


def state_for(ab: dict, nc: dict | None, owners: list[str], bot_qq: str) -> dict:
    """接入后写进 state.json 的内容：只有路径和账号，不复制任何用户数据。"""
    py = Path(ab["python"])
    pyw = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").exists() else py
    kw = {"mode": ab["mode"], "attached": True, "astrbot_root": ab["root"], "astrbot_python": str(pyw),
          "astrbot_cmd": list(ab["cmd"]), "astrbot_port": int(ab.get("port") or 6185),
          "napcat_dir": nc["dir"] if nc else "", "owners": owners, "bot_qq": bot_qq,
          "home": load_state().get("home") or str(KIT.parent)}
    return kw


def detach() -> str:
    """解除接入：移除控制台自己的快捷方式、自启和状态；不停止、不删除用户原有的 AstrBot 和 NapCat。"""
    import installer
    installer.remove_shortcuts()
    s = load_state()
    kept = [x for x in (s.get("astrbot_root"), s.get("napcat_dir")) if x]
    update_state(setup_done=False, setup_step="welcome", wizard={}, attached=False, autostart=False,
                 astrbot_root="", astrbot_python="", astrbot_cmd=[], napcat_dir="", mode="pip",
                 wanted={"napcat": False, "astrbot": False}, dashboard_pass="")
    log("已解除接入，保留原有安装：" + "；".join(kept))
    return ("已解除接入：控制台的桌面图标、开机自启和接入状态已移除。原有的 AstrBot 和 NapCat 及其数据全部保留，"
            "可以继续用原来的方式启动。" + ("保留的目录：" + "；".join(kept) if kept else ""))


def is_attached(s: dict | None = None) -> bool:
    s = s or load_state()
    return bool(s.get("attached")) or s.get("mode") == "launcher"


# ---------------------------------------------------------------- 接入任务（后台任务中执行）

def _link_napcat(ab: dict, shell: Path, bot_qq: str) -> None:
    """新装的 NapCat 连到已有的 AstrBot：AstrBot 已有 QQ 平台时沿用它的端口和令牌；没有时新增一个，不改动其他平台。"""
    import secrets
    from core import write_json
    cfg_path = Path(ab["root"]) / "data" / "cmd_config.json"
    cfg = read_json(cfg_path)
    pl = _platform(cfg)
    if not pl:
        pl = {"id": "大肥鱼", "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1",
              "ws_reverse_port": 6199, "ws_reverse_token": secrets.token_hex(20)}
        cfg.setdefault("platform", []).append(pl)
        write_json(cfg_path, cfg)
    apps = sorted(shell.glob("versions/*/resources/app/napcat"))
    if not apps:
        raise RuntimeError("NapCat 目录不完整，请重新安装 NapCat。")
    cfgd = apps[-1] / "config"
    cfgd.mkdir(parents=True, exist_ok=True)
    ob = cfgd / f"onebot11_{bot_qq}.json"
    try:
        oc = read_json(ob)
    except Exception:
        oc = {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketServers": [],
                          "websocketClients": [], "plugins": []}}
    net = oc.setdefault("network", {})
    wsc = [x for x in net.get("websocketClients", []) if x.get("name") != "大肥鱼-AstrBot"]
    wsc.append({"name": "大肥鱼-AstrBot", "enable": True, "url": f"ws://127.0.0.1:{pl.get('ws_reverse_port', 6199)}/ws",
                "messagePostFormat": "array", "reportSelfMessage": False, "reconnectInterval": 1000,
                "token": pl.get("ws_reverse_token", ""), "debug": False, "heartInterval": 1000})
    net["websocketClients"] = wsc
    write_json(ob, oc, backup=False)


def apply(body: dict) -> str:
    """接入：备份 → 写入路径 → 连接后台 → 按勾选增量配置。"""
    import astrbot_api
    import dashlogin
    import plugin_sync
    import services
    from core import protect
    from services import job_line, job_progress

    ab = inspect_astrbot(body["astrbot"]["root"], body["astrbot"].get("python") or None, body["astrbot"].get("source", ""))
    if not ab["ok"]:
        raise RuntimeError("AstrBot 不满足接入条件：" + "；".join(ab["problems"]))
    nc = inspect_napcat(body["napcat"]["dir"], body["napcat"].get("source", "")) if body.get("napcat") else None
    if nc and not nc["ok"]:
        raise RuntimeError("NapCat 不满足接入条件：" + "；".join(nc["problems"]))
    owners = [o for o in (str(x).strip() for x in body.get("owners", [])) if re.fullmatch(r"\d{5,12}", o)]
    if not owners:
        raise RuntimeError("请填写主人的 QQ 号")
    bot_qq = str(body.get("bot_qq") or (nc or {}).get("qq") or "").strip()
    if body.get("install_napcat"):
        import installer
        raise RuntimeError(installer.NAPCAT_GONE)

    job_line("正在备份将会改动的配置文件…")
    job_progress(0.05)
    home = Path(load_state().get("home") or KIT.parent)
    dest = backup_files(files_to_backup(ab, nc), home / "Backups")
    job_line(f"已备份到 {dest}")

    update_state(**state_for(ab, nc, owners, bot_qq), pack_migrate_rev=plugin_sync.MIGRATE_REV,
                 plugins_opt_out=not body.get("plugins"), boot_skip_astrbot=body.get("boot") == "external",
                 attach_backup=str(dest))
    job_progress(0.15)

    login = body.get("login") or {}
    if login.get("reset"):
        job_line("正在重设 AstrBot 后台密码（需要短暂重启 AstrBot）…")
        services.stop_astrbot()
        user = ab.get("dashboard_user") or "admin"
        pw = dashlogin.gen_password()
        py = Path(ab["python"])
        dashlogin.write_login(user, pw, py=py.with_name("python.exe") if py.with_name("python.exe").exists() else py,
                              cfg=Path(ab["root"]) / "data" / "cmd_config.json")
        update_state(dashboard_user=user, dashboard_pass=protect(pw))
        astrbot_api._tok.update(token=None, at=0.0)
    job_line("正在启动或接管 AstrBot…")
    job_line(services.start_astrbot())
    job_progress(0.35)
    if not login.get("reset"):
        if not astrbot_api.save_login(str(login.get("user", "")).strip(), str(login.get("password", ""))):
            raise RuntimeError("AstrBot 后台账号或密码不正确。请重新填写，或选择「重设后台密码」。")
    job_line("已连接 AstrBot 后台")

    steps = (["persona"] if body.get("persona") else []) + (["plugins", "plugin_config", "memes", "reload"] if body.get("plugins") else [])
    if steps:
        if not ab["has_qq_platform"]:
            job_line("AstrBot 中还没有 QQ 平台，暂不安装插件组和人设；接入 NapCat 后可在「记忆」页安装。")
        else:
            job_line("正在按勾选内容安装：" + "、".join({"persona": "大肥鱼人设", "plugins": "插件组"}.get(s, "") for s in steps if s in ("persona", "plugins")))
            run_import(steps, owners[0])
    job_progress(0.75)

    keys = [k for k in body.get("recommended") or [] if k in RECOMMENDED]
    if keys:
        job_line("正在应用所选的推荐设置…")
        conf = astrbot_api.call("GET", "/api/v1/config-profiles/default")
        conf = conf.get("config", conf)
        astrbot_api.call("PUT", "/api/v1/config-profiles/default", apply_recommended(conf, keys))

    # 选择的 NapCat 还没有连到这个 AstrBot 时补上连接（NapCat 已停止发布，不再提供安装，只能接入本机已有的）
    if nc and not (ab.get("ws_port") and ab["ws_port"] in nc.get("ws_ports", [])):
        if re.fullmatch(r"\d{5,12}", bot_qq):
            job_line("正在把 NapCat 连接到此 AstrBot（需要短暂重启 AstrBot）…")
            services.stop_astrbot()                # 改 cmd_config.json 前先停 AstrBot
            _link_napcat(ab, Path(nc["dir"]), bot_qq)
            services.start_astrbot()
        else:
            job_line("所选 NapCat 尚未登录过 QQ，无法自动连接；请在 NapCat 中登录 bot 的 QQ 号后，在「总览」中重启 NapCat。")
    job_progress(1.0)
    log(f"已接入已有安装：AstrBot {ab['root']}（{ab['kind']}），NapCat {(nc or {}).get('dir', '未接入')}，备份 {dest}")
    return "接入完成。原有的人设、模型、插件和聊天记录均已保留；改动前的配置备份在 " + str(dest)


def run_import(steps: list[str], owner: str) -> None:
    """调用配置包导入工具，只做列出的几步（增量，不覆盖原有设置）。"""
    import tempfile
    from core import unprotect, run_stream
    from services import job_line
    import installer
    s = load_state()
    work = installer._render_pack([owner] + [o for o in s["owners"] if o != owner])
    tmp = Path(tempfile.mkdtemp(prefix="dfy-"))
    try:
        sec = tmp / "secrets.txt"
        sec.write_text(f"password: {unprotect(s['dashboard_pass'])}", encoding="utf-8")
        af = tmp / "answers.json"
        af.write_text(json.dumps({"base_url": f"http://127.0.0.1:{s['astrbot_port']}", "username": s["dashboard_user"],
                                  "password_file": str(sec), "password_key": "password", "owner_qq": owner,
                                  "voice": "0", "memory": "0", "steps": steps, "persona_keep": True}, ensure_ascii=False), encoding="utf-8")
        py = Path(s["astrbot_python"]).with_name("python.exe")
        code = run_stream([str(py), "-X", "utf8", str(work / "tools" / "import_dafeiyu.py"), "--answers", str(af)],
                          job_line, cwd=str(work), timeout=3600)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if code != 0:
        raise RuntimeError("插件组或人设未全部安装完成，请查看上方输出后重试。")
    if "plugins" in steps:
        update_state(plugins_opt_out=False)


def install_plugins() -> str:
    """接入时没有勾选插件组、之后在「记忆」等页面补装。"""
    s = load_state()
    if not s.get("owners"):
        raise RuntimeError("尚未设置主人的 QQ 号")
    run_import(["plugins", "plugin_config", "memes", "reload"], s["owners"][0])
    import plugin_sync
    plugin_sync.sync()
    return "大肥鱼插件组已安装"
