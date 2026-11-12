"""大肥鱼配置包导入工具。

把 settings/pack.json 里写的东西装进你的 AstrBot：人格、六个插件和它们的设置、
表情包、记忆、语音，还有 plugins/ 文件夹里的额外插件。可以反复运行，已经装过的会跳过或更新。

用法：双击上一层的「导入大肥鱼.bat」，按提示回答几个问题。
测试用：python import_dafeiyu.py --answers 回答.json（回答文件里的 *_file 字段指向放密码或 key 的文本文件）。
"""
import base64
import getpass
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWERS = {}
# 表情包插件下 GitHub 用的加速站，按顺序试；最后的空串是直连 GitHub。加速站常换常挂，挂了往这里补新的
GH_ACCELERATORS = ["https://gh.dpik.top/", "https://hk.gh-proxy.com/", "https://ghfast.top/", "https://gh-proxy.com/", ""]


def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say(f"\n▶ {msg}")


def warn(msg):
    say(f"  ！{msg}")


def read_secret_file(path, key=None):
    lines = open(path, encoding="utf-8-sig").read().splitlines()
    if key:
        for line in lines:
            k, sep, v = line.replace("：", ":", 1).partition(":")  # 中文冒号也认
            if sep and k.strip().lower() == key:
                return v.strip()
    return lines[0].strip()


def want(name):
    """回答文件里给了 steps 时只做列出的几步（接入已有安装时只做增量，不覆盖原有设置）；没给时全部都做。"""
    steps = ANSWERS.get("steps")
    return not steps or name in steps


def ask(name, prompt, default=None, secret=False, choices=None):
    """问一个问题；--answers 模式下从回答文件取。"""
    if name in ANSWERS:
        return str(ANSWERS[name])
    if f"{name}_file" in ANSWERS:
        return read_secret_file(ANSWERS[f"{name}_file"], ANSWERS.get(f"{name}_key"))
    if ANSWERS:
        if default is not None:
            return default
        sys.exit(f"回答文件里缺 {name}")
    while True:
        hint = f"（直接回车用 {default}）" if default not in (None, "") else ""
        text = f"{prompt}{hint}："
        value = getpass.getpass(text) if secret else input(text)
        value = value.strip() or (default or "")
        if choices and value not in choices:
            say(f"  请输入 {' / '.join(choices)} 里的一个")
            continue
        if value:
            return value


class Api:
    def __init__(self, base):
        self.root = base.rstrip("/")
        self.token = None

    def call(self, method, path, body=None, timeout=120, raw_body=None, content_type="application/json"):
        url = self.root + "/api/v1" + path
        data = raw_body if raw_body is not None else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
                code = r.status
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            code = e.code
        try:
            out = json.loads(text) if text else {}
        except ValueError:
            out = {"status": "error", "message": text[:300]}
        if not isinstance(out, dict):
            out = {"status": "ok", "data": out}
        out["_http"] = code
        return out

    @staticmethod
    def ok(r):
        return r.get("_http", 200) < 400 and r.get("status", "ok") != "error"

    @staticmethod
    def msg(r):
        return str(r.get("message") or r.get("detail") or r.get("data") or r)[:300]

    def login(self, user, password):
        for pw in (password, hashlib.md5(password.encode()).hexdigest()):
            r = self.call("POST", "/auth/login", {"username": user, "password": pw})
            data = r.get("data")
            if isinstance(data, dict) and data.get("token"):
                self.token = data["token"]
                return True
        return False


def get_path(d, dotted):
    for k in dotted.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def set_path(d, dotted, value):
    keys = dotted.split(".")
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def fill(value, ctx):
    """把 {主人会话}、{对话模型} 这类占位换成真值。"""
    if isinstance(value, str):
        for k, v in ctx.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def launcher_data_dirs():
    """启动器的数据目录：默认在用户目录下；在启动器「高级」里改过的，记在 APPDATA 下的设置文件里。"""
    dirs = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        try:
            d = json.load(open(os.path.join(appdata, "com.github.raven95676.astrbot-launcher", ".data-dir.json"),
                               encoding="utf-8-sig")).get("data_dir", "").strip()
            if d:
                dirs.append(d)
        except Exception:
            pass
    dirs.append(os.path.join(os.path.expanduser("~"), ".astrbot_launcher"))
    return dirs


