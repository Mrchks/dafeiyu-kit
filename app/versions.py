"""版本管理：用 Git 给整合包（程序部分）记版本，能看改动、能回滚。

- 整合包文件夹就是一个 Git 仓库。runtime、日志、state.json（里面有加密的密码）、知识库资料都不进仓库，见 .gitignore。
- 每个发布版本打一个标签 vX.Y.Z。回滚 = 切到那个标签（git checkout -f），回到最新 = 切回 main。
- 没装 Git（https://git-scm.com）的电脑上，这些功能自动不可用，其他照常。
- 只管控制台和配置包模板。已经装进 AstrBot 的插件、人设、设置不受影响；那些靠「备份」页回滚。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from core import KIT, VERSION, log, run

GITIGNORE = """# 大肥鱼整合包：这些是本机的运行文件和个人数据，不进版本管理
runtime/
logs/
edge-profile/
app.old/
state.json
*.tmp
*.bak
__pycache__/
*.pyc
知识库/
版本切换.bat
"""
BRANCH = "main"


def git_exe() -> str | None:
    g = shutil.which("git")
    if g:
        return g
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        for sub in (r"Git\cmd\git.exe", r"Git\bin\git.exe"):
            p = os.path.join(base, sub) if base else ""
            if p and os.path.exists(p):
                return p
    return None


def G(*args: str, timeout: int = 60) -> tuple[int, str]:
    exe = git_exe()
    if not exe:
        return -1, "未安装 Git"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
    return run([exe, "-C", str(KIT), "-c", "core.quotepath=false", "-c", "safe.directory=*", *args],
               timeout=timeout, env=env)


def is_repo() -> bool:
    return (KIT / ".git").exists()


def ensure_repo() -> str:
    """没有仓库就建一个，把当前版本记成第一个版本。控制台启动时在后台调用。"""
    if not git_exe():
        return "未安装 Git，跳过版本管理"
    gi = KIT / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE, encoding="utf-8")
    if is_repo():
        return "已有仓库"
    G("init")
    G("checkout", "-b", BRANCH)
    for k, v in (("user.name", "大肥鱼控制台"), ("user.email", "dafeiyu@localhost"),
                 ("core.autocrlf", "false"), ("core.filemode", "false")):
        G("config", k, v)
    G("add", "-A")
    code, out = G("commit", "-m", f"v{VERSION}（建立版本记录）")
    if code == 0:
        G("tag", "-a", f"v{VERSION}", "-m", f"v{VERSION}")
    log(f"版本管理：建立仓库 {out[:80]}")
    return "已建立版本记录"


def _dirty() -> bool:
    code, out = G("status", "--porcelain")
    return code == 0 and bool(out.strip())


def _branch() -> str:
    code, out = G("symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if code == 0 else ""


_STATUS = {"t": 0.0, "v": None}


def status(force: bool = False) -> dict:
    """「版本与更新」页用。几条 git 命令同时跑（Windows 上每条要起一个进程，挨个跑要半秒多），结果留 15 秒。"""
    if not force and _STATUS["v"] and time.time() - _STATUS["t"] < 15:
        return _STATUS["v"]
    if not git_exe():
        return {"git": False}
    if not is_repo():
        return {"git": True, "repo": False}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as ex:
        f_branch = ex.submit(_branch)
        f_head = ex.submit(G, "describe", "--tags", "--always")
        f_tags = ex.submit(G, "for-each-ref", "refs/tags", "--sort=-creatordate",
                           "--format=%(refname:short)\t%(creatordate:format-local:%Y-%m-%d %H:%M)\t%(contents:subject)")
        f_log = ex.submit(G, "log", BRANCH, "-n", "40", "--date=format-local:%Y-%m-%d %H:%M", "--format=%h\t%ad\t%D\t%s")
        f_cur = ex.submit(G, "rev-parse", "--short", "HEAD")
        f_dirty = ex.submit(_dirty)
    branch, (_, head), (_, tags_out), (_, log_out), (_, cur) = (f_branch.result(), f_head.result(), f_tags.result(),
                                                                 f_log.result(), f_cur.result())
    tags = []
    for line in tags_out.splitlines():
        p = line.split("\t")
        if len(p) >= 3 and re.fullmatch(r"v\d+(\.\d+)*", p[0]):     # 只列发布版本（v1.2.3），不列其他本地标签
            tags.append({"tag": p[0], "date": p[1], "msg": p[2]})
    commits = []
    for line in log_out.splitlines():
        p = line.split("\t")
        if len(p) >= 4:
            commits.append({"id": p[0], "date": p[1], "refs": p[2], "msg": p[3]})
    v = {"git": True, "repo": True, "branch": branch, "detached": not branch, "head": head.strip(),
         "commit": cur.strip(), "dirty": f_dirty.result(), "version": VERSION, "tags": tags, "commits": commits}
    _STATUS.update(t=time.time(), v=v)
    return v


def _changed() -> None:
    _STATUS["v"] = None


def snapshot(message: str) -> str:
    """把当前改动记成一次提交（在 main 上）。没改动就什么都不做。"""
    if not is_repo() or not _dirty():
        return ""
    if not _branch():
        raise RuntimeError("当前停留在旧版本，请先返回最新版本再保存改动")
    G("add", "-A")
    code, out = G("commit", "-m", message)
    if code != 0:
        raise RuntimeError(out[-300:])
    _changed()
    _, cid = G("rev-parse", "--short", "HEAD")
    log(f"版本管理：提交 {cid.strip()} {message}")
    return cid.strip()


def release(version: str, message: str) -> str:
    """记一个发布版本：提交当前改动并打标签 v<version>。"""
    ensure_repo()
    snapshot(message)
    tag = f"v{version.lstrip('v')}"
    code, _ = G("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    if code != 0:
        G("tag", "-a", tag, "-m", message)
    _changed()
    return tag


def switch(ref: str) -> str:
    """切到某个版本（标签或提交）。当前有没保存的改动时，先自动保存。"""
    if not re.fullmatch(r"[\w.\-/]+", ref or ""):
        raise RuntimeError("版本号格式不正确")
    if _dirty():
        if _branch():
            snapshot(f"切换版本前自动保存（{time.strftime('%Y-%m-%d %H:%M')}）")
        else:
            G("stash", "push", "-u", "-m", "切换版本前自动暂存")
    code, out = G("checkout", "-f", ref)
    _changed()
    if code != 0:
        raise RuntimeError(out[-300:])
    log(f"版本管理：切换到 {ref}")
    return ref


def latest() -> str:
    return switch(BRANCH)


def version_in_tree() -> str:
    m = re.search(r'VERSION = "([^"]+)"', (KIT / "app" / "core.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "?"


def changelog() -> str:
    f = KIT / "CHANGELOG.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def changelog_sections() -> list[dict]:
    """把更新日志按「## 标题」切成一段一段：[{title, version, date, body, unreleased}]，新的在前。
    开头那段版本号规则是写给开发者看的，不返回。「未发布」里只有「（暂无）」的也不返回。"""
    out, cur = [], None
    for line in changelog().splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            title = m.group(1)
            parts = [x.strip() for x in title.split("·")]
            cur = {"title": title, "version": parts[0] if parts[0] != "未发布" else "",
                   "date": parts[1] if len(parts) > 1 else "", "body": "", "unreleased": parts[0] == "未发布"}
            out.append(cur)
        elif cur is not None:
            cur["body"] += line + "\n"
    for c in out:
        c["body"] = c["body"].strip()
    return [c for c in out if not (c["unreleased"] and (not c["body"] or c["body"] in ("（暂无）", "(暂无)")))]


def latest_release() -> dict | None:
    return next((c for c in changelog_sections() if not c["unreleased"]), None)
