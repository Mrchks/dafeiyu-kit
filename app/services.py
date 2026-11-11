"""启停与状态：AstrBot、NapCat、看门狗、后台任务。"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from core import (KIT, LOG_DIR, run, update_state, astrbot_data, cmd_config_path, kill_tree, listener_pid, listening_ports_of,
                  load_state, log, log_exc, napcat_app, napcat_cfg_dir, napcat_dir, netstat, pids_under, popen_detached,
                  port_of, port_open, ps, read_json, set_wanted, tail_lines, url_q, write_json)
import logtools
import troubles

_busy = threading.Lock()
_last_start = {"napcat": 0.0, "astrbot": 0.0}
notify_hook = None  # 托盘设置：notify_hook(title, text)


def notify(title: str, text: str) -> None:
    try:
        if notify_hook and load_state().get("notify", True):
            notify_hook(title, text)
    except Exception:
        log_exc()


# ---------------------------------------------------------------- AstrBot

def onebot_port() -> int:
    try:
        for pl in read_json(cmd_config_path()).get("platform", []):
            if pl.get("type") == "aiocqhttp" and pl.get("enable", True):
                return int(pl.get("ws_reverse_port", 6199))
    except Exception:
        pass
    return 6199


def bot_name() -> str:
    try:
        for pl in read_json(cmd_config_path()).get("platform", []):
            if pl.get("type") == "aiocqhttp":
                return pl.get("id") or "大肥鱼"
    except Exception:
        pass
    return "大肥鱼"


def astrbot_procs(allow_slow: bool = True) -> list[int]:
    """用这个 AstrBot 的 Python 环境跑起来的进程（包括还没开好端口、正在启动的）。"""
    s = load_state()
    if not s["astrbot_python"]:
        return []
    fast = pids_under(Path(s["astrbot_python"]).parent, ("python.exe", "pythonw.exe"))
    if fast is not None:
        return fast
    if not allow_slow:
        return []
    scripts = str(Path(s["astrbot_python"]).parent).replace("'", "''")
    out = ps("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { "
             f"$_.ExecutablePath -and $_.ExecutablePath.StartsWith('{scripts}', [System.StringComparison]::OrdinalIgnoreCase) }}"
             " | ForEach-Object { $_.ProcessId }")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def astrbot_info(rows=None, procs: bool = False, allow_slow: bool = True) -> dict:
    rows = rows if rows is not None else netstat()
    ob = onebot_port()
    want = load_state()["astrbot_port"]
    pid = listener_pid(ob, rows) or listener_pid(want, rows)
    if pid is None:
        starting = bool(procs and astrbot_procs(allow_slow))
        return {"running": False, "starting": starting, "pid": None, "port": None, "managed": False}
    ports = [p for p in listening_ports_of(pid, rows) if p != ob]
    port = want if want in ports else (ports[0] if ports else None)
    return {"running": True, "starting": False, "pid": pid, "port": port, "managed": port == want}


def _enable_file_log() -> None:
    try:
        p = cmd_config_path()
        c = read_json(p)
        if not c.get("log_file_enable") or not c.get("log_file_max_mb"):
            c["log_file_enable"] = True
            c.setdefault("log_file_path", "logs/astrbot.log")
            c["log_file_max_mb"] = c.get("log_file_max_mb") or 20     # 满 20MB 换一份，AstrBot 自己只留 3 份旧的
            write_json(p, c)
    except Exception:
        log_exc()


# ---------------------------------------------------------------- 启动进度（显示在右下角任务卡上）

_stage: dict[str, tuple[str, float | None]] = {}
_STAGE_NAMES = {"astrbot": "AstrBot", "napcat": "QQ"}
_all_starting = threading.Event()
_stage_shape = {"v": ""}


def _stage_set(key: str, text: str, prog: float | None) -> None:
    """AstrBot 和 NapCat 同时启动时，把两边的进度拼成一行给任务卡看。"""
    if _stage.get(key) == (text, prog):
        return
    _stage[key] = (text, prog)
    if not job["running"]:
        return
    parts = [f"{_STAGE_NAMES[k]}：{_stage[k][0]}" for k in ("astrbot", "napcat") if k in _stage]
    line = " · ".join(parts)
    shape = re.sub(r"\d+", "#", line)            # 只是秒数、序号在变的话不写日志
    job_line(line, quiet=shape == _stage_shape["v"])
    _stage_shape["v"] = shape
    progs = [v[1] for v in _stage.values() if v[1] is not None]
    if progs:
        job_progress(min(1.0, sum(progs) / len(progs)))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plugin_total() -> int:
    n = 0
    d = astrbot_data()
    if d and (d / "plugins").is_dir():
        n += sum(1 for x in (d / "plugins").iterdir() if x.is_dir() and not x.name.startswith((".", "_")))
    r = load_state().get("astrbot_root")
    b = Path(r) / "astrbot" / "builtin_stars" if r else None
    n += sum(1 for x in b.iterdir() if x.is_dir() and not x.name.startswith((".", "_"))) if b and b.is_dir() else 2
    return max(n, 1)


class _BootWatch:
    """边等 AstrBot 启动边看它的输出：现在到哪一步了，每一步花了多久。"""

    def __init__(self, out: Path, pos: int, t0: float, total: int) -> None:
        self.out, self.pos, self.t0, self.total = out, pos, t0, total
        self.t_core = self.t_web = None
        self.n = 0
        self.last = ""

    def poll(self) -> None:
        try:
            with open(self.out, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            data = b""
        for line in _ANSI.sub("", data.decode("utf-8", "replace")).splitlines():
            if self.t_core is None and "core_lifecycle" in line and "AstrBot v" in line:
                self.t_core = time.time()
            m = re.search(r"Loading plugin (\S+)", line)
            if m:
                self.n += 1
                self.last = m.group(1)
            if self.t_web is None and ("Starting WebUI" in line or "AstrBot started" in line):
                self.t_web = time.time()
        el = int(time.time() - self.t0)
        if self.t_core is None:
            _stage_set("astrbot", f"正在加载程序（{el} 秒）", min(0.35, 0.35 * el / 70))
        elif self.t_web is None:
            n = min(self.n, self.total)
            name = self.last.replace("astrbot_plugin_", "")
            _stage_set("astrbot", f"加载插件 {n}/{self.total}" + (f"（{name}）" if name else ""),
                       0.35 + 0.5 * n / self.total)
        else:
            _stage_set("astrbot", "正在打开后台", 0.92)

    def done(self) -> None:
        self.poll()
        now = time.time()
        core = (self.t_core or now) - self.t0
        plug = (self.t_web or now) - (self.t_core or now)
        total = now - self.t0
        update_state(last_boot={"at": now, "load": round(core, 1), "plugins": round(plug, 1), "total": round(total, 1)})
        log(f"AstrBot 启动耗时：加载程序 {core:.0f} 秒，加载插件 {plug:.0f} 秒，一共 {total:.0f} 秒")


def start_astrbot(wait: bool = True) -> str:
    s = load_state()
    if not s["astrbot_python"] or not s["astrbot_root"]:
        return "AstrBot 尚未安装完成"
    info = astrbot_info(procs=True)
    if info["running"]:
        if info["managed"]:
            return "AstrBot 已在运行"
        stop_astrbot()
    elif info.get("starting"):
        # 上一次启动还没开好端口（刚开机时可能要两三分钟），等它，别再起一个
        log("AstrBot 正在启动中，等它开好")
        gone = False
        for n in range(240):
            time.sleep(1)
            if port_open(s["astrbot_port"]) and port_open(onebot_port()):
                return "AstrBot 已启动"
            if n % 15 == 14 and not astrbot_procs():
                gone = True  # 那个进程自己退出了，下面重新启动
                break
        if not gone:
            return "AstrBot 启动较慢，仍在启动中"
    _enable_file_log()
    py = Path(s["astrbot_python"])
    env = astrbot_env(s)
    cwd = Path(s["astrbot_root"])
    cmd = [str(py)] + list(s["astrbot_cmd"])
    out = LOG_DIR / "astrbot.out.log"
    logtools.rotate("astrbot.out.log")      # 太大就先挪去归档
    pos0 = out.stat().st_size if out.exists() else 0
    _stage_set("astrbot", "正在启动", 0.02)
    proc = popen_detached(cmd, cwd, env, out, console_app=False)
    update_state(astrbot_launch_rev=LAUNCH_REV)   # 记一下：这次是带着新环境变量启动的（旧控制台起的 AstrBot 要重启一次）
    t0 = _last_start["astrbot"] = time.time()
    log(f"启动 AstrBot（端口 {s['astrbot_port']}）：{cmd} 在 {cwd}")
    if not wait:
        return "AstrBot 启动中"
    watch = _BootWatch(out, pos0, t0, _plugin_total())
    for i in range(240):
        time.sleep(1)
        watch.poll()
        if i == 5 and proc.poll() is not None and not port_open(s["astrbot_port"]):
            log(f"AstrBot 进程马上退出了，退出码 {proc.returncode}")
            _stage_set("astrbot", "启动失败", None)
            return f"AstrBot 启动失败（退出码 {proc.returncode}），请查看日志"
        if port_open(s["astrbot_port"]) and port_open(onebot_port()):
            watch.done()
            _stage_set("astrbot", "已启动", 1.0)
            return "AstrBot 已启动"
    _stage_set("astrbot", "启动超时", None)
    return "AstrBot 启动超时，请查看日志"


# 启动 AstrBot 时的环境变量有变化、需要重启 AstrBot 才生效时加 1（总览/后台页会提示重启）
# 1：带上 ASTRBOT_LAUNCHER=1。AstrBot 给插件自带的页面（比如表情包管理器）加了「只能被 AstrBot 自己的网页套着显示」，
#    控制台里套着 AstrBot 后台算「别人」，插件页面就显示「127.0.0.1 拒绝连接」。这个变量是 AstrBot 给桌面启动器留的开关，
#    带上后插件页面才能在控制台里打开。（后台只对本机开放，不影响安全。）
LAUNCH_REV = 1


def astrbot_needs_restart(s: dict, ab: dict) -> bool:
    """正在跑的 AstrBot 是不是旧控制台用旧环境变量启动的。"""
    if not (ab.get("running") and ab.get("managed")):
        return False
    if int(s.get("astrbot_launch_rev") or 0) >= LAUNCH_REV:
        return False
    return str((s.get("astrbot_env") or {}).get("ASTRBOT_LAUNCHER", "")) not in ("1", "true")


def astrbot_env(s: dict | None = None) -> dict:
    s = s or load_state()
    py = Path(s["astrbot_python"])
    env = os.environ.copy()
    env.update({"DASHBOARD_HOST": "127.0.0.1", "DASHBOARD_PORT": str(s["astrbot_port"]),
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "VIRTUAL_ENV": str(py.parent.parent)})
    env.update({k: str(v) for k, v in (s.get("astrbot_env") or {}).items()})
    env["DAFEIYU_KB_ROOT"] = str(KIT / "知识库")   # 「大肥鱼自己记知识库」插件把主人补充写到这里
    env["ASTRBOT_LAUNCHER"] = "1"                   # 插件自带的页面才能在控制台里打开，见 LAUNCH_REV 的说明
    extra = [str(py.parent)] + [p for p in (s.get("astrbot_env") or {}).get("_PATH_PREFIX", "").split(os.pathsep) if p]
    env.pop("_PATH_PREFIX", None)
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


_PROFILE_CODE = (
    "import time\n"
    "t=time.time()\n"
    "try:\n"
    "    import runtime_bootstrap\n"
    "except Exception:\n"
    "    pass\n"
    "import astrbot.core, astrbot.core.initial_loader\n"
    "print('DAFEIYU_TOTAL', round(time.time()-t, 1))\n"
)


def _profile_once(py: Path, cwd: Path, env: dict) -> tuple[float, list[tuple[str, float]]]:
    t = time.time()
    code, out = run([str(py), "-X", "importtime", "-c", _PROFILE_CODE], timeout=600, cwd=str(cwd), env=env)
    wall = time.time() - t
    tops: dict[str, float] = {}
    for line in out.splitlines():
        m = re.match(r"import time:\s*\d+\s*\|\s*(\d+)\s*\|( *)(\S+)", line)
        if m and not m.group(2).strip(" ") and len(m.group(2)) <= 1:   # 只看最外层的包
            root = m.group(3).split(".")[0]
            tops[root] = tops.get(root, 0) + int(m.group(1)) / 1e6
    return wall, sorted(tops.items(), key=lambda x: -x[1])


def profile_astrbot() -> str:
    """测 AstrBot「加载程序」那一段到底慢在哪：连测两遍，看是哪个组件慢、第二遍是不是快很多。"""
    s = load_state()
    if not s["astrbot_python"] or not s["astrbot_root"]:
        return "AstrBot 尚未安装完成"
    py = Path(s["astrbot_python"])
    py = py.with_name("python.exe") if py.with_name("python.exe").exists() else py
    env, cwd = astrbot_env(s), Path(s["astrbot_root"])
    job_line("第 1 遍：加载 AstrBot 的程序部分（不影响正在运行的 bot）…")
    job_progress(0.1)
    w1, top1 = _profile_once(py, cwd, env)
    job_line(f"第 1 遍 {w1:.0f} 秒。第 2 遍…")
    job_progress(0.55)
    w2, top2 = _profile_once(py, cwd, env)
    job_progress(1.0)
    worst = "、".join(f"{n} {sec:.1f} 秒" for n, sec in top1[:5])
    lines = [f"加载程序第 1 遍 {w1:.0f} 秒，第 2 遍 {w2:.0f} 秒。最慢的组件：{worst}。"]
    if w1 > 30 and w2 < w1 * 0.5:
        lines.append("第 2 遍快很多：慢在第一次读硬盘（刚开机、硬盘慢，或安全软件在逐个扫描文件）。")
    if w2 > 30:
        lines.append("两遍都慢：多半是 Windows 安全中心每次都在扫描这几千个 Python 文件。可以把 AstrBot 所在文件夹"
                     f"（{Path(s['astrbot_root']).parent}）加进「Windows 安全中心 → 病毒和威胁防护 → 管理设置 → 排除项」，"
                     "此项属于安全设置，请自行决定是否修改。")
    if w1 <= 30 and w2 <= 30:
        lines.append("加载程序本身不慢。上次慢可能是刚开机电脑还在忙，或者插件多。")
    msg = "\n".join(lines)
    log("启动速度测试：" + msg.replace("\n", "") + " 明细：" + ", ".join(f"{n}={sec:.1f}s" for n, sec in top1[:15]))
    update_state(last_profile={"at": time.time(), "msg": msg})
    return msg


def stop_astrbot() -> str:
    info = astrbot_info()
    pids = astrbot_procs()
    if not info["running"] and not pids:
        return "AstrBot 原本未在运行"
    if info["running"]:
        kill_tree(info["pid"])
    for p in pids:
        kill_tree(p)
    for _ in range(30):
        time.sleep(0.5)
        if not astrbot_info()["running"] and not astrbot_procs():
            break
    log(f"停止 AstrBot（{len(pids)} 个进程）")
    return "AstrBot 已停止"


def astrbot_log_file() -> Path:
    d = astrbot_data()
    cands = [LOG_DIR / "astrbot.out.log"]
    if d:
        cands += [d / "logs" / "astrbot.log", d.parent / "logs" / "astrbot.log"]
    existing = [p for p in cands if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else LOG_DIR / "astrbot.out.log"


# ---------------------------------------------------------------- NapCat

def napcat_webui() -> dict:
    try:
        d = read_json(napcat_cfg_dir() / "webui.json")
        return {"port": int(d.get("port", 6099)), "token": d.get("token", ""), "qq": str(d.get("autoLoginAccount", ""))}
    except Exception:
        return {"port": 6099, "token": "", "qq": load_state()["bot_qq"]}


def napcat_connected(rows, ob_port: int) -> bool:
    for local, remote, _s, pid in rows:
        if port_of(remote) == ob_port and remote.startswith("127.0.0.1") and pid != "0" and port_of(local) != ob_port:
            return True
    return False


_proc_cache = {"at": 0.0, "n": 0}


def _procs_ps() -> str:
    prefix = str(napcat_dir()).replace("'", "''")
    return ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
            f"$_.ExecutablePath.StartsWith('{prefix}', [System.StringComparison]::OrdinalIgnoreCase) }}")


def napcat_proc_count(max_age: float = 10.0) -> int:
    if not napcat_dir():
        return 0
    if time.time() - _proc_cache["at"] < max_age:
        return _proc_cache["n"]
    fast = pids_under(napcat_dir())
    if fast is not None:
        n = len(fast)
    else:
        out = ps(_procs_ps() + " | Measure-Object | % Count").strip()
        n = int(out) if out.isdigit() else 0
    _proc_cache.update(at=time.time(), n=n)
    return n


def napcat_running() -> bool:
    if napcat_connected(netstat(), onebot_port()) or port_open(napcat_webui()["port"]):
        return True
    return napcat_proc_count(0) > 0


def start_napcat(wait: bool = True) -> str:
    d = napcat_dir()
    if not d or not (d / "NapCatWinBootMain.exe").exists():
        return "NapCat 尚未安装完成"
    if napcat_running():
        return "NapCat 已在运行"
    qq = napcat_webui()["qq"] or load_state()["bot_qq"]
    logtools.rotate("napcat.log")
    popen_detached([str(d / "NapCatWinBootMain.exe")] + ([qq] if qq else []), d, None, LOG_DIR / "napcat.log",
                   console_app=True)
    _last_start["napcat"] = time.time()
    _proc_cache["at"] = 0
    log(f"启动 NapCat（小号 {qq}）")
    _stage_set("napcat", "正在启动 QQ", 0.02)
    if not wait:
        return "NapCat 启动中"
    t0 = time.time()
    for _ in range(90):
        time.sleep(1)
        rows = netstat()
        if napcat_connected(rows, onebot_port()):
            _stage_set("napcat", "已连上", 1.0)
            return "NapCat 已启动，并已连接 AstrBot"
        up = port_open(napcat_webui()["port"])
        st = napcat_login_state(True, False)
        if st == "need_qr":
            _stage_set("napcat", "要扫码登录（看弹出的二维码）", 0.6)
        elif up:
            # QQ 起来了，等它连上 AstrBot（AstrBot 还在启动的话要等它好了才连得上）
            _stage_set("napcat", "QQ 已启动，等连上 AstrBot", 0.8)
            # 单独启动 NapCat 时不用一直等；全部启动时 AstrBot 还在开，接着等它连上
            if not _all_starting.is_set() and time.time() - t0 > 15:
                return "NapCat 已启动"
        else:
            _stage_set("napcat", f"正在启动 QQ（{int(time.time() - t0)} 秒）", min(0.5, (time.time() - t0) / 40))
    return "NapCat 正在启动，请稍后查看状态（需要扫码时会弹出二维码）"


def stop_napcat() -> str:
    if napcat_dir():
        pids = pids_under(napcat_dir())
        if pids is None:
            ps(_procs_ps() + " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        else:
            for pid in pids:
                kill_tree(pid)
    _proc_cache["at"] = 0
    log("停止 NapCat")
    return "NapCat 已停止"


def napcat_qr_file() -> Path | None:
    a = napcat_app()
    return a / "cache" / "qrcode.png" if a else None


def napcat_login_state(running: bool, connected: bool) -> str:
    if not running:
        return "stopped"
    if connected:
        return "online"
    qr = napcat_qr_file()
    try:
        if qr and qr.exists() and time.time() - qr.stat().st_mtime < 180:
            tail = tail_lines(LOG_DIR / "napcat.log", 30)
            if not any(("接收" in l or "适配器初始化完成" in l or "登录成功" in l) for l in tail[-12:]):
                return "need_qr"
    except Exception:
        pass
    return "connecting"


# ---------------------------------------------------------------- 后台任务

job = {"name": "", "running": False, "msg": "", "ok": True, "at": 0.0, "progress": None, "lines": [], "trouble": None}


def job_line(text: str, quiet: bool = False) -> None:
    """quiet=True 只给界面看，不写进控制台日志（像「已等 23 秒」这种每秒变的）。"""
    job["lines"] = (job["lines"] + [text])[-200:]
    if not quiet:
        log(f"[{job['name']}] {text}")


def job_progress(p: float | None) -> None:
    job["progress"] = p


def run_job(name: str, fn) -> bool:
    if job["running"]:
        return False
    job.update(name=name, running=True, msg="进行中…", ok=True, at=time.time(), progress=None, lines=[], trouble=None)
    _stage.clear()

    def wrap():
        with _busy:
            try:
                job["msg"] = fn() or "完成"
                job["ok"] = True
            except Exception as e:
                job["msg"] = f"出错了：{e}"
                job["ok"] = False
                log_exc()
                try:            # 翻译成问题卡：发生了什么、为什么、怎么办
                    job["trouble"] = troubles.explain(str(e), job["lines"], name)
                except Exception:
                    log_exc()
            finally:
                job["running"] = False
                status_invalidate()
                job["at"] = time.time()

    threading.Thread(target=wrap, daemon=True).start()
    return True


def with_astrbot_stopped(fn):
    def inner():
        was = astrbot_info()["running"]
        if was:
            stop_astrbot()
        try:
            return fn()
        finally:
            if was:
                start_astrbot()
    return inner


def start_all() -> str:
    """NapCat 和 AstrBot 一起启动（以前等 AstrBot 全好了才开 NapCat，白等半分钟到一分钟）。
    NapCat 连不上 AstrBot 时会自己隔几秒重连，所以谁先好都没关系。"""
    set_wanted("napcat", True)
    set_wanted("astrbot", True)
    _stage.clear()
    _all_starting.set()
    res = {}

    def nc():
        try:
            res["n"] = start_napcat()
        except Exception as e:  # noqa: BLE001
            log_exc()
            res["n"] = f"NapCat 启动出错：{e}"
    t = threading.Thread(target=nc, daemon=True)
    t.start()
    try:
        res["a"] = start_astrbot()
        t.join(timeout=120)
    finally:
        _all_starting.clear()
    # AstrBot 好了以后 NapCat 一般几秒内就连上，顺手等一下，结果更准
    for _ in range(20):
        if napcat_connected(netstat(), onebot_port()):
            res["n"] = "NapCat 已连上"
            break
        time.sleep(1)
    return f"{res.get('a', '')}；{res.get('n', 'NapCat 仍在启动')}"


def stop_all() -> str:
    set_wanted("napcat", False)
    set_wanted("astrbot", False)
    b = stop_astrbot()
    a = stop_napcat()
    return f"{b}；{a}"


# ---------------------------------------------------------------- 状态

def _updates_flag() -> list[str]:
    try:
        import updater
        return updater.has_updates()
    except Exception:
        return []


def brief() -> dict:
    """总览页要的几条信息：机器人名、默认人设、白名单里几个群几个私聊、AstrBot / NapCat 版本。
    读的都是小文件（NapCat 版本按修改时间缓存），跟着状态一起算。"""
    out = {"bot_name": bot_name(), "persona": "", "groups": 0, "friends": 0, "versions": {}}
    try:
        c = read_json(cmd_config_path())
        out["persona"] = c.get("agent_runner", {}).get("config", {}).get("persona", {}).get("persona_id", "") or ""
        for x in c.get("platform_settings", {}).get("id_whitelist", []):
            parts = x.split(":")
            if len(parts) == 3 and parts[1] == "GroupMessage":
                out["groups"] += 1
            elif len(parts) == 3 and parts[1] == "FriendMessage":
                out["friends"] += 1
    except Exception:
        pass
    try:
        import updater
        out["versions"] = {"astrbot": updater.ab_current(), "napcat": updater.nc_current()}
    except Exception:
        log_exc()
    return out


def status() -> dict:
    s = load_state()
    rows = netstat()
    ob = onebot_port() if cmd_config_path() and cmd_config_path().exists() else 6199
    ab = astrbot_info(rows, procs=True, allow_slow=False)   # 顺便看看是不是「启动中」（只用快的查法）
    web = napcat_webui()
    connected = napcat_connected(rows, ob)
    webui_up = port_open(web["port"])
    nc_running = connected or webui_up or napcat_proc_count() > 0
    return {
        "setup_done": s["setup_done"],
        "astrbot": ab,
        "napcat": {"running": nc_running, "connected": connected, "qq": web["qq"] or s["bot_qq"],
                   "webui_up": webui_up, "state": napcat_login_state(nc_running, connected),
                   "webui": f"http://127.0.0.1:{web['port']}/webui?token={url_q(web['token'])}" if web["token"]
                   else f"http://127.0.0.1:{web['port']}/webui"},
        "settings": {k: s[k] for k in ("astrbot_port", "autostart", "watchdog", "notify", "wanted")},
        "has_dashboard_login": bool(s.get("dashboard_pass")),
        "last_boot": s.get("last_boot"),
        "astrbot_needs_restart": astrbot_needs_restart(s, ab),
        "updates": _updates_flag(),
        "brief": brief() if s["setup_done"] else {},
        "job": {k: job[k] for k in ("name", "running", "msg", "ok", "at", "progress")},
    }


_st_cache = {"at": 0.0, "data": None}
_st_lock = threading.Lock()


def status_snapshot() -> dict:
    """Serve the last completed sample without waiting on system-process probes."""
    data, at = _st_cache["data"], _st_cache["at"]
    if data is None:
        return {"loading": True, "setup_done": load_state()["setup_done"], "sampled_at": 0}
    return dict(data, sampled_at=at, stale=time.time() - at > 10,
                job={k: job[k] for k in ("name", "running", "msg", "ok", "at", "progress")})


def status_cached(max_age: float = 2.0) -> dict:
    """界面每 3 秒问一次状态。状态要跑 netstat、探端口，慢的时候要一两秒，
    以前每次都现算，界面就一卡一卡的。现在后台线程每 2 秒算一次，界面直接拿最近一次的结果；
    同时只有一个线程在算，别的请求等它算完共用。"""
    d = _st_cache["data"]
    if d is not None and time.time() - _st_cache["at"] < max_age:
        return dict(d, job={k: job[k] for k in ("name", "running", "msg", "ok", "at", "progress")})
    with _st_lock:
        if _st_cache["data"] is None or time.time() - _st_cache["at"] >= max_age:
            _st_cache.update(data=status(), at=time.time())
        d = _st_cache["data"]
    return dict(d, job={k: job[k] for k in ("name", "running", "msg", "ok", "at", "progress")})


def status_invalidate() -> None:
    _st_cache["at"] = 0.0


def status_refresher() -> None:
    """控制台启动就开始算，第一次打开界面不用等。"""
    while True:
        try:
            status_cached(max_age=1.5)
        except Exception:
            log_exc()
        time.sleep(2)


# ---------------------------------------------------------------- 看门狗与提醒

_last = {"online": None, "qr_notified": 0.0}
_KICK = re.compile(r"KickedOffLine|下线通知|其他设备|另一台|别的设备|被踢|强制下线|在别处登录|异地登录")
KICK_REASON = "bot 使用的账号在其他设备登录，已被挤下线"


def napcat_offline_reason(st: dict) -> str:
    """掉线时判断原因：AstrBot 停了、NapCat 退出了、被别处登录挤下线、登录过期要扫码，还是只是连接断了。"""
    if not st["astrbot"]["running"]:
        return "AstrBot 未运行"
    if not st["napcat"]["running"]:
        return "NapCat（QQ）进程已退出"
    try:
        tail = logtools.meaningful_tail(LOG_DIR / "napcat.log", 300, scan_bytes=1_000_000)
    except Exception:
        tail = []
    for ln in reversed(tail[-150:]):
        if _KICK.search(ln):
            return f"{KICK_REASON}（QQ 提示：{ln.strip()[-80:]}）"
    if st["napcat"]["state"] == "need_qr":
        return "QQ 登录状态已失效，需要重新扫码"
    return "QQ 与 AstrBot 之间的连接已断开"


def _offline_event(reason: str | None = None, back: bool = False) -> None:
    evs = list(load_state().get("offline_events") or [])
    if back:
        if evs and not evs[-1].get("back_at"):
            evs[-1]["back_at"] = time.time()
    else:
        evs.append({"at": time.time(), "reason": reason or ""})
    update_state(offline_events=evs[-30:])


def watchdog() -> None:
    while True:
        time.sleep(15)
        try:
            s = load_state()
            if not s["setup_done"]:
                continue
            st = status_cached(max_age=3)
            online = st["napcat"]["state"] == "online" and st["astrbot"]["running"]
            if _last["online"] is True and not online and (s["wanted"]["napcat"] or s["wanted"]["astrbot"]):
                reason = napcat_offline_reason(st)
                log(f"看门狗：掉线了，原因：{reason}")
                _offline_event(reason)
                tip = ("请不要在电脑版 QQ 上登录 bot 使用的账号（手机可以），并打开控制台重新扫码登录。" if "挤下线" in reason
                       else "控制台正在尝试恢复；未能恢复时，请打开控制台查看。")
                notify(f"{bot_name()} 已掉线", f"{reason.split('（')[0]}。{tip}")
            if online and _last["online"] is False:
                log("看门狗：重新上线")
                _offline_event(back=True)
                notify(f"{bot_name()} 已上线", "bot 已恢复在线。")
            _last["online"] = online
            if st["napcat"]["state"] == "need_qr" and time.time() - _last["qr_notified"] > 600:
                _last["qr_notified"] = time.time()
                notify("需要扫码登录", "小号的登录已过期，请打开控制台，用手机 QQ 扫码登录。")
            if not s.get("watchdog") or job["running"] or _busy.locked():
                continue
            now = time.time()
            if s["wanted"].get("astrbot") and now - _last_start["astrbot"] > 150 and not st["astrbot"]["running"]:
                log("看门狗：AstrBot 不在，重新拉起")
                run_job("自动拉起 AstrBot", start_astrbot)
                continue
            if s["wanted"].get("napcat") and now - _last_start["napcat"] > 90 and not st["napcat"]["running"]:
                log("看门狗：NapCat 不在，重新拉起")
                run_job("自动拉起 NapCat", start_napcat)
        except Exception:
            log_exc()
