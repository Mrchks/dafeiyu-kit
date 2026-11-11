"""常用设置、QQ 群列表、备份还原、诊断、余额。"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import sqlite3
import time
import urllib.parse
import zipfile
from pathlib import Path

import services
from core import (KIT, astrbot_data, backups_dir, cmd_config_path, http_json, load_state, log, napcat_cfg_dir,
                  plugin_cfg_dir, read_json, tail_lines, update_state, write_json)

# ---------------------------------------------------------------- NapCat 的 OneBot HTTP 接口（拿群列表、好友列表）


def ensure_onebot_http() -> bool:
    """确保 NapCat 开了一个只给本机用的 HTTP 接口。返回 True 表示刚加上（需要重启 NapCat）。"""
    s = load_state()
    cfgd = napcat_cfg_dir()
    qq = s["bot_qq"] or services.napcat_webui()["qq"]
    if not cfgd or not qq:
        return False
    p = cfgd / f"onebot11_{qq}.json"
    try:
        c = read_json(p)
    except Exception:
        return False
    tok = s.get("onebot_http_token") or secrets.token_hex(16)
    port = s.get("onebot_http_port") or 3010
    net = c.setdefault("network", {})
    servers = net.setdefault("httpServers", [])
    for sv in servers:
        if sv.get("name") == "dafeiyu-console":
            if sv.get("enable") and sv.get("token") == tok and int(sv.get("port", 0)) == port:
                update_state(onebot_http_token=tok, onebot_http_port=port)
                return False
            servers.remove(sv)
            break
    servers.append({"name": "dafeiyu-console", "enable": True, "port": port, "host": "127.0.0.1",
                    "enableCors": False, "enableWebsocket": False, "messagePostFormat": "array",
                    "token": tok, "debug": False})
    write_json(p, c)
    update_state(onebot_http_token=tok, onebot_http_port=port)
    log("已给 NapCat 加上本机 HTTP 接口（群列表用）")
    return True


def onebot(action: str, params=None):
    s = load_state()
    d = http_json(f"http://127.0.0.1:{s['onebot_http_port']}/{action}", params or {},
                  headers={"Authorization": f"Bearer {s['onebot_http_token']}"}, timeout=10)
    if d.get("status") != "ok":
        raise RuntimeError(d.get("message") or d.get("wording") or "NapCat 接口出错")
    return d.get("data")


def group_list() -> list[dict]:
    try:
        rows = onebot("get_group_list") or []
    except Exception as e:
        raise RuntimeError(f"无法读取群列表（{e}）。请在总览页点击 NapCat 的「重启」后重试。")
    return [{"id": str(g.get("group_id")), "name": g.get("group_name", ""), "count": g.get("member_count")}
            for g in rows]


# ---------------------------------------------------------------- 群规则：静音、主人吩咐
# 文件由「大肥鱼通用能力」插件读写，插件按修改时间缓存，这边改了她下一条消息就生效，不用重启。

def _group_rules_file() -> Path:
    d = astrbot_data()
    if not d:
        raise RuntimeError("AstrBot 尚未安装完成")
    p = d / "plugin_data" / "astrbot_plugin_dafeiyu_common" / "group_rules.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _group_rules_load() -> dict:
    p = _group_rules_file()
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        d = {}
    return {"mutes": dict(d.get("mutes") or {}), "notes": dict(d.get("notes") or {})}


def _group_rules_save(d: dict) -> None:
    p = _group_rules_file()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def group_rules() -> dict:
    d = _group_rules_load()
    now = time.time()
    mutes = {}
    for gid, m in d["mutes"].items():
        until = float(m.get("until") or 0)
        if until and until <= now:
            continue
        mutes[gid] = {"until": until, "left_min": int((until - now) / 60 + 0.5) if until else None}
    return {"mutes": mutes, "notes": d["notes"]}


def group_mute(gid: str, minutes: float) -> None:
    """minutes < 0 解除；0 一直静音；> 0 静音多少分钟。"""
    d = _group_rules_load()
    gid = str(gid)
    if minutes < 0:
        d["mutes"].pop(gid, None)
    else:
        d["mutes"][gid] = {"until": time.time() + minutes * 60 if minutes > 0 else 0, "at": time.time(), "name": ""}
    _group_rules_save(d)


def group_note_add(gid: str, text: str) -> int:
    text = (text or "").strip()
    if not text:
        raise RuntimeError("吩咐是空的")
    d = _group_rules_load()
    lst = d["notes"].setdefault(str(gid), [])
    nid = max([int(n.get("id") or 0) for n in lst] + [0]) + 1
    lst.append({"id": nid, "text": text, "at": time.time(), "name": "所有群" if gid == "*" else ""})
    _group_rules_save(d)
    return nid


def group_note_del(gid: str, nid: int) -> None:
    d = _group_rules_load()
    lst = [n for n in d["notes"].get(str(gid)) or [] if int(n.get("id") or 0) != int(nid)]
    if lst:
        d["notes"][str(gid)] = lst
    else:
        d["notes"].pop(str(gid), None)
    _group_rules_save(d)


def group_note_restore(gid: str, note: dict) -> None:
    """「撤销删除」：原样放回去。"""
    d = _group_rules_load()
    lst = d["notes"].setdefault(str(gid), [])
    if not any(int(n.get("id") or 0) == int(note.get("id") or 0) for n in lst):
        lst.append(note)
        lst.sort(key=lambda n: int(n.get("id") or 0))
    _group_rules_save(d)


# ---------------------------------------------------------------- 常用设置（改文件，需要 AstrBot 停着）

def personas_from_db() -> list[str]:
    try:
        uri = "file:" + urllib.parse.quote(str(astrbot_data() / "data_v4.db").replace("\\", "/")) + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            return [r[0] for r in db.execute("select persona_id from personas order by sort_order, id")]
        finally:
            db.close()
    except Exception:
        return []


def get_settings() -> dict:
    c = read_json(cmd_config_path())
    name = services.bot_name()
    wl = c.get("platform_settings", {}).get("id_whitelist", [])
    friends, groups, others = [], [], []
    for s in wl:
        parts = s.split(":")
        if len(parts) == 3 and parts[0] == name and parts[1] == "FriendMessage":
            friends.append(parts[2])
        elif len(parts) == 3 and parts[0] == name and parts[1] == "GroupMessage":
            groups.append(parts[2])
        else:
            others.append(s)
    pc_dir = plugin_cfg_dir()

    def pj(name):
        try:
            return read_json(pc_dir / name)
        except Exception:
            return {}
    gcp = pj("astrbot_plugin_group_chat_plus_config.json")
    fs = pj("astrbot_plugin_proactive_chat_config.json").get("friend_settings", {})
    voice = None
    for p in c.get("provider", []):
        if p.get("type") == "seed_audio_tts":
            voice = p.get("seed_audio_reply_probability")
    if voice is None and c.get("provider_tts_settings", {}).get("provider_id"):
        voice = c.get("provider_tts_settings", {}).get("trigger_probability")
    meme = pj("meme_manager_config.json").get("generation", {}).get("emotion", {}).get("probability")
    return {
        "bot_name": name,
        "friends": friends, "groups": groups, "others": others,
        "admins": [a for a in c.get("admins_id", []) if a != "astrbot"],
        "persona": c.get("agent_runner", {}).get("config", {}).get("persona", {}).get("persona_id", ""),
        "personas": personas_from_db(),
        "group_reply_prob": gcp.get("initial_probability"),
        "group_proactive": gcp.get("enable_proactive_chat"),
        "keywords": gcp.get("trigger_keywords", []),
        "private_proactive": fs.get("enable"),
        "proactive_targets": [x.split(":")[-1] for x in fs.get("session_list", [])],
        "voice_prob": voice,
        "meme_prob": meme,
        "group_max_replies": gcp.get("reply_density_max_replies"),
        "group_ignore_at_others": gcp.get("enable_ignore_at_others"),
        "kb_names": c.get("kb_names", []),
        "kb_agentic": bool(c.get("kb_agentic_mode", False)),
        "group_quote": pj("astrbot_plugin_dafeiyu_at_config.json").get("group_quote_reply", True)
        if (pc_dir / "astrbot_plugin_dafeiyu_at_config.json").exists() or plugin_installed("astrbot_plugin_dafeiyu_at") else None,
        "group_quote_at": pj("astrbot_plugin_dafeiyu_at_config.json").get("group_quote_at", True)
        if (pc_dir / "astrbot_plugin_dafeiyu_at_config.json").exists() or plugin_installed("astrbot_plugin_dafeiyu_at") else None,
        **_kb_plugin_settings(pj("astrbot_plugin_dafeiyu_kb_config.json")),
    }


def plugin_installed(name: str) -> bool:
    d = astrbot_data()
    return bool(d and (d / "plugins" / name).exists())


def _kb_plugin_settings(k: dict) -> dict:
    if not plugin_installed("astrbot_plugin_dafeiyu_kb"):
        return {"ask_enable": None, "ask_limit": None, "ask_owner": ""}
    owner = str(k.get("owner_session") or "")
    return {"ask_enable": bool(k.get("ask_owner_enable", True)), "ask_limit": int(k.get("ask_daily_limit", 5) or 0),
            "ask_owner": owner.split(":")[-1] if owner else ""}


# ---------------------------------------------------------------- 知识库资料文件夹

KB_ROOT = KIT / "知识库"
KB_EXTS = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".epub"}


def kb_folder(kb_name: str, create: bool = True) -> Path:
    import re
    f = KB_ROOT / (re.sub(r'[\\/:*?"<>|]+', "_", kb_name).strip() or "知识库")
    if create:
        f.mkdir(parents=True, exist_ok=True)
    return f


def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha1(b).hexdigest()


def kb_record(kb_id: str, fname: str, data: bytes | None) -> None:
    """记下这份资料最后一次由控制台导入时的内容指纹和时间。"""
    s = load_state()
    m = s.get("kb_sync") or {}
    t = s.get("kb_sync_at") or {}
    d, dt_ = m.setdefault(kb_id, {}), t.setdefault(kb_id, {})
    if data is None:
        d.pop(fname, None)
        dt_.pop(fname, None)
    else:
        d[fname] = _sha(data)
        dt_[fname] = time.time()
    update_state(kb_sync=m, kb_sync_at=t)


def _doc_time(d: dict) -> float:
    """知识库里文档的创建时间（AstrBot 存的是 UTC），读不出来返回 0。"""
    try:
        v = dt.datetime.fromisoformat(str(d.get("created_at") or "").replace("Z", "")[:26])
        return v.replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def kb_save_copy(kb_id: str, kb_name: str, fname: str, data: bytes) -> None:
    """控制台上传的资料，同时在资料文件夹里留一份，文件夹就是「原稿」。"""
    try:
        (kb_folder(kb_name) / Path(fname).name).write_bytes(data)
        kb_record(kb_id, Path(fname).name, data)
    except Exception as e:
        log(f"知识库：存副本失败 {fname}：{e}")


def kb_remove_copy(kb_id: str, kb_name: str, fname: str) -> None:
    """控制台里删了资料：文件夹里那份挪进「已删除」，免得下次同步又导回去。"""
    f = kb_folder(kb_name) / Path(fname).name
    if f.exists():
        trash = f.parent / "已删除"
        trash.mkdir(exist_ok=True)
        dst = trash / f.name
        if dst.exists():
            dst = trash / f"{f.stem}-{dt.datetime.now():%Y%m%d%H%M%S}{f.suffix}"
        shutil.move(str(f), str(dst))
    kb_record(kb_id, Path(fname).name, None)


def kb_sync(kb_id: str, kb_name: str) -> str:
    """把资料文件夹和知识库对齐：新的、改过的重新导入；文件夹里删掉的（以前同步过的）从知识库删掉。"""
    import astrbot_api
    folder = kb_folder(kb_name)
    st = load_state()
    tracked = dict((st.get("kb_sync") or {}).get(kb_id, {}))
    tracked_at = dict((st.get("kb_sync_at") or {}).get(kb_id, {}))
    docs = astrbot_api.kb_docs(kb_id)
    by_name: dict[str, list[str]] = {}
    newest: dict[str, float] = {}
    for d in docs:
        n = d.get("doc_name") or ""
        by_name.setdefault(n, []).append(d.get("doc_id"))
        newest[n] = max(newest.get(n, 0.0), _doc_time(d))
    files = {f.name: f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in KB_EXTS and not f.name.startswith("~$")}
    changed = removed = same = 0
    for name, f in sorted(files.items()):
        data = f.read_bytes()
        # 文件没变、知识库里也还是控制台上次导入的那份，才跳过。
        # 「大肥鱼自己记知识库」会直接改「主人补充.md」并重新导入；之后文件又被改回上次同步的样子时，
        # 光比文件指纹会误以为没变，知识库里留着已经删掉的内容。所以知识库里的文档比上次导入新，也要重新导入。
        by_other = name in tracked_at and newest.get(name, 0.0) > tracked_at[name] + 120
        if tracked.get(name) == _sha(data) and name in by_name and not by_other:
            same += 1
            continue
        services.job_line(f"导入 {name}")
        for did in by_name.get(name, []):
            astrbot_api.kb_doc_delete(kb_id, did)
        tid = astrbot_api.kb_upload(kb_id, name, data)
        for _ in range(300):
            time.sleep(2)
            t = astrbot_api.kb_task(tid)
            if t.get("status") == "completed":
                break
            if t.get("status") == "failed":
                raise RuntimeError(f"{name} 导入失败：{t.get('error')}")
        kb_record(kb_id, name, data)
        changed += 1
    for name in list(tracked):
        if name not in files:
            services.job_line(f"文件夹中已不存在，从知识库中删除 {name}")
            for did in by_name.get(name, []):
                astrbot_api.kb_doc_delete(kb_id, did)
            kb_record(kb_id, name, None)
            removed += 1
    msg = f"同步完成：更新 {changed} 个，删掉 {removed} 个，没变 {same} 个"
    log(f"知识库「{kb_name}」{msg}")
    return msg


def embedding_providers() -> list[dict]:
    c = read_json(cmd_config_path())
    out = []
    for p in c.get("provider", []):
        if p.get("provider_type") == "embedding" or "embedding" in str(p.get("type", "")):
            out.append({"id": p.get("id"), "enabled": p.get("enable", True)})
    return out


def _digits(xs) -> list[str]:
    out = []
    for x in xs or []:
        x = str(x).strip()
        if x.isdigit() and x not in out:
            out.append(x)
    return out


def apply_settings(new: dict) -> str:
    p = cmd_config_path()
    c = read_json(p)
    name = services.bot_name()
    ps = c.setdefault("platform_settings", {})
    if "friends" in new or "groups" in new:
        wl = [f"{name}:FriendMessage:{q}" for q in _digits(new.get("friends"))]
        wl += [f"{name}:GroupMessage:{g}" for g in _digits(new.get("groups"))]
        wl += [s for s in new.get("others", []) if isinstance(s, str) and s.strip()]
        ps["id_whitelist"] = wl
    admins = _digits(new.get("admins")) if "admins" in new else []
    if "admins" in new:
        c["admins_id"] = (["astrbot"] if "astrbot" in c.get("admins_id", []) else []) + admins
    if isinstance(new.get("kb_names"), list):
        c["kb_names"] = [k for k in new["kb_names"] if isinstance(k, str) and k.strip()]
    if new.get("kb_agentic") is not None:
        c["kb_agentic_mode"] = bool(new["kb_agentic"])
    if new.get("persona"):
        c.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("persona", {})["persona_id"] = new["persona"]
    if new.get("voice_prob") is not None:
        v = max(0.0, min(1.0, float(new["voice_prob"])))
        for pv in c.get("provider", []):
            if pv.get("type") == "seed_audio_tts":
                pv["seed_audio_reply_probability"] = v
        c.setdefault("provider_tts_settings", {})["trigger_probability"] = v
    write_json(p, c)
    if admins:
        update_state(owners=admins)

    pc = plugin_cfg_dir()
    gp = pc / "astrbot_plugin_group_chat_plus_config.json"
    if gp.exists():
        g = read_json(gp)
        if new.get("group_reply_prob") is not None:
            g["initial_probability"] = round(max(0.0, min(1.0, float(new["group_reply_prob"]))), 3)
            g["frequency_min_probability"] = min(g.get("frequency_min_probability", 0.05), g["initial_probability"])
        if new.get("group_proactive") is not None:
            g["enable_proactive_chat"] = bool(new["group_proactive"])
        if new.get("group_max_replies") is not None:
            g["reply_density_max_replies"] = int(max(1, min(30, int(new["group_max_replies"]))))
        if new.get("group_ignore_at_others") is not None:
            g["enable_ignore_at_others"] = bool(new["group_ignore_at_others"])
        if isinstance(new.get("keywords"), list):
            g["trigger_keywords"] = [k.strip() for k in new["keywords"] if isinstance(k, str) and k.strip()]
        write_json(gp, g)
    pp = pc / "astrbot_plugin_proactive_chat_config.json"
    if pp.exists():
        pcfg = read_json(pp)
        fs = pcfg.setdefault("friend_settings", {})
        if new.get("private_proactive") is not None:
            fs["enable"] = bool(new["private_proactive"])
        if isinstance(new.get("proactive_targets"), list):
            fs["session_list"] = [f"{name}:FriendMessage:{q}" for q in _digits(new["proactive_targets"])]
        write_json(pp, pcfg)
    if new.get("group_quote") is not None or new.get("group_quote_at") is not None:
        ap = pc / "astrbot_plugin_dafeiyu_at_config.json"
        a = read_json(ap) if ap.exists() else {}
        if new.get("group_quote") is not None:
            a["group_quote_reply"] = bool(new["group_quote"])
        if new.get("group_quote_at") is not None:
            a["group_quote_at"] = bool(new["group_quote_at"])
        write_json(ap, a)
    if plugin_installed("astrbot_plugin_dafeiyu_kb"):
        kp = pc / "astrbot_plugin_dafeiyu_kb_config.json"
        k = read_json(kp) if kp.exists() else {}
        k["kb_root"] = str(KB_ROOT)
        if new.get("ask_enable") is not None:
            k["ask_owner_enable"] = bool(new["ask_enable"])
        if new.get("ask_limit") is not None:
            k["ask_daily_limit"] = max(0, int(new["ask_limit"]))
        if new.get("ask_owner"):
            k["owner_session"] = f"{name}:FriendMessage:{''.join(ch for ch in str(new['ask_owner']) if ch.isdigit())}"
        write_json(kp, k)
    mp = pc / "meme_manager_config.json"
    if new.get("meme_prob") is not None and mp.exists():
        m = read_json(mp)
        m.setdefault("generation", {}).setdefault("emotion", {})["probability"] = int(max(0, min(100, float(new["meme_prob"]))))
        write_json(mp, m)
    log("常用设置已保存")
    return "设置已保存，AstrBot 已重启"


# ---------------------------------------------------------------- 备份与还原

BACKUP_SKIP = {"site-packages", "temp", "logs", "dist"}


def do_backup(include_memes: bool, tag: str = "") -> str:
    data = astrbot_data()
    name = f"dafeiyu-{dt.datetime.now():%Y%m%d-%H%M%S}{('-' + tag) if tag else ''}.zip"
    path = backups_dir() / name
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for root, dirs, files in os.walk(data):
            rel = Path(root).relative_to(data)
            if rel.parts and rel.parts[0] in BACKUP_SKIP:
                dirs[:] = []
                continue
            if not include_memes and rel.parts[:2] == ("plugin_data", "meme_manager"):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".tmp", ".pyc", ".part")):
                    continue
                z.write(Path(root) / f, str(Path("data") / rel / f))
                n += 1
        cfg = napcat_cfg_dir()
        if cfg and cfg.exists():
            for fp in cfg.glob("*.json"):
                z.write(fp, str(Path("napcat_config") / fp.name))
        st = KIT / "state.json"
        if st.exists():
            z.write(st, "extra/state.json")
        z.writestr("README.txt", f"大肥鱼整合包备份\n创建时间 {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n包含表情包：{include_memes}\n"
                                 "data = AstrBot 的 data 文件夹；napcat_config = NapCat 配置。\n")
    log(f"备份完成 {name}（{n} 个文件）")
    return f"备份完成：{name}"


def list_backups() -> list[dict]:
    out = []
    for p in sorted(backups_dir().glob("*.zip"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "time": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def do_restore(name: str) -> str:
    src = backups_dir() / Path(name).name
    if not src.exists():
        raise RuntimeError("找不到该备份")
    do_backup(False, "before-restore")
    data = astrbot_data()
    with zipfile.ZipFile(src) as z:
        for m in z.infolist():
            parts = Path(m.filename).parts
            if len(parts) < 2 or parts[0] != "data" or ".." in parts:
                continue
            target = data.joinpath(*parts[1:])
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    log(f"已还原 {src.name}")
    return f"已还原到 {src.name}（还原前的状态已另存为 before-restore 备份）"


# ---------------------------------------------------------------- DeepSeek 余额

_bal = {"at": 0.0, "data": None}


def deepseek_key() -> str | None:
    try:
        for s in read_json(cmd_config_path()).get("provider_sources", []):
            if "deepseek" in (s.get("api_base", "") + s.get("id", "")).lower():
                k = s.get("key")
                return k[0] if isinstance(k, list) and k else k
    except Exception:
        pass
    return None


def check_deepseek(key: str) -> dict:
    try:
        d = http_json("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=12)
        info = (d.get("balance_infos") or [{}])[0]
        return {"ok": bool(d.get("is_available")), "balance": info.get("total_balance"),
                "currency": info.get("currency", "CNY")}
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            msg = "key 不对（401）"
        return {"ok": False, "error": msg[:160]}


def deepseek_balance(force=False) -> dict:
    if not force and time.time() - _bal["at"] < 600 and _bal["data"] is not None:
        return _bal["data"]
    k = deepseek_key()
    res = check_deepseek(k) if k else {"ok": False, "error": "没找到 DeepSeek key"}
    _bal.update(at=time.time(), data=res)
    if res.get("balance") is not None:
        try:
            if float(res["balance"]) < 1:
                services.notify("DeepSeek 余额不足", f"余额仅剩 ¥{res['balance']}，用尽后 bot 将无法回复消息。")
        except ValueError:
            pass
    return res


# ---------------------------------------------------------------- 诊断：她为什么没回

def diagnose() -> list[dict]:
    """返回检查项：[{ok, title, detail, fix, actions:[{key,label}]}]。actions 是可以直接点的按钮，key 和问题卡的一致。"""
    out = []
    A = lambda *xs: [{"key": k, "label": l} for k, l in xs]
    st = services.status()
    nc, ab = st["napcat"], st["astrbot"]
    out.append({"ok": ab["running"], "title": "AstrBot 在运行",
                "detail": f"端口 {ab['port']}" if ab["running"] else "AstrBot 未运行，bot 不会回复任何消息。",
                "fix": "" if ab["running"] else "点击下方的「启动 AstrBot」，一至两分钟后重新诊断。",
                "actions": [] if ab["running"] else A(("start_astrbot", "启动 AstrBot"))})
    lb = load_state().get("last_boot") or {}
    if lb.get("load", 0) > 40:
        out.append({"ok": False, "warn": True, "title": "AstrBot 启动偏慢",
                    "detail": f"上次启动共用时 {lb.get('total', 0):.0f} 秒，其中加载程序用时 {lb.get('load', 0):.0f} 秒（正常为十余秒）。不影响 bot 回复消息，只是启动较慢。",
                    "fix": "点击「启动耗时分析」，查看耗时较长的环节和处理方法。",
                    "actions": A(("profile_astrbot", "启动耗时分析"))})
    procs = services.astrbot_procs()
    if len(procs) > 1:
        out.append({"ok": False, "title": "只有一个 AstrBot 在运行", "detail": f"当前有 {len(procs)} 个 AstrBot 进程，可能导致重复回复。",
                    "fix": "点击「重启 AstrBot」，控制台会关闭多余的进程后重新启动一个。",
                    "actions": A(("restart_astrbot", "重启 AstrBot"))})
    if services.astrbot_needs_restart(load_state(), ab):
        out.append({"ok": False, "warn": True, "title": "AstrBot 仍在使用更新前的设置",
                    "detail": "整合包更新后，部分改动需要重启一次 AstrBot 才能生效（例如在控制台中打开插件页面）。",
                    "fix": "点击「重启 AstrBot」。重启期间 bot 约一分钟不回复消息。",
                    "actions": A(("restart_astrbot", "重启 AstrBot"))})
    nc_ok = nc["state"] == "online"
    state_txt = {"online": "在线", "need_qr": "登录过期了，需要用手机 QQ 重新扫码", "connecting": "启动了但没连上 AstrBot",
                 "stopped": "没在跑"}
    if nc_ok:
        nc_fix, nc_act = "", []
    elif nc["state"] == "need_qr":
        nc_fix, nc_act = "点击「扫码登录」，使用小号所在手机的 QQ 扫描二维码。", A(("qr", "扫码登录"))
    elif nc["state"] == "connecting" and not ab["running"]:
        nc_fix, nc_act = "AstrBot 未运行，NapCat 无法与其连接。请先启动 AstrBot。", A(("start_astrbot", "启动 AstrBot"))
    else:
        nc_fix, nc_act = "点击「重启 NapCat」；仍未解决时，重新扫码登录。", A(("restart_napcat", "重启 NapCat"), ("qr", "扫码登录"))
    out.append({"ok": nc_ok, "title": "QQ 小号在线并已连接 AstrBot", "detail": state_txt.get(nc["state"], nc["state"]),
                "fix": nc_fix, "actions": nc_act})
    evs = [e for e in (load_state().get("offline_events") or []) if time.time() - (e.get("at") or 0) < 86400]
    if evs:
        last = evs[-1]
        kicked = any("挤下线" in str(e.get("reason", "")) for e in evs)
        when = time.strftime("%H:%M", time.localtime(last.get("at") or 0))
        out.append({"ok": False, "warn": True, "title": "最近 24 小时没有掉线",
                    "detail": f"掉线 {len(evs)} 次，最近一次在 {when}：{str(last.get('reason', '')).split('（')[0]}。",
                    "fix": ("bot 使用的账号在其他设备登录时会被挤下线：请不要在任何电脑版 QQ 上登录该账号（手机可以），"
                            "并在任务管理器中只保留由 NapCat 启动的 QQ 进程，然后重新扫码。" if kicked else
                            "点击「查看日志」查看掉线前后的记录；反复掉线时，可生成问题报告并反馈给作者。"),
                    "actions": A(("qr", "扫码登录"), ("report", "生成问题报告")) if kicked else A(("logs", "查看日志"), ("report", "生成问题报告"))})
    config = read_json(cmd_config_path())
    model_id = config.get("agent_runner", {}).get("config", {}).get("model", {}).get("provider_id", "")
    model = next((p for p in config.get("provider", []) if p.get("id") == model_id), {})
    source = next((p for p in config.get("provider_sources", []) if p.get("id") == model.get("provider_source_id")), {})
    if "api.deepseek.com" in source.get("api_base", ""):
        bal = deepseek_balance(force=True)
        bal_ok = bool(bal.get("ok")) and float(bal.get("balance") or 0) > 0
        if bal_ok:
            b_fix, b_act = "", []
        elif bal.get("ok"):
            b_fix, b_act = "余额已用尽，bot 将无法回复消息。请点击「前往充值」，充值后几分钟内恢复。", A(("topup", "前往充值"))
        else:
            b_fix, b_act = ("无法查询余额，通常是密钥填写有误或已被删除。请在 DeepSeek 密钥页新建并复制密钥，再到「常用设置」中更换。",
                            A(("deepseek_key", "打开密钥页"), ("settings", "常用设置")))
        out.append({"ok": bal_ok, "title": "DeepSeek 可用且余额充足",
                    "detail": f"余额 ¥{bal.get('balance')}" if bal.get("balance") is not None else bal.get("error", ""),
                    "fix": b_fix, "actions": b_act})
    else:
        ready = bool(model.get("enable", True) and source.get("key") and source.get("enable", True))
        out.append({"ok": ready, "title": "默认模型已配置", "detail": model.get("model") or "尚未选择默认聊天模型",
                    "fix": "可在「模型与 API」验证模型权限和网络。", "actions": A(("models", "模型与 API"))})
    # 日志里最近的问题
    lines = tail_lines(services.astrbot_log_file(), 400)
    blocked = [l for l in lines if "not in the session allowlist" in l]
    if blocked:
        import re
        ids = sorted({m.group(1) for l in blocked[-30:] for m in [re.search(r"Session ID (\S+) is not", l)] if m})
        out.append({"ok": False, "warn": True, "title": "有消息被白名单拦截",
                    "detail": "以下会话不在白名单中：" + "、".join(ids[-6:]),
                    "fix": "需要 bot 回复这些群或用户时，请在「常用设置」中将群号或 QQ 号加入白名单；无需回复时可忽略。",
                    "actions": A(("settings", "常用设置"))})
    errs = [l for l in lines[-200:] if "[ERRO" in l or "Traceback" in l or "[ERROR" in l]
    if errs:
        import troubles
        t = troubles.explain(errs[-1], errs[-20:], "运行中")
        known = t.get("code") != "unknown"
        out.append({"ok": False, "title": "最近没有报错",
                    "detail": (t["title"] + "。" + t["cause"]) if known else "日志中近期有报错：" + errs[-1][-160:],
                    "fix": "；".join(t["steps"][:2]) if known else "点击「查看日志」查看完整报错；需要协助时，可生成问题报告并反馈给作者。",
                    "actions": ([a for a in t["actions"] if a["key"] not in ("retry", "env", "report", "logs")][:2] if known else []) + A(("logs", "查看日志"), ("report", "生成问题报告"))})
    else:
        out.append({"ok": True, "title": "最近没有报错", "detail": "日志最近 200 行中没有报错。", "fix": "", "actions": []})
    return out