def find_instances():
    """启动器各实例配置里写的后台端口（启动器常另给端口，这个只当候选）。"""
    found = []
    cfgs = []
    for data_dir in launcher_data_dirs():
        cfgs += glob.glob(os.path.join(data_dir, "instances", "*", "core", "data", "cmd_config.json"))
    for cfg in cfgs:
        try:
            port = json.load(open(cfg, encoding="utf-8-sig")).get("dashboard", {}).get("port")
        except Exception:
            continue
        if port:
            found.append((cfg.split(os.sep)[-4], int(port)))
    return found


def python_listening_ports():
    """本机 python 进程正在监听的端口（AstrBot 后台一定在里面）。拿不到就返回空。"""
    ports = set()
    try:
        if os.name == "nt":
            names = {}
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 timeout=15, errors="replace").stdout
            for line in out.splitlines():
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) > 1:
                    names[cols[1]] = cols[0].lower()
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                 timeout=15, errors="replace").stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    host, _, port = parts[1].rpartition(":")
                    if host in ("127.0.0.1", "0.0.0.0") and names.get(parts[4], "").startswith("python"):
                        ports.add(int(port))
        else:
            out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True,
                                 timeout=15, errors="replace").stdout
            for line in out.splitlines():
                if line.lower().startswith("python") and "(LISTEN)" in line:
                    addr = line.split()[-2]
                    host, _, port = addr.rpartition(":")
                    if host in ("127.0.0.1", "*", "localhost") and port.isdigit():
                        ports.add(int(port))
    except Exception:
        pass
    return ports


def is_astrbot_dashboard(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return "AstrBot" in r.read(65536).decode("utf-8", "replace")
    except Exception:
        return False


def pick_backend():
    if "base_url" in ANSWERS:
        return ANSWERS["base_url"]
    candidates = sorted({port for _, port in find_instances()} | python_listening_ports())
    alive = [port for port in candidates if is_astrbot_dashboard(port)]
    if not alive:
        # 兜底：扫一遍 AstrBot 和启动器常用的端口段
        alive = [port for port in [*range(6185, 6201), *range(18600, 18621)]
                 if port not in candidates and is_astrbot_dashboard(port)]
    if len(alive) > 1:
        say("  找到好几个正在运行的 AstrBot：" + "、".join(f"127.0.0.1:{p}" for p in alive))
    default = f"http://127.0.0.1:{alive[0]}" if alive else "http://127.0.0.1:6185"
    url = ask("base_url", "AstrBot 后台地址（浏览器里打开后台时地址栏那串）", default)
    if not url.startswith("http"):
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def upsert_provider(api, conf):
    pid = conf["id"]
    existing = provider_config(api, pid)
    if existing:
        merged = {**existing, **conf}
        r = api.call("PUT", f"/providers/{urllib.parse.quote(pid, safe='')}", {"config": merged})
    else:
        r = api.call("POST", "/providers", {"config": conf})
    if not api.ok(r):
        warn(f"保存「{pid}」失败：{api.msg(r)}")
        return False
    return True


def provider_config(api, pid):
    """读一个模型提供商的配置；没有就返回 None。"""
    r = api.call("GET", f"/providers/{urllib.parse.quote(pid, safe='')}")
    data = r.get("data") if api.ok(r) and isinstance(r.get("data"), dict) else {}
    data = data.get("provider", data)
    return data if isinstance(data, dict) and data.get("id") == pid else None


def test_provider(api, pid):
    r = api.call("POST", "/providers/test", {"provider_id": pid}, timeout=90)
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    status = str(data.get("status") or r.get("status") or "")
    if api.ok(r) and status not in ("error", "unavailable", "failed"):
        return True, ""
    return False, str(data.get("error") or data.get("message") or api.msg(r))[:200]


def extra_plugins():
    root = os.path.join(PACK, "plugins")
    zips = glob.glob(os.path.join(root, "*.zip"))
    dirs = [d for d in glob.glob(os.path.join(root, "*")) if os.path.isfile(os.path.join(d, "metadata.yaml"))]
    return sorted(zips + dirs)


def plugin_name(path):
    meta = os.path.join(path, "metadata.yaml")
    if os.path.isfile(meta):
        for line in open(meta, encoding="utf-8-sig"):
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def zip_folder(folder):
    import io
    import zipfile
    buf = io.BytesIO()
    top = os.path.basename(folder.rstrip("\\/"))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in filenames:
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, f)
                zf.write(full, os.path.join(top, os.path.relpath(full, folder)))
    return buf.getvalue()


