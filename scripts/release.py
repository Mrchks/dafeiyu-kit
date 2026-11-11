"""大肥鱼整合包 发版脚本（只在作者电脑上用，不随包分发）。

用法（一般双击 scripts\\发版.bat）：
    python release.py 1.5.1          正式发布
    python release.py 1.5.1 --pre    预发布：Release 标为预发布，已安装的用户收不到，给朋友试装
    python release.py 1.5.1 --dry    只打包：不改仓库、不提交、不打标签、不上传，只在本地生成文件

正式发布做的事：
  1. 检查：在 main 上、工作区干净、标签不存在、版本号比现在大、CHANGELOG「未发布」里有内容。
  2. 没有签名密钥时生成一把（私钥放 C:\\AI\\Dafeiyu\\private\\，公钥写进 app/core.py 的 UPDATE_PUBKEY）。
  3. 改版本号（core.py、README.txt 第一行、CHANGELOG「未发布」→「vX.Y.Z · 日期」），提交，打带注释的标签。
  4. 打两个包到 C:\\AI\\Dafeiyu\\Releases\\vX.Y.Z\\：
       完整包：新用户安装用，含自带 Python；
       更新包：控制台自动更新用，只含「从压缩包更新」会覆盖的文件。
  5. 生成 latest.json（版本、说明、两个包的地址和 sha256、最低可直接升级版本、更新后待办），整体用私钥签名。
  6. 推送 main 和标签，创建 GitHub Release，上传两个包和 latest.json（用 Git 凭据管理器里保存的 GitHub 授权）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "app"))
import ed25519  # noqa: E402

REPO = "Mrchks/dafeiyu-kit"
ROOT_DIR = "Dafeiyu"                      # 压缩包里的根文件夹名
DAFEIYU = KIT.parent                      # C:\AI\Dafeiyu
KEY_FILE = Path(os.environ.get("DAFEIYU_RELEASE_KEY") or DAFEIYU / "private" / "release-ed25519.key")
OUT_ROOT = Path(os.environ.get("DAFEIYU_RELEASE_OUT") or DAFEIYU / "Releases")

# 低于这个版本的用户不能直接用更新包升级（例如自带 Python 换了），控制台会提示下载完整包。
# 自带 Python 或 runtime 有变化时改成新版本号。
UPDATE_MIN_FROM = "1.5.0"

# 只给开发用、不进发布包的文件
EXCLUDE = ("AGENTS.md", "AGENTS.local.md", "CLAUDE.md", "docs/", "tests/", "scripts/")
# 不在 Git 里、但要进发布包的文件
EXTRA_FILES = ("版本切换.bat",)
# 「从压缩包更新」会覆盖的文件（与 app/server.py 的 update_from_zip 保持一致），更新包只放这些
UPDATE_PREFIXES = ("app/", "START.vbs", "README.txt", "LICENSES/", "CHANGELOG.md", ".gitignore", "版本切换.bat")

FULL_LOCAL = "大肥鱼整合包-v{v}.zip"
UPDATE_LOCAL = "大肥鱼更新包-v{v}.zip"
FULL_ASSET = "Dafeiyu-Kit-v{v}.zip"         # GitHub 上的文件名只用英文，中文会被改名
UPDATE_ASSET = "Dafeiyu-Update-v{v}.zip"


# ---------- 小工具 ----------

def say(msg: str = "") -> None:
    print(msg, flush=True)


class ReleaseError(RuntimeError):
    pass


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def git_exe() -> str:
    g = shutil.which("git")
    if g:
        return g
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        for sub in (r"Git\cmd\git.exe", r"Git\bin\git.exe"):
            p = os.path.join(base, sub) if base else ""
            if p and os.path.exists(p):
                return p
    raise ReleaseError("没找到 Git，请先安装 Git for Windows")


def git(*args: str, input_text: str | None = None, check: bool = True) -> str:
    cmd = [git_exe(), "-C", str(KIT), "-c", "core.quotepath=false", "-c", "safe.directory=*",
           "-c", "credential.helper=", "-c", "credential.helper=manager", *args]
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args[:2])} 失败：{(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


def read_raw(p: Path, enc: str = "utf-8") -> str:
    """读文本但不改换行符（README.txt 用 utf-8-sig 去掉 BOM）。"""
    with open(p, encoding=enc, newline="") as f:
        return f.read()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 文本改写（纯函数，测试里直接调） ----------

def current_version(core_text: str) -> str:
    m = re.search(r'^VERSION = "([^"]+)"', core_text, re.M)
    if not m:
        raise ReleaseError("app/core.py 里找不到 VERSION")
    return m.group(1)


def set_core(core_text: str, version: str, pubkey_hex: str | None = None) -> str:
    out, n = re.subn(r'^VERSION = "[^"]+"', f'VERSION = "{version}"', core_text, count=1, flags=re.M)
    if n != 1:
        raise ReleaseError("app/core.py 里找不到 VERSION")
    if pubkey_hex is not None:
        out, n = re.subn(r'^UPDATE_PUBKEY = "[0-9a-f]*"', f'UPDATE_PUBKEY = "{pubkey_hex}"', out, count=1, flags=re.M)
        if n != 1:
            raise ReleaseError("app/core.py 里找不到 UPDATE_PUBKEY")
    return out


def set_readme(readme_text: str, version: str) -> str:
    first, sep, rest = readme_text.partition("\n")
    new_first, n = re.subn(r"v\d+\.\d+\.\d+", f"v{version}", first, count=1)
    if n != 1:
        raise ReleaseError("README.txt 第一行里找不到版本号")
    return new_first + sep + rest


def unreleased_body(changelog_text: str) -> str:
    m = re.search(r"^## 未发布[ \t]*\n(.*?)(?=^## |\Z)", changelog_text, re.M | re.S)
    body = m.group(1).strip() if m else ""
    return "" if body in ("", "（暂无）", "(暂无)") else body


def set_changelog(changelog_text: str, version: str, date: str) -> str:
    if not unreleased_body(changelog_text):
        raise ReleaseError("CHANGELOG.md「## 未发布」里没有内容，先写好这次改了什么")
    new, n = re.subn(r"^## 未发布[ \t]*$", f"## 未发布\n\n（暂无）\n\n## v{version} · {date}", changelog_text,
                     count=1, flags=re.M)
    if n != 1:
        raise ReleaseError("CHANGELOG.md 里找不到「## 未发布」")
    return new


def changelog_section(changelog_text: str, version: str) -> str:
    m = re.search(rf"^## v{re.escape(version)} · .*?\n(.*?)(?=^## |\Z)", changelog_text, re.M | re.S)
    return m.group(1).strip() if m else ""


def todo_items(notes: str) -> list[str]:
    """「### 升级须知」下的每一条，作为更新后待办。"""
    m = re.search(r"^### 升级须知[ \t]*\n(.*?)(?=^### |\Z)", notes, re.M | re.S)
    if not m:
        return []
    return [re.sub(r"^[-*]\s*", "", ln).strip() for ln in m.group(1).splitlines() if re.match(r"^\s*[-*]\s+\S", ln)]


