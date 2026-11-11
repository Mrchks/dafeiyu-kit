"""新手安装向导的每一步。每步都是后台任务，失败可以重试，做过的会跳过。"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import manage
import modelhub
import services
from core import (zip_ok, GH_PROXIES, ASTRBOT_VERSION, CREATE_NO_WINDOW, KIT, LOG_DIR, PACK_DIR, PYPI_MIRRORS, RUNTIME_PY, WIN,
                  cmd_config_path, disk_free_gb, download, edge_path, github_latest_asset, is_ascii_path,
                  load_state, log, mem_gb, napcat_cfg_dir, ps, read_json, run, run_stream, set_wanted,
                  update_state, write_json)
from services import job_line, job_progress

NAPCAT_FALLBACK_TAG = "v4.18.41"
BOT_NAME = "大肥鱼"


def step_done(name: str) -> None:
    s = load_state()
    w = s["wizard"]
    done = w.setdefault("done", [])
    if name not in done:
        done.append(name)
    update_state(wizard=w)


def is_done(name: str) -> bool:
    return name in load_state()["wizard"].get("done", [])


# ---------------------------------------------------------------- 1 环境检查

def default_home() -> str:
    if is_ascii_path(str(KIT)):
        return str(KIT)
    for drive in ("D:\\", "E:\\", "C:\\"):
        if os.path.exists(drive) and disk_free_gb(drive) > 8:
            return drive + "Dafeiyu"
    return "C:\\Dafeiyu"


def check_env(home: str) -> list[dict]:
    out = []
    ver = platform.version()
    build = int(ver.split(".")[-1]) if ver.split(".")[-1].isdigit() else 0
    out.append({"ok": WIN and build >= 17763, "title": "Windows 10 / 11", "detail": f"{platform.system()} {platform.release()}（{ver}）",
                "fix": "需要 Windows 10 1809 以上或 Windows 11。"})
    out.append({"ok": is_ascii_path(home), "title": "安装位置是英文路径、不带空格", "detail": home,
                "fix": "换一个像 D:\\Dafeiyu 这样的路径。"})
    drive = os.path.splitdrive(home)[0] + "\\" if os.path.splitdrive(home)[0] else home
    free = disk_free_gb(drive)
    out.append({"ok": free >= 6, "title": "硬盘空间 6GB 以上", "detail": f"{drive} 剩 {free:.1f} GB",
                "fix": "清理一下硬盘，或换一个盘。"})
    mem = mem_gb()
    out.append({"ok": mem < 0 or mem >= 7.5, "warn_only": True, "title": "内存 8GB 以上（推荐 16GB）",
                "detail": f"{mem:.1f} GB" if mem > 0 else "未知", "fix": "内存小也能跑，就是可能卡一点。"})
    out.append({"ok": bool(edge_path()), "warn_only": True, "title": "装了 Edge 浏览器", "detail": edge_path() or "没找到",
                "fix": "控制台窗口用 Edge 打开；没有的话会用默认浏览器。"})
    # 网络：分别测安装要用到的几个地方，同时测，最多等十来秒
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def reach(url: str) -> bool:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            urllib.request.urlopen(req, timeout=8).close()
            return True
        except urllib.error.HTTPError as e:      # 有回应（哪怕 403/404/405）就说明连得上
            return e.code < 500
        except Exception:
            return False

    gh_target = "https://github.com/NapNeko/NapCatQQ/releases"
    checks = {"pypi": PYPI_MIRRORS[:2], "gh": [p + gh_target for p in GH_PROXIES],
              "deepseek": ["https://api.deepseek.com/"], "qq": ["https://qqdl.gtimg.cn/"]}
    with ThreadPoolExecutor(max_workers=12) as ex:
        res = {k: list(ex.map(reach, v)) for k, v in checks.items()}
    gh_ok = [GH_PROXIES[i] for i, ok in enumerate(res["gh"]) if ok]
    out.append({"ok": any(res["pypi"]), "title": "能连上国内软件源（下载 AstrBot 的组件）",
                "detail": "通" if any(res["pypi"]) else "阿里云、清华的软件源都连不上",
                "fix": "检查网络是否正常；开着代理的话先关掉再点「检查」（有的代理会挡住国内软件源）。"})
    out.append({"ok": bool(gh_ok), "title": "能下载 NapCat（GitHub 或加速站）",
                "detail": ("GitHub 直连可用" if "" in gh_ok else f"GitHub 直连不通，可用加速站 {len(gh_ok)} 个") if gh_ok
                else "GitHub 和加速站都连不上",
                "fix": "等几分钟再点「检查」，加速站时好时坏；或者换个网络（比如手机热点）、打开代理后再检查。"})
    out.append({"ok": res["qq"][0], "warn_only": True, "title": "能连上腾讯下载地址（下载 QQ 内核）",
                "detail": "通" if res["qq"][0] else "连不上 qqdl.gtimg.cn",
                "fix": "安装 NapCat 时需要从此地址下载约 300MB 的 QQ 内核。无法连接时，请更换网络后重试。"})
    out.append({"ok": res["deepseek"][0], "warn_only": True, "title": "可以连接 DeepSeek（bot 回复时需要）",
                "detail": "通" if res["deepseek"][0] else "连不上 api.deepseek.com",
                "fix": "bot 每次回复都需要连接 DeepSeek。无法连接时，请检查网络，或关闭代理后重试。"})
    return out


# ---------------------------------------------------------------- 2 安装 AstrBot

def _venv_python(home: Path) -> Path:
    return home / "astrbot-venv" / "Scripts" / "python.exe"


def _pip_install(py: Path, pkgs: list[str]) -> None:
    last = None
    for mirror in PYPI_MIRRORS:
        host = mirror.split("/")[2]
        job_line(f"从 {host} 下载 {' '.join(pkgs)}（第一次要几分钟）…")
        code = run_stream([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                           "--timeout", "60", "-i", mirror, "--trusted-host", host] + pkgs,
                          lambda l: job_line(l[-200:]) if any(k in l for k in ("Collecting", "Successfully", "ERROR", "error"))
                          else None, timeout=3600)
        if code == 0:
            return
        last = code
        job_line(f"这个源没装成（{code}），换下一个…")
    raise RuntimeError(f"装 AstrBot 失败（pip 退出码 {last}）。检查网络后点重试。")


def install_astrbot(home: str, user: str = "", password: str = "") -> str:
    # 新手不用自己想密码：没填就自动生成，加密存在本机，控制台里自动登录
    import dashlogin
    user = (user or "").strip() or "admin"
    password = password or dashlogin.gen_password()
    from_check = re.fullmatch(r"(?=.*[A-Z])(?=.*[a-z])(?=.*\d).{8,}", password or "")
    if not from_check:
        raise RuntimeError("后台密码至少 8 位，要有大写字母、小写字母和数字。")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", user or ""):
        raise RuntimeError("后台账号只能用 3–32 位英文、数字、下划线。")
    homep = Path(home)
    homep.mkdir(parents=True, exist_ok=True)
    update_state(home=str(homep))
    venv_py = _venv_python(homep)
    base_py = RUNTIME_PY / "python.exe"
    if not venv_py.exists():
        job_line("创建 Python 环境…")
        job_progress(0.05)
        code, out = run([str(base_py), "-m", "venv", str(homep / "astrbot-venv")], timeout=300)
        if code != 0:
            raise RuntimeError(f"创建 Python 环境失败：{out[-300:]}")
    job_progress(0.15)
    code, out = run([str(venv_py), "-c", "import importlib.metadata as m;print(m.version('astrbot'))"], timeout=60)
    if code != 0 or out.strip() != ASTRBOT_VERSION:
        _pip_install(venv_py, [f"astrbot=={ASTRBOT_VERSION}"])
    job_progress(0.8)
    root = homep / "astrbot"
    root.mkdir(exist_ok=True)
    cfg = root / "data" / "cmd_config.json"
    if not cfg.exists():
        job_line("初始化 AstrBot…")
        env = os.environ.copy()
        env.update({"ASTRBOT_DASHBOARD_INITIAL_PASSWORD": password, "PYTHONUTF8": "1"})
        code, out = run([str(venv_py), "-m", "astrbot.cli", "init", "-y"], cwd=str(root), env=env, timeout=300)
        job_line(out.strip().splitlines()[-1] if out.strip() else "")
        if not cfg.exists():
            raise RuntimeError(f"AstrBot 初始化失败：{out[-300:]}")
    # 设账号密码，免去第一次登录强制改密码
    dashlogin.write_login(user, password, py=venv_py, cfg=cfg, fresh_install=True)
    from core import protect
    update_state(mode="pip", astrbot_root=str(root), astrbot_python=str(venv_py.with_name("pythonw.exe")),
                 astrbot_cmd=["-m", "astrbot.cli", "run"], astrbot_env={}, dashboard_user=user,
                 dashboard_pass=protect(password))
    job_progress(1.0)
    step_done("astrbot")
    return "AstrBot 装好了"


# ---------------------------------------------------------------- 3 安装 NapCat

def _find_shell(d: Path) -> Path | None:
    hits = [p for p in d.glob("**/NapCat.*.Shell") if (p / "NapCatWinBootMain.exe").exists()]
    return sorted(hits)[-1] if hits else None


def install_napcat() -> str:
    s = load_state()
    home = Path(s["home"])
    ndir = home / "napcat"
    ndir.mkdir(parents=True, exist_ok=True)
    shell = _find_shell(ndir)
    if not shell:
        installer = next(iter(ndir.glob("**/NapCatInstaller.exe")), None)
        if not installer:
            job_line("查 NapCat 最新版本…")
            tag, url = github_latest_asset("NapNeko/NapCatQQ", lambda n: n == "NapCat.Shell.Windows.OneKey.zip")
            if not url:
                tag = tag or NAPCAT_FALLBACK_TAG
                url = f"https://github.com/NapNeko/NapCatQQ/releases/download/{tag}/NapCat.Shell.Windows.OneKey.zip"
            job_line(f"下载 NapCat 安装器 {tag}…")
            zp = ndir / "NapCat.OneKey.zip"
            download(url, zp, progress=lambda g, t: job_progress(0.1 * g / t if t else None), github=True)
            with zipfile.ZipFile(zp) as z:
                z.extractall(ndir)
            installer = next(iter(ndir.glob("**/NapCatInstaller.exe")), None)
            if not installer:
                raise RuntimeError("NapCat 安装器解压后没找到 NapCatInstaller.exe")
        zp_shell = installer.parent / "NapCat.Shell.zip"

        def shell_zip_bad() -> bool:
            """NapCat 官方安装器下载 NapCat 本体后不做校验，下到半截也会拿去解压，然后报「解压失败」。"""
            return zp_shell.exists() and not zip_ok(zp_shell)

        def drop_bad_zip() -> None:
            if shell_zip_bad():
                try:
                    zp_shell.unlink()
                    job_line("上次下载的 NapCat 本体不完整，已删掉，重新下载")
                except OSError:
                    pass

        code = None
        for attempt in range(1, 4):
            drop_bad_zip()
            job_line(f"运行 NapCat 安装器（第 {attempt} 次）：会下载 QQ 内核（约 300MB）和 NapCat，要几分钟…")
            job_progress(0.15)
            t0 = time.time()

            def on(l, t0=t0):
                job_line(l[-200:])
                if time.time() - t0 > 5:
                    job_progress(min(0.9, 0.15 + (time.time() - t0) / 600))
            code = run_stream([str(installer)], on, cwd=str(installer.parent), timeout=1800)
            shell = _find_shell(ndir)
            if shell:
                break
            if not shell_zip_bad():
                break               # 不是下载不完整，换下面的方式再试
            job_line("NapCat 本体下载到一半断开了（文件不完整），换一个下载地址再试…")
        if not shell and not shell_zip_bad():
            job_line("后台安装没成功，换成在单独的窗口里安装。请看着弹出的黑色窗口：装完它会停住，"
                     "出现「Press any key」时按任意键关掉就行，控制台会接着处理。")
            p = subprocess.Popen([str(installer)], cwd=str(installer.parent),
                                 creationflags=subprocess.CREATE_NEW_CONSOLE if WIN else 0)
            for _ in range(900):
                time.sleep(2)
                shell = _find_shell(ndir)
                if (shell and (shell / "napcat.bat").exists()) or p.poll() is not None:
                    time.sleep(5)
                    shell = _find_shell(ndir)
                    break
            try:
                p.kill()
            except Exception:
                pass
        if not shell:
            if shell_zip_bad():
                drop_bad_zip()
                raise RuntimeError("NapCat 本体连续几次都没下载完整（文件不完整，解压失败）。一般是网络不稳定，"
                                   "换个网络或打开代理后点重试。")
            raise RuntimeError(f"NapCat 没装好（安装器退出码 {code}）。点重试；还不行就生成问题报告发给作者。")
    update_state(napcat_dir=str(shell))
    job_progress(1.0)
    step_done("napcat")
    return "NapCat 装好了"


# ---------------------------------------------------------------- 4 配置（小号、主人、DeepSeek、连接）

def _napcat_cfg_dir_for(shell: Path) -> Path:
    hits = sorted(shell.glob("versions/*/resources/app/napcat"))
    if not hits:
        raise RuntimeError("NapCat 目录不完整，重新安装 NapCat。")
    d = hits[-1] / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure(bot_qq: str, owners: list[str], groups: list[str], deepseek_key: str = "", model_connection: dict | None = None) -> str:
    if not re.fullmatch(r"\d{5,12}", bot_qq or ""):
        raise RuntimeError("小号 QQ 号不对")
    owners = [o for o in owners if re.fullmatch(r"\d{5,12}", o)]
    if not owners:
        raise RuntimeError("至少填一个主人 QQ 号")
    if bot_qq in owners:
        raise RuntimeError("主人号不能和小号是同一个")
    groups = [g for g in groups if re.fullmatch(r"\d{5,12}", g)]
    connection_body = model_connection or ({"preset": "deepseek", "key": deepseek_key} if deepseek_key else None)
    connection_config = modelhub.connection(connection_body, "dafeiyu-setup") if connection_body else None
    if not connection_config and not is_done("configure"):
        raise ValueError("请先填写模型API Key")
    services.stop_astrbot()
    s = load_state()
    token = secrets.token_hex(20)
    ob_port = 6199
    # AstrBot
    p = cmd_config_path()
    c = read_json(p)
    plats = [x for x in c.get("platform", []) if x.get("type") != "aiocqhttp"]
    plats.append({"id": BOT_NAME, "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1",
                  "ws_reverse_port": ob_port, "ws_reverse_token": token})
    c["platform"] = plats
    if connection_config:
        source, provider = connection_config
        c["provider_sources"] = [x for x in c.get("provider_sources", []) if x.get("id") != source["id"]] + [source]
        c["provider"] = [x for x in c.get("provider", []) if x.get("provider_source_id") != source["id"]] + [provider]
        c.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("model", {})["provider_id"] = provider["id"]
        c.setdefault("provider_settings", {})["default_image_caption_provider_id"] = provider["id"] if "image" in provider["modalities"] else ""
    ps_ = c.setdefault("platform_settings", {})
    ps_["enable_id_white_list"] = True
    wl = [f"{BOT_NAME}:FriendMessage:{o}" for o in owners] + [f"{BOT_NAME}:GroupMessage:{g}" for g in groups]
    ps_["id_whitelist"] = list(dict.fromkeys(wl + [w for w in ps_.get("id_whitelist", []) if w not in wl]))
    c["admins_id"] = list(dict.fromkeys(owners + [a for a in c.get("admins_id", []) if a not in owners]))
    c.setdefault("provider_settings", {})["computer_use_runtime"] = "none"
    c["log_file_enable"] = True
    c.setdefault("log_file_path", "logs/astrbot.log")
    c.setdefault("dashboard", {})["host"] = "127.0.0.1"
    write_json(p, c)
    # NapCat
    shell = Path(s["napcat_dir"])
    cfgd = _napcat_cfg_dir_for(shell)
    ob = cfgd / f"onebot11_{bot_qq}.json"
    try:
        oc = read_json(ob)
    except Exception:
        oc = {"parseMultMsg": False, "imageDownloadProxy": "", "musicSignUrl": "", "enableLocalFile2Url": False,
              "network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketServers": [],
                          "websocketClients": [], "plugins": []}}
    net = oc.setdefault("network", {})
    wsc = [x for x in net.get("websocketClients", []) if x.get("name") != "大肥鱼-AstrBot"]
    wsc.append({"name": "大肥鱼-AstrBot", "enable": True, "url": f"ws://127.0.0.1:{ob_port}/ws", "messagePostFormat": "array",
                "reportSelfMessage": False, "reconnectInterval": 1000, "token": token, "debug": False, "heartInterval": 1000})
    net["websocketClients"] = wsc
    http_tok = s.get("onebot_http_token") or secrets.token_hex(16)
    hs = [x for x in net.get("httpServers", []) if x.get("name") != "dafeiyu-console"]
    hs.append({"name": "dafeiyu-console", "enable": True, "port": 3010, "host": "127.0.0.1", "enableCors": False,
               "enableWebsocket": False, "messagePostFormat": "array", "token": http_tok, "debug": False})
    net["httpServers"] = hs
    write_json(ob, oc, backup=False)
    wp = cfgd / "webui.json"
    try:
        wc = read_json(wp)
    except Exception:
        wc = {"host": "127.0.0.1", "port": 6099, "token": secrets.token_hex(12)}
    wc["autoLoginAccount"] = bot_qq
    wc["host"] = "127.0.0.1"
    write_json(wp, wc, backup=False)
    w = s["wizard"]
    w.update(groups=groups)
    update_state(bot_qq=bot_qq, owners=owners, onebot_http_token=http_tok, onebot_http_port=3010, wizard=w)
    step_done("configure")
    return "配置好了"


# ---------------------------------------------------------------- 5 启动并扫码

def start_for_login() -> str:
    services.start_astrbot()
    services.stop_napcat()
    time.sleep(2)
    services.start_napcat(wait=False)
    set_wanted("napcat", True)
    set_wanted("astrbot", True)
    return "已启动，等扫码"


# ---------------------------------------------------------------- 6 导入大肥鱼配置包

def _render_pack(owners: list[str]) -> Path:
    s = load_state()
    work = Path(s["home"]) / "configpack-work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(PACK_DIR, work)
    pf = work / "persona" / "大肥鱼.md"
    text = pf.read_text(encoding="utf-8")
    if len(owners) > 1:
        others = "、".join(owners[1:])
        text = text.replace("{OWNER_EXTRA}", f"主人还有别的号：{others}，也都是主人本人。")
    else:
        text = text.replace("{OWNER_EXTRA}", "")
    pf.write_text(text, encoding="utf-8")
    return work


def import_pack(voice: str, memory: str, keys: dict) -> str:
    s = load_state()
    from core import unprotect
    owners = s["owners"]
    if not owners:
        raise RuntimeError("还没设主人 QQ")
    if not services.astrbot_info()["running"]:
        services.start_astrbot()
    work = _render_pack(owners)
    tmp = Path(tempfile.mkdtemp(prefix="dfy-"))
    try:
        sec = tmp / "secrets.txt"
        lines = [f"password: {unprotect(s['dashboard_pass'])}"]
        ans = {"base_url": f"http://127.0.0.1:{s['astrbot_port']}", "username": s["dashboard_user"],
               "password_file": str(sec), "password_key": "password", "owner_qq": owners[0],
               "voice": {"edge": "1", "doubao": "3", "off": "0"}.get(voice, "1"),
               "memory": {"siliconflow": "2", "ollama": "1", "off": "0"}.get(memory, "0")}
        if ans["memory"] == "2":
            lines.append(f"siliconflow_key: {keys.get('siliconflow', '')}")
            ans.update(siliconflow_key_file=str(sec), siliconflow_key_key="siliconflow_key")
        if ans["voice"] == "3":
            lines += [f"volc_app_id: {keys.get('volc_app_id', '')}", f"volc_access_token: {keys.get('volc_access_token', '')}"]
            ans.update(volc_app_id_file=str(sec), volc_app_id_key="volc_app_id",
                       volc_access_token_file=str(sec), volc_access_token_key="volc_access_token")
        sec.write_text("\n".join(lines), encoding="utf-8")
        af = tmp / "answers.json"
        af.write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
        py = Path(s["astrbot_python"]).with_name("python.exe")
        results = []
        from services import job

        def on(l):
            job_line(l)
            if l.strip().startswith(("✔", "✘")):
                results.append(l.strip())
            if l.startswith("▶"):
                job_progress(min(0.95, (job.get("progress") or 0) + 0.08))
        job_progress(0.02)
        code = run_stream([str(py), "-X", "utf8", str(work / "tools" / "import_dafeiyu.py"), "--answers", str(af)], on,
                          cwd=str(work), timeout=3600)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [r for r in results if r.startswith("✘")]
    w = load_state()["wizard"]
    w.update(voice=voice, memory=memory, import_results=results)
    update_state(wizard=w)
    if code != 0 and not results:
        raise RuntimeError("导入工具没跑完，看上面的输出。点重试会跳过已经装好的部分。")
    step_done("import")
    job_progress(1.0)
    if bad:
        return "导入完成，但有几项没装上：" + "；".join(b[1:].strip() for b in bad) + "。可以点「重试」再跑一次。"
    return "大肥鱼导入完成"


# ---------------------------------------------------------------- 7 测试：等主人发消息、她回复

def wait_test(timeout: int = 300) -> str:
    owners = load_state()["owners"]
    t0 = time.time()
    seen_in = False
    from core import tail_lines
    start_len = len(tail_lines(services.astrbot_log_file(), 3000))
    while time.time() - t0 < timeout:
        lines = tail_lines(services.astrbot_log_file(), 3000)
        recent = lines[max(0, min(start_len, len(lines)) - 5):][-300:]
        for o in owners:
            if any(f"FriendMessage:{o}" in l or f"/{o}:" in l or f"/{o}" in l for l in recent):
                seen_in = True
        if seen_in and any("Prepare to send" in l for l in recent[-40:]):
            step_done("test")
            return "已收到 bot 的回复，测试通过。"
        time.sleep(3)
    if seen_in:
        raise RuntimeError("已收到测试消息，但 bot 没有回复。请点击「故障诊断」查看原因。")
    raise RuntimeError("5 分钟内未收到测试消息。请确认是使用主人的 QQ 号私聊小号，再点击「重试」。")


# ---------------------------------------------------------------- 8 完成：快捷方式、开机自启

def finish() -> str:
    start = KIT / "START.vbs"
    icon = KIT / "app" / "ui" / "dafeiyu.ico"
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        "$d = [Environment]::GetFolderPath('Desktop'); $st = [Environment]::GetFolderPath('Startup');"
        f"$t = Join-Path $env:WINDIR 'System32\\wscript.exe';"
        f"$l = $s.CreateShortcut((Join-Path $d '大肥鱼控制台.lnk')); $l.TargetPath = $t; "
        f"$l.Arguments = '\"{start}\"'; $l.WorkingDirectory = '{KIT}'; $l.IconLocation = '{icon}'; $l.Save();"
        f"$l = $s.CreateShortcut((Join-Path $st '大肥鱼控制台（开机自启）.lnk')); $l.TargetPath = $t; "
        f"$l.Arguments = '\"{start}\" /boot'; $l.WorkingDirectory = '{KIT}'; $l.IconLocation = '{icon}'; $l.Save();"
    )
    out = ps(script)
    if "Exception" in out or "错误" in out:
        log(f"快捷方式：{out}")
    # 新手刚装好进来不弹「更新内容」，从第二天开始
    import versions
    rel = versions.latest_release() or {}
    update_state(setup_done=True, setup_step="done", autostart=True,
                 notice={"last_date": time.strftime("%Y-%m-%d"), "last_version": rel.get("version", ""), "only_new": False})
    set_wanted("napcat", True)
    set_wanted("astrbot", True)
    step_done("finish")
    return "全部完成！桌面上有「大肥鱼控制台」，开机会自动上线。"


def remove_shortcuts() -> None:
    ps("$d=[Environment]::GetFolderPath('Desktop'); $st=[Environment]::GetFolderPath('Startup');"
       "Remove-Item -ErrorAction SilentlyContinue (Join-Path $d '大肥鱼控制台.lnk'), (Join-Path $st '大肥鱼控制台（开机自启）.lnk')")
