"""控制台自己的更新：读取 GitHub Release 上的 latest.json → 校验签名 → 下载更新包 → 核对 sha256 → 备份 → 覆盖。

- latest.json 由作者电脑上的 scripts/release.py 生成并用私钥签名；这里用 core.UPDATE_PUBKEY 校验。
  签名不对（加速站被篡改、缓存了别的东西）就丢弃，不提示更新。没有公钥（还没正式发过版）时不检查。
- 只读最新正式版（releases/latest），预发布不会被检测到。
- 更新包只含「从压缩包更新」会覆盖的文件（UPDATE_PREFIXES），不含自带 Python；版本低于 update_min_from 时
  只能下载完整包手动更新。
- 安装前把即将被覆盖的文件打包备份（只留最近一份），安装出错自动装回备份，「回退」也用这份备份。
- 开发环境（Kit 里有 AGENTS.md，发布包里没有）只显示版本，不允许一键安装，免得覆盖作者的开发目录。

演练用的开关（普通用户不会设置）：环境变量 DAFEIYU_UPDATE_MANIFEST 指定 latest.json 地址（可以是 file:///），
DAFEIYU_UPDATE_PUBKEY 指定校验用的公钥。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path

import ed25519
from core import GH_PROXIES, KIT, LOG_DIR, UPDATE_PUBKEY, UPDATE_REPO, VERSION, download, log

MANIFEST_URL = f"https://github.com/{UPDATE_REPO}/releases/latest/download/latest.json"
RELEASES_PAGE = f"https://github.com/{UPDATE_REPO}/releases/latest"
ENV_MANIFEST = "DAFEIYU_UPDATE_MANIFEST"
ENV_PUBKEY = "DAFEIYU_UPDATE_PUBKEY"

# 「从压缩包更新」会覆盖的文件。scripts/release.py 的更新包按同一份清单挑文件（测试会核对两边一致）。
UPDATE_PREFIXES = ("app/", "START.vbs", "README.txt", "LICENSE", "LICENSES/", "CHANGELOG.md", ".gitignore", "版本切换.bat")
ROOT_DIR = "Dafeiyu"


# ---------------------------------------------------------------- 小工具

def vtuple(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def newer(latest: str, current: str) -> bool:
    return bool(latest) and bool(current) and vtuple(latest) > vtuple(current)


def pubkey() -> str:
    return (os.environ.get(ENV_PUBKEY) or UPDATE_PUBKEY or "").strip().lower()


def enabled() -> bool:
    return bool(pubkey())


def dev_mode() -> bool:
    return (KIT / "AGENTS.md").exists()


def in_update(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in UPDATE_PREFIXES)


def canonical(payload: dict) -> bytes:
    """和 scripts/release.py 的签名方式一致：键排序、紧凑分隔符、不转义中文。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_doc(doc: dict, key_hex: str) -> dict:
    """校验 latest.json，通过就返回 payload，否则抛错。"""
    try:
        payload, sig = doc["payload"], bytes.fromhex(doc["sig"])
        ok = ed25519.verify(bytes.fromhex(key_hex), canonical(payload), sig)
    except (KeyError, TypeError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise ValueError("签名校验失败")
    upd = payload.get("update") or {}
    if not (isinstance(payload.get("version"), str) and re.fullmatch(r"[0-9a-f]{64}", str(upd.get("sha256", "")))
            and str(upd.get("url", "")).startswith(("https://", "file:")) and int(upd.get("size") or 0) > 0):
        raise ValueError("更新信息缺少必要内容")
    return payload


def _host(u: str) -> str:
    """出错说明里用的地址简称：github.com、gh-proxy.com 这样；本地文件写「本地文件」。"""
    if u.startswith("https://") and "https://github.com/" in u[8:]:
        return u.split("/")[2]                      # 加速站
    return u.split("/")[2] if u.startswith(("http://", "https://")) else "本地文件"


def _manifest_urls() -> list[str]:
    env = os.environ.get(ENV_MANIFEST)
    return [env] if env else [p + MANIFEST_URL for p in GH_PROXIES]


def _download_urls(url: str) -> list[str]:
    return [p + url for p in GH_PROXIES] if url.startswith("https://github.com/") else [url]


# ---------------------------------------------------------------- 检查

def fetch_manifest() -> dict:
    """依次试直连和各加速站，返回第一份签名正确的 payload。全部失败抛 RuntimeError。"""
    key = pubkey()
    if not key:
        raise RuntimeError("这个版本还没有启用自动更新")
    reasons = []
    for u in _manifest_urls():
        try:
            req = urllib.request.Request(u, headers={"User-Agent": f"DafeiyuKit/{VERSION}", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = r.read(512 * 1024)
            return verify_doc(json.loads(raw.decode("utf-8")), key)
        except ValueError as e:          # 签名不对、内容不全、不是 JSON：这个地址的内容不可信，换下一个
            reasons.append(f"{_host(u)}：{e}")
            log(f"更新信息不可信 {u}：{e}")
        except Exception as e:  # noqa: BLE001
            reasons.append(f"{_host(u)}：{str(e)[:80]}")
            log(f"读取更新信息失败 {u}：{e}")
    bad_sig = any("签名" in r for r in reasons)
    raise RuntimeError(("更新包校验失败：更新信息的签名不对" if bad_sig and all("签名" in r or "内容" in r for r in reasons)
                        else "读取更新信息失败：直连和加速站都没连上") + "（" + "；".join(reasons[:3]) + "）")


def latest() -> dict:
    """给 updater.check 用：签名正确的最新版信息，没启用时返回 {}。"""
    if not enabled():
        return {}
    p = fetch_manifest()
    return {"version": p.get("version", ""), "date": p.get("date", ""), "notes": p.get("notes", ""),
            "todo": list(p.get("todo") or []), "update_min_from": p.get("update_min_from", ""),
            "page": p.get("page") or RELEASES_PAGE,
            "update_size": int((p.get("update") or {}).get("size") or 0),
            "full_size": int((p.get("full") or {}).get("size") or 0),
            "full_url": (p.get("full") or {}).get("url", "")}


def describe(cached: dict) -> dict:
    """「版本与更新」页的控制台卡片要的信息（不联网，用上次检查的结果）。"""
    lat = cached.get("version", "")
    min_from = cached.get("update_min_from", "")
    return {"current": VERSION, "latest": lat, "date": cached.get("date", ""), "notes": cached.get("notes", ""),
            "todo": cached.get("todo") or [], "newer": newer(lat, VERSION),
            "needs_full": bool(min_from) and vtuple(VERSION) < vtuple(min_from),
            "page": cached.get("page") or RELEASES_PAGE, "update_size": cached.get("update_size", 0),
            "full_size": cached.get("full_size", 0), "enabled": enabled(), "dev": dev_mode()}


# ---------------------------------------------------------------- 下载

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_update(payload: dict, progress=None) -> Path:
    """下载更新包，核对大小和 sha256；不一致就换下一个地址。progress(0~1)。"""
    upd = payload["update"]
    name = re.sub(r"[^\w.\-]", "_", upd.get("name") or f"update-{payload['version']}.zip")
    d = LOG_DIR / "更新下载"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    dest = d / name
    reasons = []
    for u in _download_urls(upd["url"]):
        try:
            download(u, dest, progress=(lambda g, t: progress(g / t) if t else None) if progress else None, github=False)
        except Exception as e:  # noqa: BLE001
            reasons.append(f"{_host(u)}：{str(e)[:80]}")
            continue
        size, digest = dest.stat().st_size, sha256_file(dest)
        if size == int(upd["size"]) and digest == upd["sha256"]:
            log(f"更新包校验通过 {u}")
            return dest
        reasons.append(f"{_host(u)}：内容和发布时不一致")
        log(f"更新包校验不通过 {u}：大小 {size}/{upd['size']}，sha256 {digest[:12]}…/{upd['sha256'][:12]}…")
        dest.unlink(missing_ok=True)
    if any("不一致" in r for r in reasons):
        raise RuntimeError("更新包校验失败：下载到的文件和作者发布的不一致（" + "；".join(reasons[:3]) + "）")
    raise RuntimeError("下载更新失败：直连和加速站都没下载成功（" + "；".join(reasons[:3]) + "）")


# ---------------------------------------------------------------- 安装、备份

def _zip_entries(z: zipfile.ZipFile) -> dict:
    """{解码后的文件名: ZipInfo}。没标 UTF-8 的压缩包，中文文件名按 UTF-8 还原。"""
    out = {}
    for info in z.infolist():
        n = info.filename
        if not info.flag_bits & 0x800:
            try:
                n = n.encode("cp437").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        out[n] = info
    return out


def package_version(path: Path) -> str:
    """检查是不是大肥鱼整合包的压缩包，返回里面的版本号。"""
    with zipfile.ZipFile(path) as z:
        ents = _zip_entries(z)
        core = next((n for n in ents if n.endswith("app/core.py")), None)
        if core is None:
            raise RuntimeError("这不是大肥鱼整合包的压缩包")
        m = re.search(r'^VERSION = "([^"]+)"', z.read(ents[core]).decode("utf-8", "replace"), re.M)
    return m.group(1) if m else "?"


def install_zip(path: Path, kit: Path = KIT) -> int:
    """把压缩包里 UPDATE_PREFIXES 范围内的文件覆盖到 kit，返回覆盖的文件数。旧的 app 另存一份到 app.old。"""
    with zipfile.ZipFile(path) as z:
        ents = _zip_entries(z)
        core = next((n for n in ents if n.endswith("app/core.py")), None)
        if core is None:
            raise RuntimeError("这不是大肥鱼整合包的压缩包")
        prefix = core[: -len("app/core.py")]
        old = kit / "app.old"
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        if (kit / "app").exists():
            shutil.copytree(kit / "app", old, ignore=shutil.ignore_patterns("__pycache__"))
        n = 0
        for name, info in ents.items():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            if not in_update(rel) or ".." in Path(rel).parts:
                continue
            tgt = kit / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fs, open(tgt, "wb") as fd:
                shutil.copyfileobj(fs, fd)
            n += 1
    return n


def backup_current(dest: Path, kit: Path = KIT) -> Path:
    """把 kit 里会被更新覆盖的文件打包成 dest（根目录 Dafeiyu/，可以直接用 install_zip 装回去）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(kit.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(kit).as_posix()
            if not in_update(rel) or "__pycache__/" in rel or rel.endswith(".pyc") or rel.startswith("app.old/"):
                continue
            z.write(p, f"{ROOT_DIR}/{rel}")
    tmp.replace(dest)
    return dest


def install_with_backup(path: Path, backup_dir: Path, kit: Path = KIT) -> tuple[Path, int]:
    """先备份再覆盖；覆盖中途出错就把备份装回去，再把错误抛出来。返回 (备份文件, 覆盖文件数)。"""
    cur = re.search(r'^VERSION = "([^"]+)"', (kit / "app" / "core.py").read_text(encoding="utf-8"), re.M)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bk = backup_current(Path(backup_dir) / f"控制台-v{cur.group(1) if cur else '?'}-{stamp}.zip", kit)
    try:
        n = install_zip(path, kit)
    except Exception:
        log("更新控制台出错，装回备份")
        install_zip(bk, kit)
        raise
    return bk, n