# ---------- 选文件、打包 ----------

def is_excluded(rel: str) -> bool:
    return any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in EXCLUDE)


def in_update(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in UPDATE_PREFIXES)


def pick_files(tracked: list[str], extras: list[str], runtime: list[str]) -> tuple[list[str], list[str]]:
    """返回 (完整包文件, 更新包文件)，都是相对 Kit 的路径。"""
    base = sorted({r for r in tracked + extras if not is_excluded(r)})
    rt = sorted(r for r in runtime if "__pycache__/" not in r and not r.endswith(".pyc"))
    return base + rt, [r for r in base if in_update(r)]


def list_runtime() -> list[str]:
    rt = KIT / "runtime"
    if not rt.exists():
        return []
    return [p.relative_to(KIT).as_posix() for p in rt.rglob("*") if p.is_file()]


def build_zip(dest: Path, files: list[str], overrides: dict[str, bytes]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            arc = f"{ROOT_DIR}/{rel}"
            if rel in overrides:
                z.writestr(zipfile.ZipInfo(arc, date_time=dt.datetime.now().timetuple()[:6]), overrides[rel],
                           compress_type=zipfile.ZIP_DEFLATED)
            else:
                z.write(KIT / rel, arc)
    tmp.replace(dest)


def check_zip(path: Path, version: str, need_runtime: bool) -> int:
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ReleaseError(f"{path.name} 校验失败")
        names = z.namelist()
        core = z.read(f"{ROOT_DIR}/app/core.py").decode("utf-8")
    if current_version(core) != version:
        raise ReleaseError(f"{path.name} 里的版本号不对")
    bad = [n for n in names if is_excluded(n[len(ROOT_DIR) + 1:])]
    if bad:
        raise ReleaseError(f"{path.name} 混进了开发文件：{bad[:3]}")
    has_rt = any(n.startswith(f"{ROOT_DIR}/runtime/") for n in names)
    if has_rt != need_runtime:
        raise ReleaseError(f"{path.name} 的自带 Python {'缺失' if need_runtime else '不该出现'}")
    return len(names)


# ---------- 签名 ----------

def canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(payload: dict, secret: bytes) -> dict:
    return {"payload": payload, "sig": ed25519.sign(secret, canonical(payload)).hex(),
            "key": ed25519.public_key(secret).hex()[:16]}


def verify_manifest(doc: dict, pubkey_hex: str) -> bool:
    try:
        return ed25519.verify(bytes.fromhex(pubkey_hex), canonical(doc["payload"]), bytes.fromhex(doc["sig"]))
    except (KeyError, ValueError, TypeError):
        return False


def load_or_create_key(create: bool) -> tuple[bytes, bool]:
    if KEY_FILE.exists():
        secret = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
        if len(secret) != 32:
            raise ReleaseError(f"签名私钥文件损坏：{KEY_FILE}")
        return secret, False
    if not create:
        return os.urandom(32), True  # 只打包且没有密钥：用临时密钥，仅供自测
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    KEY_FILE.write_text(secret.hex() + "\n", encoding="ascii")
    return secret, True


# ---------- GitHub ----------

def gh_token() -> str:
    out = git("credential", "fill", input_text="protocol=https\nhost=github.com\n\n")
    for ln in out.splitlines():
        if ln.startswith("password="):
            return ln[len("password="):]
    raise ReleaseError("没拿到 GitHub 授权：先双击 scripts\\推送到GitHub.bat 完成一次授权")


def gh(method: str, url: str, token: str, data=None, raw: bytes | None = None, ctype: str = "application/json"):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "dafeiyu-release")
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        raise ReleaseError(f"GitHub 接口 {method} {url.split('?')[0]} 返回 {e.code}：{e.read()[:300]!r}")
    return json.loads(txt) if txt else {}


