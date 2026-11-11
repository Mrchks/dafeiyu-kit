"""AstrBot 后台账号密码：自动生成、控制台里自动登录、忘了一键重设。

以前安装向导让新手自己想一个「8 位、带大小写和数字」的后台密码，之后在控制台点「全部设置」
又要再输一遍。新手要么卡在密码规则上，要么过两天就忘了，进不去后台。现在：

- 安装时账号密码自动生成（admin + 随机密码），DPAPI 加密存在本机，新手什么都不用填；
- 控制台里打开 AstrBot 后台时自动登录：往 AstrBot 自己的网页目录放一个小页面
  dafeiyu-login.html，控制台把登录令牌放在网址 # 后面交给它（# 后面的内容不会发给服务器），
  它写进 AstrBot 网页的本地存储再跳到首页——跟手动登录完全一样；
- 想在别的浏览器里登录，控制台能「查看账号密码」；
- 忘了 / 对不上（比如老用户在 AstrBot 里改过密码），可以一键重设：停 AstrBot → 改它配置里的账号密码 → 再启动。
"""

from __future__ import annotations

import os
import secrets
import string
import urllib.parse
from pathlib import Path

import astrbot_api
from core import astrbot_data, astrbot_root, cmd_config_path, load_state, log, protect, run, unprotect, update_state

PAGE_NAME = "dafeiyu-login.html"
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>正在登录…</title></head>
<body style="font:14px sans-serif;color:#888;padding:24px">正在登录 AstrBot 后台…
<script>
(function () {
  var h = new URLSearchParams(location.hash.slice(1)), t = h.get('t'), u = h.get('u');
  try {
    if (t) {
      localStorage.setItem('token', t);
      localStorage.setItem('user', u || '');
      ['change_pwd_hint', 'md5_pwd_hint', 'password_upgrade_required'].forEach(function (k) { localStorage.removeItem(k); });
    }
  } catch (e) {}
  history.replaceState(null, '', location.pathname);
  location.replace('/');
})();
</script></body></html>
"""

# 改 AstrBot 配置里的账号密码（用 AstrBot 自己的加密函数），顺便免掉第一次登录强制改密码
HELPER = (
    "import json,sys\n"
    "from astrbot.core.utils.auth_password import hash_dashboard_password as h, hash_md5_dashboard_password as m\n"
    "p,u,pw,local=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]=='1'\n"
    "c=json.load(open(p,encoding='utf-8-sig'))\n"
    "d=c.setdefault('dashboard',{})\n"
    "d.update(username=u,pbkdf2_password=h(pw),password=m(pw),password_storage_upgraded=True,password_change_required=False)\n"
    "d.update(host='127.0.0.1') if local else None\n"
    "json.dump(c,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
)


def gen_password(n: int = 14) -> str:
    """AstrBot 要求：至少 8 位，有大写、小写、数字。去掉容易看错的 0O1lI。"""
    alpha = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    while True:
        pw = "".join(secrets.choice(alpha) for _ in range(n))
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _python() -> Path | None:
    p = load_state().get("astrbot_python") or ""
    if not p:
        return None
    p = Path(p)
    console = p.with_name("python.exe")   # pythonw 没有输出，出错看不到原因
    return console if console.exists() else p


def write_login(user: str, password: str, py: Path | None = None, cfg: Path | None = None,
                fresh_install: bool = False) -> None:
    """把账号密码写进 AstrBot 配置（AstrBot 要停着，不然它退出时会把旧配置写回去）。
    fresh_install：安装向导刚装好时用，顺便把后台限制成只有本机能打开；重设密码时不动用户原来的设置。"""
    py = py or _python()
    cfg = cfg or cmd_config_path()
    if not py or not cfg or not Path(cfg).exists():
        raise RuntimeError("找不到 AstrBot 的配置文件，先在安装向导里装好 AstrBot。")
    env = os.environ.copy()
    if not fresh_install:
        env.update({k: str(v) for k, v in (load_state().get("astrbot_env") or {}).items() if not k.startswith("_")})
    env["PYTHONUTF8"] = "1"
    root = Path(cfg).parent.parent
    code, out = run([str(py), "-c", HELPER, str(cfg), user, password, "1" if fresh_install else "0"], cwd=str(root), env=env, timeout=120)
    if code != 0:
        raise RuntimeError(f"改后台密码失败：{out[-300:]}")


def reset() -> str:
    """一键重设（调用方负责先停 AstrBot、改完再启动）。账号沿用原来的，没有就用 admin。"""
    s = load_state()
    user = s.get("dashboard_user") or "admin"
    pw = gen_password()
    write_login(user, pw)
    update_state(dashboard_user=user, dashboard_pass=protect(pw))
    astrbot_api._tok.update(token=None, at=0.0)
    log("AstrBot 后台密码已重设")
    return f"后台密码重设好了，账号 {user}，控制台已经记住，会自动登录"


def credentials() -> dict:
    s = load_state()
    return {"user": s.get("dashboard_user") or "", "password": unprotect(s.get("dashboard_pass") or "")}


def _dist_dirs() -> list[Path]:
    out = []
    d, r = astrbot_data(), astrbot_root()
    for p in ([d / "dist"] if d else []) + ([r / "astrbot" / "dashboard" / "dist"] if r else []):
        if (p / "index.html").is_file():
            out.append(p)
    return out


def install_page() -> bool:
    """AstrBot 升级会换掉网页目录，所以每次要用前都放一遍。"""
    ok = False
    for d in _dist_dirs():
        f = d / PAGE_NAME
        try:
            if not f.exists() or f.read_text(encoding="utf-8") != PAGE:
                f.write_text(PAGE, encoding="utf-8")
            ok = True
        except OSError as e:
            log(f"放自动登录页失败 {d}: {e}")
    return ok


def login_url() -> str:
    """带登录令牌的 AstrBot 后台地址。没存密码或密码不对时抛 NEED_LOGIN。"""
    tok = astrbot_api.token()
    base = astrbot_api.base()
    if not install_page():
        return base + "/"
    frag = urllib.parse.urlencode({"t": tok, "u": load_state().get("dashboard_user") or ""})
    return f"{base}/{PAGE_NAME}#{frag}"
