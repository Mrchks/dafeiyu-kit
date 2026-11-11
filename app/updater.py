"""一键更新 AstrBot 和 NapCat，新手不用进它们各自的后台。

为什么不用 AstrBot 后台自带的「更新」：控制台启动 AstrBot 时带着启动器的标记（ASTRBOT_LAUNCHER），
有了它 AstrBot 才肯让插件页面嵌在控制台里显示；可带着这个标记，AstrBot 会拒绝自己更新
（报「You are running AstrBot via CLI」）。所以由控制台来更新：

AstrBot
- 源码安装（AstrBot 启动器装的那种，代码在 core 文件夹里）：停掉 → 把程序代码打包备份（不含 data，
  人设、记忆、知识库都在 data 里，完全不碰）→ 单独起一个进程调 AstrBot 自己的更新程序下载新版
  代码和后台网页 → 用国内镜像装新依赖 → 启动并检查 → 起不来就自动换回备份。
- pip 安装（安装向导装的那种）：停掉 → 用国内镜像 pip 装新版 → 启动并检查 → 起不来就装回旧版。
  这种只记旧版本号，不占空间。

NapCat
- 普通更新：只换 napcat 文件夹里的程序文件，配置文件夹（QQ 登录、和 AstrBot 的连接）原样保留；
  外层一键包自带的启动器不动（它和 NapCat.Shell.zip 里的启动器不是一个东西）。
- 连 QQ 内核一起更新：新版 NapCat 要求更新的 QQ 时用。下一整套新的（约 300MB），把旧配置搬过去，
  旧的整套留作备份。

备份每样只留一份（最近一次更新前的），新的备份做好了才删旧的。「版本与更新」页可以退回或删除备份。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import selfupdate
import services
from core import (LOG_DIR, backups_dir, download, github_latest_asset, http_json, load_state, log, log_exc, napcat_app,
                  napcat_dir, run_stream, update_state)
from services import job_line, job_progress

RELEASE_API = "https://api.soulter.top/releases"      # AstrBot 官方的版本列表（国内能直接访问）
NAPCAT_REPO = "NapNeko/NapCatQQ"
CHECK_EVERY = 6 * 3600


# ---------------------------------------------------------------- 小工具

def _v(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def _newer(latest: str, current: str) -> bool:
    return bool(latest) and bool(current) and _v(latest) > _v(current)


def _bk_dir() -> Path:
    d = backups_dir() / "更新前"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _size(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    n = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                n += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return n


def _records() -> dict:
    return dict(load_state().get("updates") or {})


def _save_record(comp: str, rec: dict | None) -> None:
    r = _records()
    if rec is None:
        r.pop(comp, None)
    else:
        r[comp] = rec
    update_state(updates=r)


def _drop_backup(rec: dict | None) -> None:
    """删掉一份备份（zip 文件或整个文件夹）。"""
    if not rec or not rec.get("backup"):
        return
    p = Path(rec["backup"])
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
        log(f"删掉旧备份 {p}")
    except Exception:
        log_exc()


# ---------------------------------------------------------------- AstrBot：版本

def ab_source_root() -> Path | None:
    """源码安装（启动器装的）返回代码所在文件夹；pip 安装返回 None。"""
    r = load_state().get("astrbot_root")
    root = Path(r) if r else None
    if root and (root / "astrbot" / "core").is_dir() and (root / "main.py").is_file():
        return root
    return None


def _venv_python() -> Path | None:
    p = load_state().get("astrbot_python")
    if not p:
        return None
    p = Path(p)
    return p.with_name("python.exe") if p.with_name("python.exe").exists() else p


def ab_current() -> str:
    root = ab_source_root()
    try:
        if root and (root / "pyproject.toml").exists():
            m = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1)
        py = _venv_python()
        if py:
            for d in sorted(py.parent.parent.glob("Lib/site-packages/astrbot-*.dist-info")):
                m = re.match(r"astrbot-([\d.]+)", d.name)
                if m:
                    return m.group(1).rstrip(".")
    except Exception:
        log_exc()
    return ""


def ab_latest() -> dict:
    data = http_json(RELEASE_API, timeout=15)
    for r in data if isinstance(data, list) else []:
        tag = str(r.get("tag_name") or "")
        if not tag or r.get("prerelease") or r.get("draft") or re.search(r"(alpha|beta|rc|dev)", tag, re.I):
            continue
        return {"version": tag.lstrip("v"), "tag": tag, "notes": str(r.get("body") or "")[:3000],
                "date": str(r.get("published_at") or "")[:10]}
    return {}


# ---------------------------------------------------------------- NapCat：版本

_nc_cache: dict = {}


def nc_current() -> str:
    """从 napcat.mjs 里读版本号。文件 3MB，按修改时间缓存，状态刷新时不用每次都读。"""
    app = napcat_app()
    try:
        f = app / "napcat.mjs" if app else None
        if f and f.exists():
            key = (str(f), f.stat().st_mtime)
            if _nc_cache.get("key") == key:
                return _nc_cache["v"]
            txt = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'"(\d+\.\d+\.\d+)"\s*\|\|\s*"1\.0\.0-dev"', txt)
            if m:
                _nc_cache.update(key=key, v=m.group(1))
                return m.group(1)
    except Exception:
        log_exc()
    return str((_records().get("napcat") or {}).get("to") or "")


def nc_latest() -> dict:
    tag, url = github_latest_asset(NAPCAT_REPO, lambda n: n == "NapCat.Shell.zip")
    if not tag:
        return {}
    url = url or f"https://github.com/{NAPCAT_REPO}/releases/download/{tag}/NapCat.Shell.zip"
    notes = ""
    try:
        d = http_json(f"https://api.github.com/repos/{NAPCAT_REPO}/releases/latest", timeout=8)
        notes = str(d.get("body") or "")[:3000]
    except Exception:
        pass
    return {"version": tag.lstrip("v"), "tag": tag, "url": url, "notes": notes}


# ---------------------------------------------------------------- 检查更新（结果缓存在状态文件里）

_check_lock = threading.Lock()


def check(force: bool = False) -> dict:
    s = load_state()
    c = s.get("update_check") or {}
    if not force and c.get("at") and time.time() - c["at"] < CHECK_EVERY:
        return c
    with _check_lock:
        out = {"at": time.time(), "errors": {}}
        for comp, fn in (("astrbot", ab_latest), ("napcat", nc_latest), ("console", selfupdate.latest)):
            try:
                out[comp] = fn()
            except Exception as e:  # noqa: BLE001
                out[comp] = (c.get(comp) or {})
                out["errors"][comp] = str(e)[:200]
                log(f"检查 {comp} 新版本失败：{e}")
        update_state(update_check=out)
        return out


def info(force: bool = False) -> dict:
    c = check(force)
    recs = _records()
    res = {"checked_at": c.get("at"), "errors": c.get("errors", {})}
    for comp, cur in (("astrbot", ab_current()), ("napcat", nc_current())):
        lat = c.get(comp) or {}
        rec = recs.get(comp) or {}
        bk = Path(rec["backup"]) if rec.get("backup") else None
        res[comp] = {
            "current": cur, "latest": lat.get("version", ""), "date": lat.get("date", ""), "notes": lat.get("notes", ""),
            "newer": _newer(lat.get("version", ""), cur),
            "backup": ({"from": rec.get("from"), "to": rec.get("to"), "at": rec.get("at"), "kind": rec.get("kind"),
                        "size": _size(bk) if bk else 0, "exists": bool(bk and bk.exists()) or rec.get("kind") == "pip"}
                       if rec else None),
        }
    res["astrbot"]["mode"] = "source" if ab_source_root() else "pip"
    rec = recs.get("console") or {}
    bk = Path(rec["backup"]) if rec.get("backup") else None
    res["console"] = {**selfupdate.describe(c.get("console") or {}),
                      "backup": ({"from": rec.get("from"), "to": rec.get("to"), "at": rec.get("at"), "kind": "zip",
                                  "size": _size(bk), "exists": bool(bk and bk.exists())} if bk and bk.exists() else None)}
    return res


def has_updates() -> list[str]:
    """不联网，只看上次检查的结果（给总览的小红点用）。"""
    c = load_state().get("update_check") or {}
    out = []
    if _newer((c.get("astrbot") or {}).get("version", ""), ab_current()):
        out.append("astrbot")
    if _newer((c.get("napcat") or {}).get("version", ""), nc_current()):
        out.append("napcat")
    con = selfupdate.describe(c.get("console") or {})
    if con["newer"] and con["enabled"] and not con["dev"]:
        out.append("console")
    return out


def auto_check_loop() -> None:
    """控制台开着就每 6 小时看一眼有没有新版（只看，不自动更新）。"""
    time.sleep(60)
    while True:
        try:
            check()
        except Exception:
            log_exc()
        time.sleep(CHECK_EVERY)


# ---------------------------------------------------------------- AstrBot：更新

def _zip_tree(src: Path, dest: Path, skip_top: set[str]) -> None:
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for root, dirs, files in os.walk(src):
            rel_root = Path(root).relative_to(src)
            if rel_root == Path("."):
                dirs[:] = [d for d in dirs if d not in skip_top]
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for f in files:
                if rel_root == Path(".") and f in skip_top:
                    continue
                if f.endswith(".pyc"):
                    continue
                p = Path(root) / f
                z.write(p, (rel_root / f).as_posix())
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
    if bad:
        tmp.unlink()
        raise RuntimeError(f"备份文件已损坏（{bad}），更新已中止")
    tmp.replace(dest)


_UPDATE_SCRIPT = r'''
import asyncio, json, sys
from astrbot.core.updater import AstrBotUpdater

async def main():
    async def cb(ev):
        print("DAFEIYU_PROGRESS " + json.dumps({"percent": getattr(ev, "overall_percent", None),
              "message": getattr(ev, "message", "")}, ensure_ascii=False), flush=True)
    await AstrBotUpdater().update(version=sys.argv[1], progress_callback=cb)
    print("DAFEIYU_DONE", flush=True)

asyncio.run(main())
'''


def _run_astrbot_updater(root: Path, tag: str) -> None:
    """调 AstrBot 自己的更新程序（单独一个进程，不带启动器标记），下载新代码和后台网页并换上。"""
    py = _venv_python()
    env = services.astrbot_env()
    env.pop("ASTRBOT_LAUNCHER", None)
    env.pop("ASTRBOT_CLI", None)
    done = {"ok": False, "last": ""}

    def on(line: str) -> None:
        if line.startswith("DAFEIYU_PROGRESS "):
            try:
                d = json.loads(line.split(" ", 1)[1])
                if d.get("percent") is not None:
                    job_progress(0.15 + 0.45 * float(d["percent"]) / 100)
                if d.get("message") and d["message"] != done.get("msg"):   # 同一句只记一次，免得日志刷屏
                    done["msg"] = d["message"]
                    job_line("AstrBot 更新程序：" + str(d["message"]))
            except ValueError:
                pass
        elif line.startswith("DAFEIYU_DONE"):
            done["ok"] = True
        else:
            done["last"] = line[-300:]
    code = run_stream([str(py), "-c", _UPDATE_SCRIPT, tag], on, cwd=str(root), env=env, timeout=1800)
    if code != 0 or not done["ok"]:
        raise RuntimeError(f"下载或替换新代码失败：{done['last'] or ('退出码 ' + str(code))}")


def _pip(args: list[str]) -> None:
    import installer
    installer._pip_install(_venv_python(), args)


def _restore_tree(root: Path, zip_path: Path) -> None:
    """把源码文件夹换回备份：除了 data 以外的东西先删掉，再解压备份。"""
    if not zip_path.exists():
        raise RuntimeError("找不到备份文件，无法回退")
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip():
            raise RuntimeError("备份文件已损坏，无法回退")
        for item in root.iterdir():
            if item.name in ("data", "venv", ".venv"):
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
        z.extractall(root)


def _start_and_check() -> None:
    job_line("正在启动 AstrBot 并检查能否正常运行（需要一至两分钟）…")
    res = services.start_astrbot()
    if "已启动" not in res:
        raise RuntimeError(res)


def update_astrbot() -> str:
    c = check(force=True)
    lat = c.get("astrbot") or {}
    cur = ab_current()
    if not lat.get("version"):
        raise RuntimeError("无法查询 AstrBot 最新版本（可能是网络不通），请稍后重试。")
    if not _newer(lat["version"], cur):
        return f"已是最新版本 {cur}"
    root = ab_source_root()
    # 更新完要不要让它接着跑：本来在跑，或者用户希望她在线（总览点过启动）
    was_running = services.astrbot_info()["running"] or bool(load_state()["wanted"].get("astrbot"))
    old_rec = _records().get("astrbot")
    job_line(f"准备从 {cur} 更新到 {lat['version']}，正在停止 AstrBot…")
    job_progress(0.03)
    services.stop_astrbot()
    new_rec = {"from": cur, "to": lat["version"], "at": time.time()}
    try:
        if root:
            job_line("正在备份当前程序代码（不含 data，人设和记忆不受影响）…")
            bk = _bk_dir() / f"astrbot-{cur}.zip"
            if old_rec and old_rec.get("backup") == str(bk):
                old_rec = None          # 同名，下面覆盖掉就行
            _zip_tree(root, bk, {"data", "venv", ".venv"})
            new_rec.update(kind="zip", backup=str(bk))
            _drop_backup(old_rec)       # 新备份好了才删旧的，只留一份
            job_progress(0.12)
            _run_astrbot_updater(root, lat["tag"])
            job_progress(0.62)
            job_line("正在从国内镜像安装新版本所需的组件…")
            _pip(["-r", str(root / "requirements.txt")])
        else:
            new_rec.update(kind="pip", backup=None)
            _drop_backup(old_rec)
            job_line("正在从国内镜像下载新版 AstrBot…")
            _pip([f"astrbot=={lat['version']}"])
        job_progress(0.8)
        _start_and_check()
    except Exception as e:  # noqa: BLE001
        log_exc()
        job_line(f"更新失败：{e}。正在回退到 {cur}…")
        try:
            services.stop_astrbot()
            if root and new_rec.get("backup"):
                _restore_tree(root, Path(new_rec["backup"]))
            elif not root:
                _pip([f"astrbot=={cur}"])
            if was_running:
                services.start_astrbot()
        except Exception as e2:  # noqa: BLE001
            log_exc()
            raise RuntimeError(f"更新失败（{e}），回退旧版本时也出现错误（{e2}）。备份位于 {new_rec.get('backup') or '无'}，"
                               "请将此信息连同问题报告反馈给作者。")
        # 退回成功：备份内容就是现在在用的，留着也没意义
        if new_rec.get("backup"):
            _drop_backup(new_rec)
        _save_record("astrbot", None)
        raise RuntimeError(f"更新到 {lat['version']} 失败（{e}），已自动回退到 {cur}，bot 可正常使用。")
    _save_record("astrbot", new_rec)
    if not was_running:
        services.stop_astrbot()
    job_progress(1.0)
    return f"AstrBot 已更新到 {lat['version']}。旧版本已保留一份备份，可在「版本与更新」页回退或删除。"


def rollback_astrbot() -> str:
    rec = _records().get("astrbot")
    if not rec:
        raise RuntimeError("没有可用于回退的备份")
    cur = ab_current()
    root = ab_source_root()
    # 更新完要不要让它接着跑：本来在跑，或者用户希望她在线（总览点过启动）
    was_running = services.astrbot_info()["running"] or bool(load_state()["wanted"].get("astrbot"))
    job_line(f"回退到 {rec['from']}，正在停止 AstrBot…")
    services.stop_astrbot()
    if rec.get("kind") == "zip" and root:
        job_line("正在将程序代码恢复为备份版本…")
        _restore_tree(root, Path(rec["backup"]))
        job_progress(0.4)
        job_line("正在检查旧版本所需的组件…")
        try:
            _pip(["-r", str(root / "requirements.txt")])
        except Exception:
            log_exc()
    else:
        job_line(f"正在从国内镜像重新安装 {rec['from']}…")
        _pip([f"astrbot=={rec['from']}"])
    job_progress(0.7)
    _start_and_check()
    if not was_running:
        services.stop_astrbot()
    _drop_backup(rec)
    _save_record("astrbot", None)
    job_progress(1.0)
    return f"已回退到 {rec['from']}（此前为 {cur}）。所用备份已删除。"


# ---------------------------------------------------------------- NapCat：更新

def _napcat_ok(timeout: int = 120) -> str:
    """启动 NapCat 并等它好：AstrBot 在跑就等连上；没在跑就等它的网页后台开起来。"""
    services.start_napcat(wait=False)
    ab = services.astrbot_info()["running"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        rows = services.netstat()
        if services.napcat_connected(rows, services.onebot_port()):
            return "已连接 AstrBot"
        st = services.napcat_login_state(True, False)
        if st == "need_qr":
            return "已启动，需要扫码登录"
        if not ab and services.port_open(services.napcat_webui()["port"]):
            return "已启动"
        job_line(f"等待 NapCat 启动（{int(time.time() - t0)} 秒）…", quiet=True)
    raise RuntimeError("新版 NapCat 无法启动，或无法连接 AstrBot")


def _napcat_log_since(f: Path, pos: int) -> str:
    """更新失败时看看新版 NapCat 说了什么，一行都没有说明 QQ 根本没把它带起来。"""
    try:
        if pos > f.stat().st_size:      # 启动前日志被挪去归档了，从头读
            pos = 0
        with open(f, "rb") as fh:
            fh.seek(pos)
            txt = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).strip() for ln in txt.splitlines()]
    lines = [ln for ln in lines if ln and "重新连接" not in ln and "TCPConnectWrap" not in ln]
    if not lines:
        return "（没有任何输出：QQ 未能启动 NapCat）"
    return " / ".join(lines[-4:])[:600]


def _backup_napcat(app: Path, shell: Path, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for root, dirs, files in os.walk(app):
            rel = Path(root).relative_to(app)
            if rel == Path("."):
                dirs[:] = [d for d in dirs if d not in ("cache", "logs")]
            for f in files:
                z.write(Path(root) / f, ("app" / rel / f).as_posix())
        for f in ("NapCatWinBootMain.exe", "NapCatWinBootHook.dll"):
            if (shell / f).exists():
                z.write(shell / f, f"shell/{f}")
    with zipfile.ZipFile(tmp) as z:
        if z.testzip():
            tmp.unlink()
            raise RuntimeError("备份文件已损坏，更新已中止")
    tmp.replace(dest)


def _copy_over(src: Path, dst: Path, skip_top: set[str]) -> None:
    for item in src.iterdir():
        if item.name in skip_top:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _restore_napcat(app: Path, shell: Path, zip_path: Path) -> None:
    with tempfile.TemporaryDirectory(dir=str(_bk_dir())) as td:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(td)
        for item in app.iterdir():                 # 配置、缓存、日志之外的先清掉，免得新版多出来的文件留着
            if item.name in ("config", "cache", "logs"):
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
        _copy_over(Path(td) / "app", app, set())
        sh = Path(td) / "shell"
        if sh.is_dir():
            _copy_over(sh, shell, set())


def update_napcat() -> str:
    c = check(force=True)
    lat = c.get("napcat") or {}
    cur = nc_current()
    if not lat.get("version"):
        raise RuntimeError("无法查询 NapCat 最新版本（可能无法连接 GitHub），请稍后重试。")
    if not _newer(lat["version"], cur):
        return f"已是最新版本 {cur}"
    app, shell = napcat_app(), napcat_dir()
    if not app or not shell:
        raise RuntimeError("找不到 NapCat 的安装位置")
    old_rec = _records().get("napcat")
    with tempfile.TemporaryDirectory(dir=str(_bk_dir())) as td:
        zp = Path(td) / "NapCat.Shell.zip"
        job_line(f"正在下载 NapCat {lat['version']}…")
        download(lat["url"], zp, progress=lambda g, t: job_progress(0.05 + 0.3 * g / t if t else None), github=True)
        new_dir = Path(td) / "new"
        with zipfile.ZipFile(zp) as z:
            z.extractall(new_dir)
        if not (new_dir / "napcat.mjs").exists():          # 有的包外面还套了一层文件夹
            hits = list(new_dir.glob("*/napcat.mjs"))
            if not hits:
                raise RuntimeError("下载的 NapCat 安装包不正确（缺少 napcat.mjs），当前的 NapCat 未做改动")
            new_dir = hits[0].parent
        job_line("正在停止 NapCat 并备份当前版本（包含登录信息和配置）…")
        job_progress(0.4)
        services.stop_napcat()
        time.sleep(2)
        bk = _bk_dir() / f"napcat-{cur or 'old'}.zip"
        if old_rec and old_rec.get("backup") == str(bk):
            old_rec = None
        _backup_napcat(app, shell, bk)
        _drop_backup(old_rec)
        rec = {"from": cur, "to": lat["version"], "at": time.time(), "kind": "zip", "backup": str(bk)}
        pos = None
        try:
            job_line("正在替换为新版程序文件（配置文件夹保持不变）…")
            job_progress(0.55)
            _copy_over(new_dir, app, {"config"})
            # 注意：外层 Shell 文件夹里的 NapCatWinBootMain.exe / Hook.dll 是一键包自己的启动器，
            # 和 NapCat.Shell.zip 里的不是同一个东西（大小都不一样），不能拿新包里的去覆盖，
            # 否则 QQ 根本带不起 NapCat（11-02 两次更新失败就是这个原因）。只换 napcat 文件夹里的。
            job_progress(0.7)
            nlog = LOG_DIR / "napcat.log"
            pos = nlog.stat().st_size if nlog.exists() else 0
            how = _napcat_ok()
        except Exception as e:  # noqa: BLE001
            log_exc()
            why = _napcat_log_since(LOG_DIR / "napcat.log", pos) if pos is not None else ""
            if why:
                log(f"新版 NapCat 的输出：{why}")
            job_line(f"新版本未能运行（{e}），正在恢复 {cur}…")
            try:
                services.stop_napcat()
                time.sleep(2)
                _restore_napcat(app, shell, bk)
                services.start_napcat(wait=False)
            except Exception as e2:  # noqa: BLE001
                log_exc()
                raise RuntimeError(f"更新失败（{e}），恢复旧版本时也出现错误（{e2}）。备份位于 {bk}")
            _drop_backup(rec)
            _save_record("napcat", None)
            raise RuntimeError(f"NapCat {lat['version']} 未能运行，已恢复为 {cur}。新版本可能要求更新版本的 QQ 内核，"
                               "可尝试「完整更新（含 QQ 内核）」。")
    _save_record("napcat", rec)
    job_progress(1.0)
    return f"NapCat 已更新到 {lat['version']}（{how}）。旧版本已保留一份备份，可以回退或删除。"


def update_napcat_full() -> str:
    """连 QQ 内核一起更新：装一整套新的，把配置搬过去，旧的整套留作备份。"""
    import installer
    s = load_state()
    old_shell, old_app = napcat_dir(), napcat_app()
    if not old_shell or not old_app:
        raise RuntimeError("找不到 NapCat 的安装位置")
    cur = nc_current()
    home = Path(s["home"]) if s.get("home") else old_shell.parent.parent
    tag, url = github_latest_asset(NAPCAT_REPO, lambda n: n == "NapCat.Shell.Windows.OneKey.zip")
    if not tag:
        raise RuntimeError("无法查询 NapCat 最新版本（可能无法连接 GitHub），请稍后重试。")
    url = url or f"https://github.com/{NAPCAT_REPO}/releases/download/{tag}/NapCat.Shell.Windows.OneKey.zip"
    ndir = home / f"napcat-{tag}"
    if ndir.exists():
        shutil.rmtree(ndir, ignore_errors=True)
    ndir.mkdir(parents=True)
    old_rec = _records().get("napcat")
    try:
        job_line(f"正在下载 NapCat {tag} 一键包…")
        zp = ndir / "NapCat.OneKey.zip"
        download(url, zp, progress=lambda g, t: job_progress(0.1 * g / t if t else None), github=True)
        with zipfile.ZipFile(zp) as z:
            z.extractall(ndir)
        inst = next(iter(ndir.glob("**/NapCatInstaller.exe")), None)
        if not inst:
            raise RuntimeError("一键包中未找到 NapCatInstaller.exe")
        job_line("正在运行 NapCat 安装器：下载新版 QQ 内核（约 300MB），需要几分钟…")
        t0 = time.time()

        def on(line):
            job_line(line[-200:])
            job_progress(min(0.75, 0.15 + (time.time() - t0) / 900))
        run_stream([str(inst)], on, cwd=str(inst.parent), timeout=1800)
        new_shell = installer._find_shell(ndir)
        if not new_shell:
            raise RuntimeError("新版 QQ 内核安装未完成")
        new_cfg = installer._napcat_cfg_dir_for(new_shell)
        job_line("正在将登录信息和连接设置迁移到新版本…")
        shutil.copytree(old_app / "config", new_cfg, dirs_exist_ok=True)
        job_progress(0.8)
        services.stop_napcat()
        time.sleep(2)
        update_state(napcat_dir=str(new_shell))
        how = _napcat_ok(180)
    except Exception as e:  # noqa: BLE001
        log_exc()
        job_line(f"未成功（{e}），正在恢复原版本…")
        try:
            services.stop_napcat()
            time.sleep(2)
        except Exception:
            log_exc()
        update_state(napcat_dir=str(old_shell))
        services.start_napcat(wait=False)
        shutil.rmtree(ndir, ignore_errors=True)
        raise RuntimeError(f"完整更新（含 QQ 内核）失败（{e}），已恢复为原来的 NapCat {cur}。")
    _drop_backup(old_rec)
    _save_record("napcat", {"from": cur, "to": tag.lstrip("v"), "at": time.time(), "kind": "dir",
                            "backup": str(old_shell), "full": True})
    job_progress(1.0)
    return f"NapCat 和 QQ 内核均已更新到 {tag}（{how}）。旧版本已完整保留作为备份（占用空间较大），确认运行正常后可在「版本与更新」页删除。"


def rollback_napcat() -> str:
    rec = _records().get("napcat")
    if not rec:
        raise RuntimeError("没有可用于回退的备份")
    cur = nc_current()
    job_line(f"回退到 NapCat {rec.get('from')}…")
    services.stop_napcat()
    time.sleep(2)
    if rec.get("kind") == "dir":
        old = Path(rec["backup"])
        if not (old / "NapCatWinBootMain.exe").exists():
            raise RuntimeError("备份的旧版 NapCat 文件夹已不存在，无法回退")
        now_shell = napcat_dir()
        update_state(napcat_dir=str(old))
        how = _napcat_ok()
        # 退回去以后，刚才那套新的就没用了
        if now_shell and now_shell != old:
            home = Path(load_state().get("home") or "")
            for p in (now_shell, *now_shell.parents):
                if home and p.parent == home and p.name.startswith("napcat-"):
                    shutil.rmtree(p, ignore_errors=True)
                    break
    else:
        _restore_napcat(napcat_app(), napcat_dir(), Path(rec["backup"]))
        how = _napcat_ok()
        _drop_backup(rec)
    _save_record("napcat", None)
    return f"已回退到 NapCat {rec.get('from')}（此前为 {cur}，{how}）。"


def delete_backup(comp: str) -> str:
    rec = _records().get(comp)
    if not rec:
        return "没有备份"
    if rec.get("kind") == "dir" and Path(rec["backup"]) == napcat_dir():
        raise RuntimeError("该备份正在使用，无法删除")
    _drop_backup(rec)
    _save_record(comp, None)
    return "备份已删除"
