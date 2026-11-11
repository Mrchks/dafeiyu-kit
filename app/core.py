"""大肥鱼整合包：公共工具（路径、状态、日志、进程、网络、下载、加密保存）。只用标准库。"""

from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.5.0"
CONSOLE_PORT = 6190
# 6190 用不了时依次换这些。Windows 会把一段段端口「保留」给 Hyper-V、WSL、Docker 等用（每次开机可能不同），
# 落在保留段里的端口没有人在用，却绑不上（WinError 10013）。换端口后记进 state.json 的 console_port。
CONSOLE_PORTS = [6190, 6290, 8190, 16190, 26190, 36190, 41190]
ASTRBOT_VERSION = "4.29.3"
# 控制台自动更新：从这个 GitHub 仓库的 Release 读取 latest.json；签名公钥由 scripts/release.py 首次正式发版时写入
UPDATE_REPO = "Mrchks/dafeiyu-kit"
UPDATE_PUBKEY = ""
# 问题反馈的中转地址（阿里云函数计算，代码在 scripts/feedback-relay）；要换地址时随自动更新下发
FEEDBACK_URL = "https://dafeiyufeedback-xhqjjdjhgd.cn-hangzhou.fcapp.run/"

APP = Path(__file__).resolve().parent          # <整合包>\app
KIT = APP.parent                                 # <整合包>
UI_DIR = APP / "ui"
PACK_DIR = APP / "configpack"
RUNTIME_PY = KIT / "runtime" / "python"
STATE_FILE = KIT / "state.json"
LOG_DIR = KIT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

GH_PROXIES = ["", "https://gh-proxy.com/", "https://ghfast.top/", "https://hk.gh-proxy.com/", "https://gh.dpik.top/"]
PYPI_MIRRORS = ["https://mirrors.aliyun.com/pypi/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple",
                "https://pypi.org/simple"]

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with _log_lock:
        try:
            p = LOG_DIR / "console.log"
            if p.exists() and p.stat().st_size > 2_000_000:
                p.replace(LOG_DIR / "console.old.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def log_exc() -> None:
    log(traceback.format_exc())


# ---------------------------------------------------------------- 状态文件

DEFAULT_STATE = {
    "version": VERSION,
    "setup_done": False,
    "setup_step": "welcome",
    "home": "",                 # 组件安装目录（英文路径）
    "mode": "pip",              # pip = 向导装的；launcher = AstrBot 启动器装的（老用户）
    "astrbot_root": "",         # AstrBot 根目录（里面有 data）
    "astrbot_python": "",       # 运行 AstrBot 的 pythonw.exe
    "astrbot_cmd": [],          # 启动参数（在 python 后面）
    "astrbot_env": {},          # 额外环境变量
    "astrbot_port": 6185,
    "napcat_dir": "",           # NapCat.*.Shell
    "bot_qq": "",
    "owners": [],
    "dashboard_user": "",
    "dashboard_pass": "",       # DPAPI 加密后 base64
    "onebot_http_port": 3010,
    "onebot_http_token": "",
    "autostart": True,
    "watchdog": True,
    "notify": True,
    "wanted": {"napcat": False, "astrbot": False},
    "wizard": {},               # 向导里填过的东西（不含密钥）
}

_state_lock = threading.RLock()


def load_state() -> dict:
    with _state_lock:
        s = json.loads(json.dumps(DEFAULT_STATE))
        try:
            s.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:
            log_exc()
        for k, v in DEFAULT_STATE.items():
            s.setdefault(k, v)
        return s


def save_state(s: dict) -> None:
    with _state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def update_state(**kw) -> dict:
    with _state_lock:
        s = load_state()
        s.update(kw)
        save_state(s)
        return s


def set_wanted(name: str, value: bool) -> None:
    with _state_lock:
        s = load_state()
        s["wanted"][name] = value
        save_state(s)


# ---------------------------------------------------------------- 加密保存（Windows DPAPI，只有本机本用户能解开）

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def protect(text: str) -> str:
    if not text:
        return ""
    if not WIN:
        return "plain:" + base64.b64encode(text.encode()).decode()
    data = text.encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("加密失败")
    try:
        return "dpapi:" + base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(blob: str) -> str:
    if not blob:
        return ""
    kind, _, b64 = blob.partition(":")
    raw = base64.b64decode(b64)
    if kind == "plain":
        return raw.decode()
    buf = ctypes.create_string_buffer(raw, len(raw))
    inp = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("解密失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- 路径（都从状态文件来）

def astrbot_root() -> Path | None:
    r = load_state()["astrbot_root"]
    return Path(r) if r else None


def astrbot_data() -> Path | None:
    r = astrbot_root()
    return r / "data" if r else None


def cmd_config_path() -> Path | None:
    d = astrbot_data()
    return d / "cmd_config.json" if d else None


def plugin_cfg_dir() -> Path | None:
    d = astrbot_data()
    return d / "config" if d else None


def napcat_dir() -> Path | None:
    r = load_state()["napcat_dir"]
    return Path(r) if r else None


def napcat_app() -> Path | None:
    d = napcat_dir()
    if not d:
        return None
    hits = sorted(d.glob("versions/*/resources/app/napcat"))
    return hits[-1] if hits else None


def napcat_cfg_dir() -> Path | None:
    a = napcat_app()
    return a / "config" if a else None


def backups_dir() -> Path:
    s = load_state()
    base = Path(s["home"]) if s["home"] else KIT
    p = base / "Backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- JSON

def read_json(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def write_json(p: Path, data, backup: bool = True) -> None:
    p = Path(p)
    if backup and p.exists():
        bak = p.with_name(p.name + f".kit-{dt.date.today():%Y%m%d}.bak")
        if not bak.exists():
            shutil.copy2(p, bak)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------- 进程与端口

def decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs" if WIN else "utf-8", "gbk", "cp932"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def run(cmd: list[str], timeout: int = 30, cwd=None, env=None) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, env=env,
                             creationflags=CREATE_NO_WINDOW if WIN else 0)
        return out.returncode, decode(out.stdout + out.stderr)
    except subprocess.TimeoutExpired:
        return -1, "超时"
    except Exception as e:
        return -1, f"ERR {e}"


def run_stream(cmd: list[str], on_line, cwd=None, env=None, timeout: int = 3600) -> int:
    """运行命令，把输出一行行交给 on_line。"""
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW if WIN else 0)
    start = time.time()
    buf = b""
    while True:
        chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"\r\n|\n|\r", buf)
        for ln in lines:
            t = decode(ln).strip()
            if t:
                on_line(t)
        if time.time() - start > timeout:
            p.kill()
            on_line("超时，已中止")
            return -1
    if buf.strip():
        on_line(decode(buf).strip())
    return p.wait()