def publish(version: str, notes: str, pre: bool, assets: list[tuple[str, Path, str]]) -> str:
    token = gh_token()
    api = f"https://api.github.com/repos/{REPO}"
    tag = f"v{version}"
    rel = gh("GET", f"{api}/releases/tags/{urllib.parse.quote(tag)}", token)
    info = {"tag_name": tag, "name": tag, "body": notes, "prerelease": pre, "make_latest": "false" if pre else "true"}
    if rel is None:
        rel = gh("POST", f"{api}/releases", token, info)
    else:
        rel = gh("PATCH", f"{api}/releases/{rel['id']}", token, info)
    for a in rel.get("assets", []):
        if a["name"] in {n for n, _, _ in assets}:
            gh("DELETE", f"{api}/releases/assets/{a['id']}", token)
    upload = rel["upload_url"].split("{")[0]
    for name, path, ctype in assets:
        say(f"  上传 {name}（{path.stat().st_size // 1024} KB）…")
        got = gh("POST", f"{upload}?name={urllib.parse.quote(name)}", token, raw=path.read_bytes(), ctype=ctype)
        if got.get("name") != name:
            raise ReleaseError(f"上传后文件名变成了 {got.get('name')}，和 latest.json 对不上")
    return rel["html_url"]


# ---------- 主流程 ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大肥鱼整合包发版")
    ap.add_argument("version")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--pre", action="store_true", help="预发布")
    g.add_argument("--dry", action="store_true", help="只打包")
    ap.add_argument("--yes", action="store_true", help="不再询问确认")
    a = ap.parse_args(argv)
    version = a.version.strip().lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError("版本号写成 1.5.1 这样")
    mode = "只打包" if a.dry else ("预发布" if a.pre else "正式发布")
    tag = f"v{version}"
    today = dt.date.today().isoformat()

    core_p, readme_p, log_p = KIT / "app/core.py", KIT / "README.txt", KIT / "CHANGELOG.md"
    core_t = read_raw(core_p)
    readme_t = read_raw(readme_p, "utf-8-sig")
    log_t = read_raw(log_p)
    cur = current_version(core_t)

    # 1. 检查。标签已存在且就是当前提交时，视为上次推送或上传中断后的续跑：不再改版本号和提交。
    resume = False
    if not a.dry:
        if git("rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
            raise ReleaseError("请先回到 main 分支")
        if git("status", "--porcelain").strip():
            raise ReleaseError("工作区有没提交的改动，先提交或撤销")
        if git("tag", "-l", tag).strip():
            same = git("rev-parse", f"{tag}^{{commit}}").strip() == git("rev-parse", "HEAD").strip()
            if not (same and cur == version):
                raise ReleaseError(f"标签 {tag} 已存在")
            resume = True
    if resume:
        notes = changelog_section(log_t, version)
    else:
        if vtuple(version) <= vtuple(cur) and not a.dry:
            raise ReleaseError(f"新版本号 {version} 要比现在的 {cur} 大")
        notes = unreleased_body(log_t)
    if not notes:
        raise ReleaseError("CHANGELOG.md「## 未发布」里没有内容")

    say(f"大肥鱼整合包发版：{cur} → {version}（{mode}{'，续跑：跳过改版本号和提交' if resume else ''}）")
    say("本次更新说明：")
    say("  " + notes.replace("\n", "\n  "))
    if not a.dry and not a.yes:
        if input(f"\n确认{mode} {tag}？会提交、打标签、推送并上传到 GitHub。输入 yes 继续：").strip().lower() != "yes":
            say("已取消")
            return 1

    # 2. 密钥
    secret, new_key = load_or_create_key(create=not a.dry)
    pub = ed25519.public_key(secret).hex()
    m = re.search(r'^UPDATE_PUBKEY = "([0-9a-f]*)"', core_t, re.M)
    if not m:
        raise ReleaseError("app/core.py 里找不到 UPDATE_PUBKEY")
    if m.group(1) and m.group(1) != pub:
        if a.dry and new_key:
            say("提示：没有找到私钥，本次用临时密钥签名，仅供自测。")
        else:
            raise ReleaseError("私钥和 app/core.py 里的公钥对不上：私钥文件被换过？已安装的控制台只认原来的公钥")
    if new_key and not a.dry:
        say(f"\n已生成签名私钥：{KEY_FILE}\n请立刻备份到别处（U 盘、网盘加密目录）。私钥丢了，旧控制台就不认新版本的签名。\n")
    elif new_key:
        say("提示：没有找到私钥，本次用临时密钥签名，仅供自测（正式发布时会生成正式密钥）。")

    # 3. 改版本号
    if resume:
        if current_version(core_t) != version or f'UPDATE_PUBKEY = "{pub}"' not in core_t:
            raise ReleaseError("续跑时发现 app/core.py 的版本号或公钥不对")
        new_core, new_readme, new_log = core_t, readme_t, log_t
    else:
        new_core = set_core(core_t, version, pub)
        new_readme = set_readme(readme_t, version)
        new_log = set_changelog(log_t, version, today)
    overrides = {"app/core.py": new_core.encode("utf-8"),
                 "README.txt": ("\ufeff" + new_readme).encode("utf-8"),
                 "CHANGELOG.md": new_log.encode("utf-8")}
    if not a.dry and not resume:
        core_p.write_text(new_core, encoding="utf-8", newline="")
        readme_p.write_text(new_readme, encoding="utf-8-sig", newline="")
        log_p.write_text(new_log, encoding="utf-8", newline="")
        git("add", "app/core.py", "README.txt", "CHANGELOG.md")
        git("commit", "-m", tag)
        git("tag", "-a", tag, "-m", tag)
        say(f"已提交并打标签 {tag}")

    # 4. 打包
    out = OUT_ROOT / tag
    out.mkdir(parents=True, exist_ok=True)
    tracked = [ln for ln in git("ls-files").splitlines() if ln]
    extras = [e for e in EXTRA_FILES if (KIT / e).exists()]
    full_files, upd_files = pick_files(tracked, extras, list_runtime())
    full_p, upd_p = out / FULL_LOCAL.format(v=version), out / UPDATE_LOCAL.format(v=version)
    say("打包完整包…")
    build_zip(full_p, full_files, overrides)
    say("打包更新包…")
    build_zip(upd_p, upd_files, overrides)
    n_full = check_zip(full_p, version, need_runtime=bool(list_runtime()))
    n_upd = check_zip(upd_p, version, need_runtime=False)

    # 5. latest.json
    dl = f"https://github.com/{REPO}/releases/download/{tag}/"
    payload = {
        "schema": 1,
        "version": version,
        "date": today,
        "prerelease": bool(a.pre),
        "notes": notes,
        "todo": todo_items(notes),
        "update_min_from": UPDATE_MIN_FROM,
        "update": {"name": UPDATE_ASSET.format(v=version), "size": upd_p.stat().st_size, "sha256": sha256_file(upd_p),
                   "url": dl + UPDATE_ASSET.format(v=version)},
        "full": {"name": FULL_ASSET.format(v=version), "size": full_p.stat().st_size, "sha256": sha256_file(full_p),
                 "url": dl + FULL_ASSET.format(v=version)},
        "page": f"https://github.com/{REPO}/releases/tag/{tag}",
    }
    doc = sign_manifest(payload, secret)
    if not verify_manifest(doc, pub):
        raise ReleaseError("签名自检失败")
    latest_p = out / "latest.json"
    latest_p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    say(f"\n已生成（{out}）：")
    say(f"  {full_p.name}  {full_p.stat().st_size // 1024} KB，{n_full} 个文件")
    say(f"  {upd_p.name}  {upd_p.stat().st_size // 1024} KB，{n_upd} 个文件")
    say(f"  latest.json  已签名（公钥 {pub[:16]}…）")

    if a.dry:
        say("\n只打包模式：没有改动仓库，也没有上传。")
        return 0

    # 6. 推送与上传
    say("\n推送 main 和标签…")
    git("push", "origin", "main", tag)
    say("创建 GitHub Release 并上传…")
    url = publish(version, notes, bool(a.pre), [
        (payload["full"]["name"], full_p, "application/zip"),
        (payload["update"]["name"], upd_p, "application/zip"),
        ("latest.json", latest_p, "application/json"),
    ])
    say(f"\n完成：{url}")
    if a.pre:
        say("这是预发布：已安装的用户不会收到，把完整包或更新包发给试装的朋友即可。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as e:
        say(f"\n发版中止：{e}")
        sys.exit(2)
    except KeyboardInterrupt:
        say("\n已取消")
        sys.exit(1)
