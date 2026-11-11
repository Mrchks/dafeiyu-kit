"""调用 AstrBot 后台接口（需要后台账号密码，密码用 Windows DPAPI 加密存在本机）。"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from core import load_state, log, protect, unprotect, update_state

_tok = {"token": None, "at": 0.0}


class ApiError(Exception):
    pass


def base() -> str:
    return f"http://127.0.0.1:{load_state()['astrbot_port']}"


def _raw(method: str, path: str, body=None, token=None, timeout: int = 60, stream=False):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base() + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {e.code}: {txt[:200]}")
    except Exception as e:
        raise ApiError(f"连不上 AstrBot 后台：{e}")
    if stream:
        return r
    with r:
        txt = r.read().decode("utf-8", "replace")
    try:
        return json.loads(txt) if txt else {}
    except ValueError:
        return {"status": "error", "message": txt[:300]}


def try_login(user: str, password: str) -> str | None:
    for pw in (password, hashlib.md5(password.encode()).hexdigest()):
        try:
            r = _raw("POST", "/api/v1/auth/login", {"username": user, "password": pw}, timeout=15)
        except ApiError:
            r = {}
        d = r.get("data") if isinstance(r, dict) else None
        if isinstance(d, dict) and d.get("token"):
            return d["token"]
    return None


def save_login(user: str, password: str) -> bool:
    tok = try_login(user, password)
    if not tok:
        return False
    update_state(dashboard_user=user, dashboard_pass=protect(password))
    _tok.update(token=tok, at=time.time())
    return True


def token() -> str:
    if _tok["token"] and time.time() - _tok["at"] < 3600:
        return _tok["token"]
    s = load_state()
    if not s.get("dashboard_pass"):
        raise ApiError("NEED_LOGIN")
    tok = try_login(s["dashboard_user"], unprotect(s["dashboard_pass"]))
    if not tok:
        raise ApiError("NEED_LOGIN")
    _tok.update(token=tok, at=time.time())
    return tok


def call(method: str, path: str, body=None, timeout: int = 60):
    """调后台接口。登录过期（HTTP 401）时重新登录再试一次；接口报错一律抛出，不当成功处理。"""
    for attempt in (1, 2):
        try:
            r = _raw(method, path, body, token(), timeout)
        except ApiError as e:
            if attempt == 1 and str(e).startswith("HTTP 401"):
                _tok["token"] = None
                continue
            raise
        if isinstance(r, dict) and r.get("status") == "error":
            raise ApiError(str(r.get("message") or r)[:300])
        return r.get("data", r) if isinstance(r, dict) else r
    raise ApiError("NEED_LOGIN")


def upload_plugin(zip_bytes: bytes, filename: str, timeout: int = 300):
    """把插件 zip 上传给 AstrBot 安装（已装过就覆盖并重载）。"""
    import uuid
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode("utf-8") + zip_bytes + f"\r\n--{boundary}--\r\n".encode()
    for attempt in (1, 2):
        req = urllib.request.Request(base() + "/api/v1/plugins/install/upload", data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", f"Bearer {token()}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                _tok["token"] = None
                continue
            raise ApiError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except Exception as e:
            raise ApiError(f"连不上 AstrBot 后台：{e}")
        try:
            d = json.loads(txt) if txt else {}
        except ValueError:
            d = {"status": "error", "message": txt[:300]}
        if isinstance(d, dict) and d.get("status") == "error":
            raise ApiError(str(d.get("message") or d)[:300])
        return d
    raise ApiError("NEED_LOGIN")


# ---------------------------------------------------------------- 人设

def list_personas() -> list[dict]:
    d = call("GET", "/api/v1/personas")
    items = d if isinstance(d, list) else (d.get("personas") or d.get("items") or []) if isinstance(d, dict) else []
    out = []
    for p in items:
        if isinstance(p, dict) and p.get("persona_id"):
            out.append({"persona_id": p["persona_id"], "system_prompt": p.get("system_prompt", ""),
                        "begin_dialogs": p.get("begin_dialogs") or []})
    return out


def save_persona(persona_id: str, system_prompt: str, is_new: bool) -> None:
    body = {"persona_id": persona_id, "system_prompt": system_prompt}
    if is_new:
        call("POST", "/api/v1/personas", body)
    else:
        call("PUT", "/api/v1/personas/" + urllib.parse.quote(persona_id, safe=""), body)


def delete_persona(persona_id: str) -> None:
    call("DELETE", "/api/v1/personas/" + urllib.parse.quote(persona_id, safe=""))


def get_session_service(umo: str) -> dict:
    try:
        d = call("GET", "/api/v1/sessions/rules?page=1&page_size=50&search=" + urllib.parse.quote(umo))
    except ApiError:
        return {}
    rows = d.get("rules") or d.get("items") or d.get("list") or [] if isinstance(d, dict) else d or []
    for row in rows:
        if isinstance(row, dict) and row.get("umo") == umo:
            return dict((row.get("rules") or row).get("session_service_config") or {})
    return {}


def set_session_service(umo: str, **kv) -> None:
    cur = get_session_service(umo)
    for k, v in kv.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    call("POST", "/api/v1/sessions/rules", {"umo": umo, "rule_key": "session_service_config", "rule_value": cur})


def get_session_persona(umo: str) -> str:
    try:
        d = call("GET", "/api/v1/sessions/rules?page=1&page_size=50&search=" + urllib.parse.quote(umo))
    except ApiError:
        return ""
    rows = d.get("rules") or d.get("items") or d.get("list") or [] if isinstance(d, dict) else d or []
    for row in rows:
        if not isinstance(row, dict) or row.get("umo") != umo:
            continue
        rules = row.get("rules") or row
        cfg = rules.get("session_service_config") or {}
        return cfg.get("persona_id") or ""
    return ""


def set_session_persona(umo: str, persona_id: str) -> None:
    cur = {}
    try:
        d = call("GET", "/api/v1/sessions/rules?page=1&page_size=50&search=" + urllib.parse.quote(umo))
        rows = d.get("rules") or d.get("items") or d.get("list") or [] if isinstance(d, dict) else d or []
        for row in rows:
            if isinstance(row, dict) and row.get("umo") == umo:
                cur = dict((row.get("rules") or row).get("session_service_config") or {})
    except ApiError:
        pass
    if persona_id:
        cur["persona_id"] = persona_id
    else:
        cur.pop("persona_id", None)
    call("POST", "/api/v1/sessions/rules", {"umo": umo, "rule_key": "session_service_config", "rule_value": cur})


def all_session_services(umos: list[str]) -> dict[str, dict]:
    """一次查完多个聊天的单独设置（人设页以前一个聊天发一次请求，群多了要等很久）。"""
    out = {u: {} for u in umos}
    try:
        d = call("GET", "/api/v1/sessions/rules?page=1&page_size=500")
    except ApiError:
        d = None
    rows = (d.get("rules") or d.get("items") or d.get("list") or []) if isinstance(d, dict) else (d or [])
    seen = set()
    for row in rows:
        if isinstance(row, dict) and row.get("umo") in out:
            out[row["umo"]] = dict((row.get("rules") or row).get("session_service_config") or {})
            seen.add(row["umo"])
    total = d.get("total") if isinstance(d, dict) else None
    if isinstance(total, int) and total > len(rows):      # 规则太多一页装不下，剩下的逐个查
        for u in umos:
            if u not in seen:
                out[u] = get_session_service(u)
    return out


# ---------------------------------------------------------------- 表情包（表情包管理器插件 meme_manager）

MEME = "/api/v1/plugins/extensions/meme_manager"


def meme_packs() -> list[dict]:
    d = call("GET", MEME + "/packs")
    packs = d.get("packs", []) if isinstance(d, dict) else []
    return [{k: p.get(k) for k in ("id", "name", "enabled", "is_default", "image_count", "category_count")}
            for p in packs if isinstance(p, dict)]


def meme_rules() -> list[dict]:
    d = call("GET", MEME + "/settings/rules")
    rules = [r for r in (d.get("rules", []) if isinstance(d, dict) else []) if isinstance(r, dict)]
    if not any(r.get("scope") == "default" for r in rules):    # 插件要求必须有且只有一条默认规则
        dp = (d.get("default_pack_id") if isinstance(d, dict) else "") or ""
        if dp:
            rules.append({"id": "default", "scope": "default", "pack_id": dp, "pack_ids": [dp]})
    return rules


def meme_persona_map() -> dict:
    """{"default": [默认用的包], "personas": {人设: [包]}}"""
    out = {"default": [], "personas": {}}
    for r in meme_rules():
        ids = r.get("pack_ids") or ([r["pack_id"]] if r.get("pack_id") else [])
        if r.get("scope") == "default":
            out["default"] = ids
        elif r.get("scope") == "persona" and r.get("target"):
            out["personas"][r["target"]] = ids
    return out


def meme_set_persona(persona: str, pack_ids: list[str]) -> None:
    """给某个人设指定表情包；pack_ids 为空 = 跟默认走。persona 写 "default" 时改的是默认那条。"""
    rules = meme_rules()
    pack_ids = [p for p in dict.fromkeys(str(x) for x in pack_ids) if p]
    if persona == "default":
        if not pack_ids:
            raise ApiError("默认至少要选一个表情包")
        for r in rules:
            if r.get("scope") == "default":
                r.update(pack_id=pack_ids[0], pack_ids=pack_ids)
    else:
        old = next((r for r in rules if r.get("scope") == "persona" and r.get("target") == persona), None)
        rules = [r for r in rules if r is not old]
        if pack_ids:
            ids = {r.get("id") for r in rules}
            rid = (old or {}).get("id") or f"dafeiyu-persona-{persona}"
            n = 2
            while rid in ids:
                rid = f"dafeiyu-persona-{persona}-{n}"
                n += 1
            # default 那条必须排在最后
            pos = next((i for i, r in enumerate(rules) if r.get("scope") == "default"), len(rules))
            rules.insert(pos, {"id": rid, "scope": "persona", "target": persona,
                               "pack_id": pack_ids[0], "pack_ids": pack_ids})
    call("POST", MEME + "/settings/rules", {"rules": rules})


def meme_create_pack(name: str, description: str = "") -> dict:
    d = call("POST", MEME + "/packs/create", {"name": name, "description": description})
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- 知识库

def kb_list() -> list[dict]:
    d = call("GET", "/api/v1/knowledge-bases?page=1&page_size=100")
    items = d.get("items", []) if isinstance(d, dict) else (d or [])
    return [{k: x.get(k) for k in ("kb_id", "kb_name", "description", "emoji", "doc_count", "chunk_count",
                                    "embedding_provider_id", "init_error")} for x in items if isinstance(x, dict)]


def kb_create(name: str, embedding_provider_id: str, description: str = "", emoji: str = "📚") -> dict:
    return call("POST", "/api/v1/knowledge-bases", {"name": name, "embedding_provider_id": embedding_provider_id,
                                                   "description": description, "emoji": emoji}, timeout=120)


def kb_delete(kb_id: str) -> None:
    call("DELETE", "/api/v1/knowledge-bases/" + urllib.parse.quote(kb_id, safe=""))


def kb_docs(kb_id: str) -> list[dict]:
    d = call("GET", "/api/v1/knowledge-bases/" + urllib.parse.quote(kb_id, safe="") + "/documents?page=1&page_size=200")
    items = d.get("items", []) if isinstance(d, dict) else []
    return [{k: x.get(k) for k in ("doc_id", "doc_name", "chunk_count", "file_size", "created_at")} for x in items]


def kb_doc_delete(kb_id: str, doc_id: str) -> None:
    call("DELETE", "/api/v1/knowledge-bases/" + urllib.parse.quote(kb_id, safe="") + "/documents/"
         + urllib.parse.quote(doc_id, safe=""))


def kb_upload(kb_id: str, filename: str, content: bytes, chunk_size: int = 512, chunk_overlap: int = 50) -> str:
    """上传一个文件，返回任务 id。"""
    boundary = "----dafeiyu" + hashlib.md5(content[:64] + str(time.time()).encode()).hexdigest()
    parts = []
    for k, v in (("chunk_size", chunk_size), ("chunk_overlap", chunk_overlap)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    safe = filename.replace('"', "")
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                  "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(base() + "/api/v1/knowledge-bases/" + urllib.parse.quote(kb_id, safe="") + "/documents",
                                 data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token()}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    if res.get("status") == "error":
        raise ApiError(str(res.get("message"))[:300])
    d = res.get("data") or {}
    return d.get("task_id", "")


def kb_task(task_id: str) -> dict:
    return call("GET", "/api/v1/knowledge-bases/tasks/" + urllib.parse.quote(task_id, safe=""))


def kb_search(kb_id: str, kb_name: str, query: str, top_k: int = 3) -> list[dict]:
    d = call("POST", "/api/v1/knowledge-bases/" + urllib.parse.quote(kb_id, safe="") + "/retrieve",
             {"query": query, "kb_names": [kb_name], "top_k": top_k}, timeout=60)
    out = []
    for x in (d.get("results") or []) if isinstance(d, dict) else []:
        out.append({"text": x.get("content") or x.get("text") or "", "score": x.get("score"),
                    "doc": x.get("doc_name") or ""})
    return out


# ---------------------------------------------------------------- 花费统计

def token_stats(days: int = 7):
    return call("GET", f"/api/v1/stats/provider-tokens?days={int(days)}")


# ---------------------------------------------------------------- 测试聊天（走 AstrBot 自带的网页聊天）

TEST_API = "/api/v1/plugins/extensions/astrbot_plugin_dafeiyu_common/test/"


def dialogue_options() -> dict:
    return call("GET", TEST_API + "options", timeout=15)


def dialogue_test(body: dict) -> dict:
    """Run a scoped text simulation; the plugin never executes outgoing tools."""
    return call("POST", TEST_API + "chat", {k: body[k] for k in
                ("umo", "text", "persona", "provider", "identity", "history", "session") if k in body}, timeout=125)

def test_chat(session_id: str, text: str, timeout: int = 120) -> str:
    r = _raw("POST", "/api/chat/send", {"session_id": session_id, "message": text, "enable_streaming": False},
             token(), timeout=timeout, stream=True)
    parts, t0 = [], time.time()
    with r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            typ = ev.get("type")
            data = ev.get("data")
            if ev.get("chain_type") in ("tool_call", "tool_call_result", "reasoning", "agent_stats"):
                continue
            if typ in ("image", "record", "file", "video"):
                parts.append({"image": "[表情包/图片]", "record": "[语音]", "file": "[文件]", "video": "[视频]"}[typ])
                continue
            if typ in ("plain", "complete", "message", "text") and isinstance(data, str):
                if typ == "complete":
                    parts = [data]
                else:
                    parts.append(data)
            elif typ in ("end", "done", "message_saved") and parts:
                pass
            if time.time() - t0 > timeout:
                break
    out = "\n".join(p.strip() for p in parts if p.strip()).strip()
    log(f"测试聊天：{text[:30]} -> {out[:60]}")
    return out or "（她没有回复，去日志里看看）"