def multipart(field, filename, payload):
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + payload + tail, f"multipart/form-data; boundary={boundary}"


def main():
    global ANSWERS
    if "--answers" in sys.argv:
        ANSWERS = json.load(open(sys.argv[sys.argv.index("--answers") + 1], encoding="utf-8-sig"))
    pack = json.load(open(os.path.join(PACK, "settings", "pack.json"), encoding="utf-8-sig"))
    say("大肥鱼配置包导入工具")
    say("开始前确认：AstrBot 已经在运行，QQ 小号已经通过 NapCat 接上，DeepSeek 已经能正常回话。")

    # ---------- 连接后台 ----------
    api = Api(pick_backend())
    user = ask("username", "后台账号", "astrbot")
    password = ask("password", "后台密码（输入时不显示）", secret=True)
    if not api.login(user, password):
        sys.exit("登录失败：检查后台地址、账号、密码。")
    say("  已登录后台")

    owner = ask("owner_qq", "你自己的 QQ 号（大号，她会把它当主人）")
    if not owner.isdigit():
        sys.exit("QQ 号只能是数字")
    say("\n记忆二选一：1 本机 Ollama（免费，要先装 Ollama）  2 硅基流动（要一个硅基流动的 key）  0 先不装")
    memory = ask("memory", "选", "2", choices=["0", "1", "2"])
    sf_key = ask("siliconflow_key", "硅基流动 key（输入时不显示）", secret=True) if memory == "2" else ""
    say("\n语音选一个：3 豆包 Seed Audio（推荐，要火山引擎豆包语音的 APP ID 和 Access Token，按语音时长约 1 元/分钟）"
        "  4 本机 Breeze（免费，显卡在本机算，先用「装Breeze语音.bat」「做声音.bat」装好、做好声音）"
        "  1 微软 Edge 免费女声  2 小米 MiMo 克隆音（要 MiMo 的 key）  0 先不开")
    voice = ask("voice", "选", "3", choices=["0", "1", "2", "3", "4"])
    mimo_key = ask("mimo_key", "MiMo key（输入时不显示）", secret=True) if voice == "2" else ""
    volc_app_id = volc_token = ""
    if voice == "3":
        volc_app_id = ask("volc_app_id", "豆包语音 APP ID")
        volc_token = ask("volc_access_token", "豆包语音 Access Token（不是火山方舟的 API Key；输入时不显示）", secret=True)

    r = api.call("GET", "/config-profiles/default")
    cfg = (r.get("data") or {}).get("config") if isinstance(r.get("data"), dict) else None
    if not isinstance(cfg, dict):
        sys.exit(f"读不到配置：{api.msg(r)}")

    platforms = [p for p in cfg.get("platform", []) if p.get("type") == "aiocqhttp"]
    platforms.sort(key=lambda p: not p.get("enable", True))
    if not platforms:
        sys.exit("后台里还没有 QQ 机器人（OneBot v11），先按视频接好 NapCat 再来。")
    platform_id = platforms[0]["id"]
    chat_id = get_path(cfg, "agent_runner.config.model.provider_id") or ""
    if not chat_id:
        chats = [p for p in cfg.get("provider", []) if p.get("provider_type", "chat_completion") == "chat_completion" and p.get("enable", True)]
        chat_id = chats[0]["id"] if chats else ""
    if not chat_id:
        sys.exit("后台里还没有对话模型，先在控制台添加模型 API 再来。")
    session = f"{platform_id}:FriendMessage:{owner}"
    ctx = {"主人会话": session, "对话模型": chat_id,
           "识图模型": get_path(cfg, "provider_settings.default_image_caption_provider_id") or "",
           "唱歌后端": (pack.get("sing") or {}).get("backend") or "doubao"}
    say(f"  机器人：{platform_id}　对话模型：{chat_id}")
    results = []
    changed, installed = [], set()
    lm, mem_ok, tts_id, v = "astrbot_plugin_livingmemory", False, "", pack["voice"]

    if want("chat_model"):
        # ---------- 对话模型：关思考，回得快 ----------
        step("调整对话模型")
        prov = provider_config(api, chat_id)
        if prov and "deepseek" in (str(prov.get("model", "")) + str(prov.get("provider_source_id", ""))).lower():
            extra = dict(prov.get("custom_extra_body") or {})
            extra.update(pack["chat_model"]["custom_extra_body"])
            prov["custom_extra_body"] = extra
            ok = api.ok(api.call("PUT", f"/providers/{urllib.parse.quote(chat_id, safe='')}", {"config": prov}))
            say("  已关闭深度思考，回得更快" if ok else "  ！没改成，不影响使用")
        else:
            say("  不是 DeepSeek 模型，跳过")

    if want("persona"):
        # ---------- 人格 ----------
        step("写入人格「大肥鱼」")
        pid = pack["persona"]["id"]
        text = open(os.path.join(PACK, pack["persona"]["file"]), encoding="utf-8-sig").read().replace("{OWNER_QQ}", owner)
        qid = urllib.parse.quote(pid, safe="")
        cur = api.call("GET", f"/personas/{qid}")
        if api.ok(cur) and isinstance(cur.get("data"), dict) and cur["data"].get("persona_id") == pid and ANSWERS.get("persona_keep"):
            say("  已有同名人格，保留原内容，不覆盖")
            r = cur
        elif api.ok(cur) and isinstance(cur.get("data"), dict) and cur["data"].get("persona_id") == pid:
            body = {k: v for k, v in cur["data"].items() if k in ("begin_dialogs", "skills", "folder_id", "sort_order", "custom_error_message")}
            body.update({"system_prompt": text, "tools": None})
            r = api.call("PUT", f"/personas/{qid}", body)
        else:
            r = api.call("POST", "/personas", {"persona_id": pid, "system_prompt": text, "begin_dialogs": [], "tools": None})
        results.append(("人格", api.ok(r)))
        say("  完成" if api.ok(r) else f"  ！失败：{api.msg(r)}")

    if want("plugins"):
        # ---------- 插件 ----------
        step("从插件市场安装插件（第一次要下载，慢的话等一两分钟）")
        installed = {p.get("name") for p in (api.call("GET", "/plugins").get("data") or []) if isinstance(p, dict)}
        for p in pack["plugins"]:
            if p["name"] in installed:
                say(f"  {p['name']}：已装过")
                continue
            say(f"  {p['name']}：安装中…（{p['what']}）")
            r = api.call("POST", "/plugins/install/github",
                         {"install_method": "market", "market_plugin_id": p["market_id"]}, timeout=600)
            if not api.ok(r):
                warn(f"装不上：{api.msg(r)}")
                warn("插件是从插件市场的服务器下的，可能一时没响应。等几分钟再运行一次本工具；还装不上，就在后台「插件 → 插件市场」里搜这个插件手动装，装好再运行一次。")
        installed = {p.get("name") for p in (api.call("GET", "/plugins").get("data") or []) if isinstance(p, dict)}
        for p in pack["plugins"]:
            results.append((f"插件 {p['name']}", p["name"] in installed))

        # 额外插件（比如豆包语音）：plugins/ 里的 zip，或带 metadata.yaml 的插件文件夹
        for z in extra_plugins():
            base = z[:-4] if z.endswith(".zip") else z
            meta_path = base + ".json"
            meta = json.load(open(meta_path, encoding="utf-8-sig")) if os.path.exists(meta_path) else {}
            name = meta.get("name") or plugin_name(z) or os.path.basename(base)
            if meta.get("only_with_voice") and meta["only_with_voice"] != voice and name not in installed:
                continue
            if name in installed and not meta.get("always_reinstall"):
                say(f"  {name}：已装过")
            else:
                say(f"  {name}：从配置包安装…")
                payload = open(z, "rb").read() if z.endswith(".zip") else zip_folder(z)
                body, ctype = multipart("file", os.path.basename(base) + ".zip", payload)
                r = api.call("POST", "/plugins/install/upload", raw_body=body, content_type=ctype, timeout=600)
                if not api.ok(r):
                    warn(f"装不上：{api.msg(r)}")
            if meta.get("config"):
                pack["plugins"].append({"name": name, "config": meta["config"], "what": meta.get("what", "")})
            installed = {p.get("name") for p in (api.call("GET", "/plugins").get("data") or []) if isinstance(p, dict)}
            results.append((f"插件 {name}", name in installed))

    if want("plugin_config"):
        step("改插件设置")
        changed = []
        for p in pack["plugins"]:
            if p["name"] not in installed or not p["config"]:
                continue
            r = api.call("GET", f"/plugins/config?plugin_id={urllib.parse.quote(p['name'])}")
            conf = (r.get("data") or {}).get("config") if isinstance(r.get("data"), dict) else None
            if not isinstance(conf, dict):
                warn(f"{p['name']}：读不到设置")
                continue
            for k, v in p["config"].items():
                if get_path(conf, k) is None and "." in k and get_path(conf, k.rsplit(".", 1)[0]) is None:
                    warn(f"{p['name']}：设置项 {k} 不存在，可能插件更新改名了，跳过")
                    continue
                set_path(conf, k, fill(v, ctx))
            r = api.call("PUT", "/plugins/config", {"plugin_id": p["name"], "config": conf})
            say(f"  {p['name']}：{'完成' if api.ok(r) else '失败 ' + api.msg(r)}")
            if api.ok(r):
                changed.append(p["name"])

        for tool, perm in pack["tool_permissions"].items():
            r = api.call("PATCH", f"/tools/{urllib.parse.quote(tool, safe='')}/permission", {"permission": perm})
            if not api.ok(r):
                warn(f"工具 {tool} 权限没改成：{api.msg(r)}")
        say("  发空间、改签名只听主人的：已设置")

    if want("memes"):
        # ---------- 表情包 ----------
        step("下载表情包（从表情包插件的资源广场下，几百张图，要一会儿）")
        if "meme_manager" in installed:
            ext = "/plugins/extensions/meme_manager"
            # 表情包从 GitHub 下，经插件里设的加速站；加速站时好时坏，下不动就换下一个再试
            cur = meme_accelerator(api)
            accels = ([cur] if cur and cur not in GH_ACCELERATORS else []) + GH_ACCELERATORS
            ai, gh_warned = 0, False
            if cur != accels[0]:
                meme_accelerator(api, accels[0])
            api.call("POST", f"{ext}/community/index/fetch", {}, timeout=120)
            have = meme_pack_ids(api, ext)
            for mp in pack["meme_packs"]:
                if mp["id"] in have:
                    say(f"  {mp['id']}：已有")
                    results.append((f"表情包 {mp['id']}", True))
                    continue
                while True:
                    r = api.call("POST", f"{ext}/community/install",
                                 {"pack_id": mp["id"], "set_as_default": mp["default"]}, timeout=900)
                    ok = api.ok(r)
                    if ok or ai + 1 >= len(accels):
                        break
                    ai += 1
                    say(f"  {mp['id']}：没下下来（{api.msg(r)[:80]}），换个 GitHub 加速地址再试：{accels[ai] or '直连 GitHub'}")
                    if meme_accelerator(api, accels[ai]) is None:
                        break
                    api.call("POST", f"{ext}/community/index/fetch", {}, timeout=120)
                say(f"  {mp['id']}：{'完成' if ok else '失败 ' + api.msg(r)}")
                results.append((f"表情包 {mp['id']}", ok))
                if not ok and not gh_warned:
                    gh_warned = True
                    warn("几个加速地址都没下下来，多半是这会儿连不上 GitHub。过一阵再运行一次本工具；"
                         "或者在后台「插件 → meme_manager → 配置」里把 GitHub 加速地址换成你能用的，再运行一次。")
            # 默认规则写成「几套一起用」：meme_manager 的选择规则支持 pack_ids 列表，主套排第一
            use = [mp["id"] for mp in sorted(pack["meme_packs"], key=lambda m: not m.get("default"))
                   if mp.get("use", True)]
            ok = bool(use) and meme_use_together(api, ext, use)
            if not ok:
                # 规则接口用不了（比如插件版本太老）：退回只设主套为默认
                main = next((mp["id"] for mp in pack["meme_packs"] if mp.get("default")), None)
                if main:
                    ok = api.ok(api.call("POST", f"{ext}/packs/default", {"pack_id": main}))
                    say(f"  只把 {main} 设成默认：{'完成' if ok else '失败'}")
            results.append(("表情包默认用的几套", ok))
        else:
            results.append(("表情包", False))

    if want("memory"):
        # ---------- 记忆 ----------
        step("记忆")
        lm = "astrbot_plugin_livingmemory"
        mem_ok = False
        if memory == "0":
            say("  先不装。以后想装，再运行一次本工具选 1 或 2。")
        else:
            m = pack["memory"]
            if memory == "1":
                o = m["ollama"]
                if not ollama_ready(o["ollama_model"]):
                    warn("Ollama 没在运行或模型没下好，先跳过记忆。装好 Ollama 后再运行一次本工具。")
                    o = None
                conf = o and {"id": o["id"], "embedding_api_key": o["embedding_api_key"],
                              "embedding_api_base": o["embedding_api_base"], "embedding_model": o["embedding_model"]}
            else:
                s = m["siliconflow"]
                conf = {"id": s["id"], "embedding_api_key": sf_key, "embedding_api_base": s["embedding_api_base"],
                        "embedding_model": s["embedding_model"]}
            if conf and lm in installed:
                conf.update({"type": "openai_embedding", "provider": "openai", "provider_type": "embedding", "enable": True,
                             "embedding_dimensions": m["embedding_dimensions"], "embedding_dimensions_mode": "auto",
                             "timeout": 20, "proxy": ""})
                if upsert_provider(api, conf):
                    good, why = test_provider(api, conf["id"])
                    if not good:
                        warn(f"记忆模型测试没通过：{why}")
                    r = api.call("GET", f"/plugins/config?plugin_id={lm}")
                    lconf = (r.get("data") or {}).get("config")
                    if isinstance(lconf, dict):
                        set_path(lconf, "provider_settings.embedding_provider_id", conf["id"])
                        mem_ok = api.ok(api.call("PUT", "/plugins/config", {"plugin_id": lm, "config": lconf})) and good
                        changed.append(lm)
        if lm in installed:
            api.call("PATCH", "/plugins/enabled", {"plugin_id": lm, "enabled": mem_ok})
            say("  记忆已打开" if mem_ok else "  记忆插件先关着（没配好记忆模型时开着会报错）")
        if memory != "0":
            results.append(("记忆", mem_ok))

    if want("voice"):
        # ---------- 语音 ----------
        step("语音")
        v = pack["voice"]
        tts_id = ""
        if voice == "1":
            e = v["edge"]
            for pkg in e["pip"]:
                say(f"  安装 {pkg.split('==')[0]}…")
                r = api.call("POST", "/pip/install", {"package": pkg}, timeout=600)
                if not api.ok(r):
                    warn(f"装不上：{api.msg(r)}")
            conf = {"id": e["id"], "type": "edge_tts", "provider": "microsoft", "provider_type": "text_to_speech",
                    "enable": True, "voice": e["voice"], "rate": "+0%", "volume": "+0%", "pitch": "+0Hz",
                    "timeout": 20, "proxy": ""}
            tts_id = e["id"] if upsert_provider(api, conf) else ""
        elif voice == "2":
            mm = v["mimo"]
            ref = os.path.join(PACK, mm["reference_audio"])
            if not os.path.exists(ref):
                found = [f for f in sorted(glob.glob(os.path.join(PACK, "voice", "*"))) if f.lower().endswith((".mp3", ".wav", ".m4a"))]
                ref = found[0] if found else ref
            if not os.path.exists(ref):
                ref = ask("reference_audio", "配置包里没有参考音，给一段你想克隆的声音文件路径（mp3/wav，5–10 秒）").strip('"')
            ext = os.path.splitext(ref)[1].lower().lstrip(".") or "mp3"
            mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}.get(ext, "audio/mpeg")
            data_uri = f"data:{mime};base64," + base64.b64encode(open(ref, "rb").read()).decode()
            conf = {"id": mm["id"], "type": "mimo_tts_api", "provider": "mimo", "provider_type": "text_to_speech",
                    "enable": True, "api_key": mimo_key, "api_base": mm["api_base"], "model": mm["model"],
                    "mimo-tts-voice": data_uri, "mimo-tts-format": "wav", "mimo-tts-style-prompt": "",
                    "mimo-tts-dialect": "", "mimo-tts-seed-text": "", "timeout": 30, "proxy": ""}
            tts_id = mm["id"] if upsert_provider(api, conf) else ""
        elif voice == "3":
            d = v["doubao"]
            if d["plugin"] not in installed:
                warn(f"豆包语音要的插件 {d['plugin']} 没装上，先跳过语音。")
            else:
                conf = {"id": d["id"], "type": "seed_audio_tts", "provider": "volcengine", "provider_type": "text_to_speech",
                        "enable": True, "app_id": volc_app_id, "access_key": volc_token, "credentials_file": "",
                        "api_base": d["api_base"], "resource_id": d["resource_id"], "model": d["model"],
                        "seed_audio_persona": d["persona"], "seed_audio_prompt_template": d["prompt_template"],
                        "seed_audio_format": d["format"], "seed_audio_sample_rate": d["sample_rate"],
                        "seed_audio_max_attempts": d["max_attempts"],
                        "seed_audio_whole_reply": d.get("whole_reply", True),
                        "seed_audio_reply_probability": v["trigger_probability"], "timeout": d["timeout"]}
                say("  试念一句「你好呀」，豆包要十几秒…")
                tts_id = d["id"] if upsert_provider(api, conf) else ""
        elif voice == "4":
            bz = v["breeze"]
            try:
                with urllib.request.urlopen(bz["api_base"] + "/voices", timeout=10) as r:
                    have = [x.get("id") for x in json.loads(r.read() or b"[]")]
            except Exception:
                have = None
            if have is None:
                warn("连不上本机的 Breeze。先双击桌面的「启动Breeze语音」，没装的话先双击「装Breeze语音.bat」。")
            elif bz["voice"] not in have:
                warn(f"Breeze 里还没有叫 {bz['voice']} 的声音，先双击「做声音.bat」做一个。")
            else:
                conf = {"id": bz["id"], "type": "openai_tts_api", "provider": "openai", "provider_type": "text_to_speech",
                        "enable": True, "api_key": "unused", "api_base": bz["api_base"], "model": "tts-1",
                        "openai-tts-voice": bz["voice"], "timeout": 180, "proxy": ""}
                say("  试念一句，Breeze 要几秒…")
                tts_id = bz["id"] if upsert_provider(api, conf) else ""
        if voice != "3":
            # 以前选过豆包又换了别的：豆包插件只要看到它的提供商开着就会掷骰子发整条语音，得关掉
            old = provider_config(api, v["doubao"]["id"])
            if old and old.get("enable"):
                upsert_provider(api, {**old, "enable": False})
                say("  之前开着的豆包语音已关掉")
        if tts_id:
            good, why = test_provider(api, tts_id)
            if good:
                say(f"  语音已打开，大约三条回复里有一条会发语音")
                if voice == "3":
                    say("  豆包是整条回复合成一条语音，要等 15–35 秒才到")
            else:
                # 测不通就先关着：开着的话她每发一次语音都会报错
                warn(f"语音测试没通过：{why}")
                if voice == "1" and re.search(r"WinError 2|找不到指定的文件|ffmpeg", why, re.I):
                    warn("Edge 语音要用 ffmpeg 转格式，这台电脑上多半没装。在 PowerShell 里运行 "
                         "winget install Gyan.FFmpeg 装上，重启 AstrBot，再运行一次本工具。")
                conf["enable"] = False
                upsert_provider(api, conf)
                tts_id = ""
                say("  语音先关着，处理好再运行一次本工具")
            results.append(("语音", good))
        elif voice != "0":
            results.append(("语音", False))
        else:
            say("  先不开")

    if want("base"):
        # ---------- 全局设置 ----------
        step("改全局设置（分段回复、认得主人、默认人格等）")
        r = api.call("GET", "/config-profiles/default")
        cfg = r["data"]["config"]
        for k, val in pack["base"].items():
            set_path(cfg, k, fill(val, ctx))
        ps = cfg.setdefault("platform_settings", {})
        if ps.get("enable_id_white_list"):
            wl = ps.setdefault("id_whitelist", [])
            if session not in wl and owner not in wl:
                wl.append(session)
        admins = cfg.setdefault("admins_id", [])
        if owner not in admins:
            admins.append(owner)
        tts = cfg.setdefault("provider_tts_settings", {})
        if tts_id and voice == "3" and v["doubao"].get("whole_reply", True):
            # 整条语音由豆包插件掷骰子、一次合成；AstrBot 自己的按段语音要关掉，不然会再按段转一遍
            tts.update({"enable": False, "provider_id": tts_id, "trigger_probability": v["trigger_probability"]})
        elif tts_id:
            tts.update({"enable": True, "provider_id": tts_id, "trigger_probability": v["trigger_probability"]})
        else:
            tts["enable"] = False
        r = api.call("PUT", "/config-profiles/default", cfg)
        results.append(("全局设置", api.ok(r)))
        say("  完成" if api.ok(r) else f"  ！失败：{api.msg(r)}")

    if want("reload"):
        step("重新加载插件")
        for name in dict.fromkeys(changed):
            if name == lm and not mem_ok:
                continue
            api.call("POST", "/plugins/reload", {"plugin_id": name}, timeout=180)
        say("  完成")

    say("\n===== 结果 =====")
    for name, ok in results:
        say(f"  {'✔' if ok else '✘'} {name}")
    bad = [n for n, ok in results if not ok]
    if bad:
        say("\n打叉的可以按提示处理后，再运行一次本工具（已经装好的会自动跳过）。")
    say("\n去 QQ 上用大号给她发一句「在吗」试试。之前聊过的话，先发 /reset 清掉旧对话，新人格才会生效。")