def console_port() -> int:
    """控制台实际用的端口：上次成功启动时记下的，没有就是 6190。"""
    try:
        p = int(load_state().get("console_port") or CONSOLE_PORT)
        return p if 1024 < p < 65536 else CONSOLE_PORT
    except Exception:
        return CONSOLE_PORT


def console_alive(port: int) -> bool:
    """这个端口上是不是正在运行的大肥鱼控制台（不是别的程序占着）。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 本机地址不走系统代理
        with op.open(f"http://127.0.0.1:{int(port)}/api/ping", timeout=2) as r:
            return "version" in json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def netstat() -> list[tuple[str, str, str, str]]:
    rows = []
    _code, out = run(["netstat", "-ano", "-p", "TCP"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP":
            rows.append((parts[1], parts[2], parts[3], parts[4]))
    return rows


def port_of(addr: str) -> int:
    try:
        return int(addr.rsplit(":", 1)[1])
    except Exception:
        return -1


def listener_pid(port: int, rows=None) -> int | None:
    for local, remote, _s, pid in rows if rows is not None else netstat():
        if port_of(local) == port and port_of(remote) == 0 and pid != "0":
            return int(pid)
    return None


def listening_ports_of(pid: int, rows) -> list[int]:
    return sorted({port_of(l) for l, r, _s, p in rows if p == str(pid) and port_of(r) == 0})


def free_port(preferred: int) -> int:
    for p in range(preferred, preferred + 50):
        if not port_open(p):
            return p
    return preferred


def popen_detached(cmd: list[str], cwd, env, out_path: Path, console_app: bool):
    out = open(out_path, "ab")
    base = (CREATE_NO_WINDOW if console_app else DETACHED_PROCESS) | CREATE_NEW_PROCESS_GROUP
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags if WIN else 0, close_fds=True)
        except OSError:
            continue
    raise RuntimeError("进程启动失败")


def kill_tree(pid: int) -> None:
    run(["taskkill", "/PID", str(pid), "/T", "/F"])


def process_paths() -> list[tuple[int, str]] | None:
    """列出本机进程的 (pid, 程序完整路径)。直接调 Windows API，几毫秒；
    以前用 PowerShell 的 Get-CimInstance，每次 1–3 秒，状态刷新和停 AstrBot 都被它拖慢。
    拿不到（非 Windows、API 出错）返回 None，调用方退回 PowerShell 老办法。"""
    if not WIN:
        return None
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        n = 4096
        while True:
            arr = (wintypes.DWORD * n)()
            got = wintypes.DWORD()
            if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(got)):
                return None
            cnt = got.value // ctypes.sizeof(wintypes.DWORD)
            if cnt < n:
                break
            n *= 2
        out = []
        buf = ctypes.create_unicode_buffer(32768)
        for pid in arr[:cnt]:
            if not pid:
                continue
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                continue
            try:
                size = wintypes.DWORD(len(buf))
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    out.append((int(pid), buf.value))
            finally:
                k32.CloseHandle(h)
        return out
    except Exception:  # noqa: BLE001
        return None


def pids_under(prefix: str | Path, names: tuple[str, ...] = ()) -> list[int] | None:
    """程序路径在 prefix 目录下的进程（names 给了就只要这些文件名）。拿不到进程表时返回 None。"""
    rows = process_paths()
    if rows is None:
        return None
    pre = str(prefix).rstrip("\\/").lower() + "\\"
    want = {x.lower() for x in names}
    return [pid for pid, path in rows
            if path.lower().startswith(pre) and (not want or Path(path).name.lower() in want)]


def ps(script: str, timeout: int = 30) -> str:
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=timeout)[1]


def tail_lines(p: Path, n: int) -> list[str]:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            data = f.read()
        return decode(data).splitlines()[-n:]
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------- 网络与下载

def http_json(url: str, data=None, headers=None, timeout: int = 15, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"DafeiyuKit/{VERSION}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    return json.loads(txt) if txt else {}


def download(url: str, dest: Path, progress=None, github: bool = False, min_size: int = 1000) -> None:
    """下载文件；GitHub 地址失败时自动换加速站。progress(已下载, 总大小)。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tries = [p + url for p in GH_PROXIES] if github else [url]
    last = None
    for u in tries:
        tmp = dest.with_name(dest.name + ".part")
        try:
            req = urllib.request.Request(u, headers={"User-Agent": f"DafeiyuKit/{VERSION}"})
            with urllib.request.urlopen(req, timeout=20) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                t0 = time.time()
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
                    # 太慢（20 秒还不到 200KB）就换下一个
                    if time.time() - t0 > 20 and got < 200_000:
                        raise TimeoutError("太慢")
            if tmp.stat().st_size < min_size:
                raise IOError("文件过小，下载到的可能是错误页面")
            # 加速站有时传到一半就断开，但连接是「正常结束」的，不核对就会把半截文件当成下好了
            if total and got != total:
                raise IOError(f"文件不完整：应有 {total} 字节，只收到 {got} 字节")
            if dest.suffix.lower() == ".zip" and not zip_ok(tmp):
                raise IOError("文件不完整：压缩包损坏")
            tmp.replace(dest)
            log(f"下载完成 {u}")
            return
        except Exception as e:
            last = e
            log(f"下载失败 {u}: {e}")
            try:
                tmp.unlink()
            except Exception:
                pass
    raise RuntimeError(f"下载失败（已尝试 {len(tries)} 个下载地址）：{last}")


def zip_ok(path: Path) -> bool:
    """压缩包是否完整（能读目录，且每个文件的校验都对得上）。"""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except Exception:
        return False


def github_latest_asset(repo: str, name_pred) -> tuple[str, str]:
    """(版本, 下载地址)。API 不通时用 releases/latest 的跳转拿版本号。"""
    try:
        d = http_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10)
        for a in d.get("assets", []):
            if name_pred(a["name"]):
                return d.get("tag_name", ""), a["browser_download_url"]
    except Exception as e:
        log(f"GitHub API 不通：{e}")
    for p in GH_PROXIES:
        try:
            req = urllib.request.Request(p + f"https://github.com/{repo}/releases/latest", method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                tag = r.geturl().rstrip("/").rsplit("/", 1)[-1]
                if tag and tag != "latest":
                    return tag, ""
        except Exception:
            continue
    return "", ""


def is_ascii_path(p: str) -> bool:
    return all(ord(c) < 128 for c in p) and " " not in p


def disk_free_gb(p: str) -> float:
    try:
        return shutil.disk_usage(p).free / 1024 ** 3
    except Exception:
        return -1


def mem_gb() -> float:
    if not WIN:
        return -1

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullTotalPhys / 1024 ** 3


def edge_path() -> str:
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if base and p.exists():
            return str(p)
    return ""


# ---------------------------------------------------------------- 窗口（单窗口）

def find_console_window() -> int:
    if not WIN:
        return 0
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith("大肥鱼控制台"):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def open_console_window() -> str:
    hwnd = find_console_window()
    if hwnd:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return "focused"
    url = f"http://127.0.0.1:{console_port()}/"
    edge = edge_path()
    if edge:
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={KIT / 'edge-profile'}",
                          "--window-size=1280,860", "--no-first-run", "--no-default-browser-check"],
                         creationflags=DETACHED_PROCESS if WIN else 0, close_fds=True)
    elif WIN:
        os.startfile(url)  # noqa
    return "opened"


def url_q(s: str) -> str:
    return urllib.parse.quote(s, safe="")