def meme_payload(r, key):
    """meme_manager 的接口有时直接返回字段，有时包在 data 里。"""
    if key in r:
        return r[key]
    data = r.get("data")
    return data.get(key) if isinstance(data, dict) else None


def meme_pack_ids(api, ext):
    packs = meme_payload(api.call("GET", f"{ext}/packs"), "packs") or []
    return {str(p.get("pack_id") or p.get("id")) for p in packs if isinstance(p, dict)}


def meme_accelerator(api, url=None):
    """不给 url：读表情包插件现在的 GitHub 加速地址（读不到返回 None）。给 url：改成它，成功返回 url，失败返回 None。"""
    r = api.call("GET", "/plugins/config?plugin_id=meme_manager")
    conf = (r.get("data") or {}).get("config") if isinstance(r.get("data"), dict) else None
    if not isinstance(conf, dict):
        return None
    if url is None:
        return str(get_path(conf, "community.github_accelerator_url") or "")
    set_path(conf, "community.github_accelerator_url", url)
    return url if api.ok(api.call("PUT", "/plugins/config", {"plugin_id": "meme_manager", "config": conf})) else None


def meme_use_together(api, ext, wanted):
    """把 meme_manager 的 default 选择规则设成 wanted 这几套一起用（已经是了就不动）。"""
    have = meme_pack_ids(api, ext)
    ids = [p for p in wanted if p in have]
    missing = [p for p in wanted if p not in have]
    if missing:
        warn(f"这几套表情包没装上，先不算进去：{'、'.join(missing)}")
    if not ids:
        return False
    r = api.call("GET", f"{ext}/settings/rules")
    rules = [x for x in (meme_payload(r, "rules") or []) if isinstance(x, dict)]
    if not api.ok(r):
        warn(f"读不到表情包规则：{api.msg(r)}")
        return False
    cur = next((x for x in reversed(rules) if x.get("scope") == "default"), {})
    cur_ids = cur.get("pack_ids") or ([cur["pack_id"]] if cur.get("pack_id") else [])
    if cur_ids == ids:
        say(f"  默认已经是这 {len(ids)} 套：{'、'.join(ids)}")
        return True
    keep = [x for x in rules if x.get("scope") != "default"]
    rule = {"id": cur.get("id") or "default", "scope": "default", "pack_id": ids[0], "pack_ids": ids}
    r = api.call("POST", f"{ext}/settings/rules", {"rules": keep + [rule]})
    if not api.ok(r):
        warn(f"表情包规则没改成：{api.msg(r)}")
        return False
    say(f"  默认改成用这 {len(ids)} 套：{'、'.join(ids)}")
    return True


def ollama_ready(model):
    def tags():
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
                return [m.get("name", "") for m in json.load(r).get("models", [])]
        except Exception:
            return None
    names = tags()
    if names is None:
        warn("连不上本机 Ollama（127.0.0.1:11434）。先装 Ollama 并打开它。")
        return False
    if any(n.split(":")[0] == model for n in names):
        say(f"  Ollama 里已经有 {model}")
        return True
    exe = shutil.which("ollama") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    if not os.path.exists(exe) and not shutil.which("ollama"):
        warn("找不到 ollama 命令。")
        return False
    say(f"  正在下载向量模型 {model}（约 1.2 GB）…")
    subprocess.call([exe, "pull", model])
    names = tags() or []
    return any(n.split(":")[0] == model for n in names)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        say("\n已取消")
