"""Manage model connections through AstrBot's existing provider APIs."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import urllib.parse
from contextlib import closing
from uuid import uuid4

import astrbot_api as ab
from core import APP, astrbot_data
from image_rules import VISION_PROMPT

PROTOCOLS = {
    "openai": ("openai", "openai_chat_completion"),
    "responses": ("openai", "openai_responses"),
    "anthropic": ("anthropic", "anthropic_chat_completion"),
    "gemini": ("google", "googlegenai_chat_completion"),
}
_lock = threading.Lock()
NON_VISUAL = {"deepseek-v4-pro", "glm-5.3"}


def catalog() -> dict:
    return json.loads((APP / "model_presets.json").read_text(encoding="utf-8"))


def _items(value, name):
    return value.get(name, []) if isinstance(value, dict) else value or []


def _url(value: str) -> str:
    url = urllib.parse.urlsplit(value.strip())
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("请填写不含密钥、查询参数或账号密码的 API 基础地址")
    return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))


def _safe_json(value):
    if isinstance(value, dict):
        return {k: ("[已隐藏]" if re.sub(r"[^a-z0-9]", "", k.lower()).endswith(
                    ("key", "token", "secret", "password", "authorization"))
                    else _safe_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_json(v) for v in value]
    return value


def snapshot() -> dict:
    sources = _items(ab.call("GET", "/api/v1/provider-sources"), "provider_sources")
    providers = _items(ab.call("GET", "/api/v1/providers?capability=chat_completion"), "providers")
    config = ab.call("GET", "/api/v1/config-profiles/default")
    config = config.get("config", config)
    default = config.get("agent_runner", {}).get("config", {}).get("model", {}).get("provider_id", "")
    image = config.get("provider_settings", {}).get("default_image_caption_provider_id", "")
    by_source = {s["id"]: s for s in sources}
    result = []
    for p in providers:
        source = by_source.get(p.get("provider_source_id"), {})
        protocol = next((k for k, v in PROTOCOLS.items() if v[1] == source.get("type")), "openai")
        result.append({"id": p["id"], "source_id": source.get("id", ""),
                       "name": source.get("dafeiyu_label") or source.get("id") or p["id"],
                       "preset": source.get("dafeiyu_preset", "custom"), "model": p.get("model", ""),
                       "protocol": protocol, "base_url": _public_url(source.get("api_base", "")),
                       "key_set": bool(source.get("key")), "vision": "image" in (p.get("modalities") or []),
                       "enabled": p.get("enable", True), "extra_body": _safe_json(p.get("custom_extra_body", {})),
                       "has_headers": bool(source.get("custom_headers")),
                       "shared_source": sum(x.get("provider_source_id") == source.get("id") for x in providers) > 1})
    sessions = []
    data = astrbot_data()
    if data and (data / "data_v4.db").exists():
        with closing(sqlite3.connect((data / "data_v4.db").as_uri() + "?mode=ro", uri=True)) as db:
            preferred = {r[0]: json.loads(r[1]).get("val") for r in db.execute(
                "SELECT scope_id,value FROM preferences WHERE scope='umo' AND key='provider_perf_chat_completion'")}
        for umo in config.get("platform_settings", {}).get("id_whitelist", []):
            parts = umo.split(":", 2)
            if len(parts) == 3 and parts[1] in ("GroupMessage", "FriendMessage"):
                sessions.append({"umo": umo, "label": ("群聊 " if parts[1] == "GroupMessage" else "私聊 ") + parts[2],
                                 "provider": preferred.get(umo) or default,
                                 "inherited": not preferred.get(umo)})
    return {**catalog(), "models": result, "default_chat": default, "default_image": image, "sessions": sessions}


def _public_url(value: str) -> str:
    url = urllib.parse.urlsplit(value)
    host = url.hostname or ""
    if url.port:
        host += ":" + str(url.port)
    return urllib.parse.urlunsplit((url.scheme, host, url.path, "", ""))


def connection(body: dict, source_id: str, provider_id: str = "", old_source=None, old_model=None) -> tuple[dict, dict]:
    """Build the same validated configuration for setup and subsequent edits."""
    preset = next((p for p in catalog()["presets"] if p["id"] == body.get("preset")), None)
    if preset is None:
        raise ValueError("请选择 API 预设")
    protocol = body.get("protocol") or preset["protocol"]
    if protocol not in PROTOCOLS:
        raise ValueError("请选择受支持的接口协议")
    base = _url(str(body.get("base_url") or preset["base_url"]))
    model = str(body.get("model") or preset["model"]).strip()
    if not model or len(model) > 240:
        raise ValueError("请填写有效的模型 ID")
    key = str(body.get("key") or "").strip()
    if key and ("\n" in key or "\r" in key):
        raise ValueError("API Key 不能包含换行")
    extra = body.get("extra_body", preset.get("extra_body", {}))
    headers = body.get("headers")
    if not isinstance(extra, dict) or (headers is not None and not isinstance(headers, dict)):
        raise ValueError("额外参数和请求头必须是 JSON 对象")
    if "[已隐藏]" in json.dumps(extra, ensure_ascii=False):
        raise ValueError("隐藏的额外参数需重新填写，或清空额外参数")
    if headers and any(not isinstance(k, str) or not isinstance(v, str) or "\n" in k + v or "\r" in k + v
                       for k, v in headers.items()):
        raise ValueError("请求头名称和值需要是单行文字")
    if not key and not old_source:
        raise ValueError("请填写 API Key")
    if old_source and not key and base != str(old_source.get("api_base", "")).rstrip("/"):
        raise ValueError("更换 API 地址时，请重新填写该地址对应的密钥")
    provider_name, provider_type = PROTOCOLS[protocol]
    source = copy.deepcopy(old_source) if old_source else {}
    source.update({"id": source_id, "provider": provider_name, "type": provider_type,
                   "provider_type": "chat_completion", "enable": True, "api_base": base,
                   "timeout": source.get("timeout", 120), "proxy": source.get("proxy", ""),
                   "dafeiyu_label": str(body.get("name") or preset["name"]).strip()[:80],
                   "dafeiyu_preset": preset["id"]})
    if key:
        source["key"] = [key]
    if headers is not None:
        source["custom_headers"] = headers
    source.setdefault("custom_headers", {})
    cfg = copy.deepcopy(old_model) if old_model else {}
    vision = bool(body.get("vision", model == preset["model"] and preset.get("vision", False))) and model.lower() not in NON_VISUAL
    cfg.update({"id": provider_id or source_id + "/" + model, "provider_source_id": source_id, "model": model,
                "enable": True, "modalities": ["text", "tool_use"] + (["image"] if vision else []),
                "custom_extra_body": extra})
    return source, cfg


def save(body: dict) -> dict:
    """Create or edit a connection without changing active session assignments."""
    old_source, old_model = None, None
    with _lock:
        provider_id = str(body.get("id") or "")
        if provider_id:
            old_model = ab.call("GET", "/api/v1/providers/by-id?merged=false&provider_id=" +
                                urllib.parse.quote(provider_id, safe="")).get("provider", {})
            if not old_model.get("provider_source_id"):
                raise ValueError("该模型连接需要在 AstrBot 原版后台中编辑")
            source_id = old_model["provider_source_id"]
            old_source = ab.call("GET", "/api/v1/provider-sources/by-id?source_id=" +
                                 urllib.parse.quote(source_id, safe=""))["provider_source"]
        else:
            source_id = "dafeiyu-" + uuid4().hex[:12]
        source, cfg = connection(body, source_id, provider_id, old_source, old_model)
        provider_id = cfg["id"]
        source_saved = False
        try:
            ab.call("PUT", "/api/v1/provider-sources/by-id", {"source_id": source_id, "config": source})
            source_saved = True
            if old_model:
                ab.call("PUT", "/api/v1/providers/by-id", {"provider_id": provider_id, "config": cfg})
            else:
                ab.call("POST", "/api/v1/provider-sources/providers", {"source_id": source_id, "config": cfg})
        except Exception:
            if source_saved:
                if old_source:
                    ab.call("PUT", "/api/v1/provider-sources/by-id", {"source_id": source_id, "config": old_source})
                    if old_model:
                        ab.call("PUT", "/api/v1/providers/by-id", {"provider_id": provider_id, "config": old_model})
                else:
                    ab.call("DELETE", "/api/v1/provider-sources/by-id?source_id=" +
                            urllib.parse.quote(source_id, safe=""))
            raise ValueError("模型连接未保存成功，已尝试恢复原配置；请检查接口协议、地址和模型 ID")
    return {"ok": True, "id": provider_id, "message": "已保存，尚未验证；原有会话的模型分配未改变"}


def assign(body: dict) -> dict:
    with _lock:
        view = snapshot()
        model = next((p for p in view["models"] if p["id"] == body.get("provider") and p["enabled"]), None)
        if model is None:
            raise ValueError("请选择已启用的模型")
        role = body.get("role")
        if role == "session":
            umo = body.get("umo")
            if not any(s["umo"] == umo for s in view["sessions"]):
                raise ValueError("该会话不在当前白名单中")
            result = ab.call("PATCH", "/api/v1/sessions/provider",
                             {"umos": [umo], "provider_type": "chat_completion", "provider_id": model["id"]})
            if result.get("failed_count"):
                raise ValueError("会话模型分配失败，原分配未确认改变")
        elif role in ("chat", "image"):
            if role == "image" and not model["vision"]:
                raise ValueError("此模型未标记为支持图片，请确认能力后再选择")
            conf = ab.call("GET", "/api/v1/config-profiles/default")
            conf = conf.get("config", conf)
            previous = copy.deepcopy(conf)
            group = original_group = None
            if role == "image":
                group = ab.call("GET", "/api/v1/plugins/config?plugin_id=astrbot_plugin_group_chat_plus").get("config")
                if not isinstance(group, dict):
                    raise ValueError("无法读取群聊识图设置，本次未做修改")
                original_group = copy.deepcopy(group)
            if role == "chat":
                conf.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("model", {})["provider_id"] = model["id"]
            else:
                conf.setdefault("provider_settings", {})["default_image_caption_provider_id"] = model["id"]
            ab.call("PUT", "/api/v1/config-profiles/default", conf)
            if role == "image":
                group["image_to_text_provider_id"] = model["id"]
                group["image_to_text_prompt"] = VISION_PROMPT
                try:
                    ab.call("PUT", "/api/v1/plugins/config",
                            {"plugin_id": "astrbot_plugin_group_chat_plus", "config": group})
                except Exception:
                    ab.call("PUT", "/api/v1/config-profiles/default", previous)
                    ab.call("PUT", "/api/v1/plugins/config",
                            {"plugin_id": "astrbot_plugin_group_chat_plus", "config": original_group})
                    raise ValueError("群聊识图模型应用失败，已恢复默认模型设置")
        else:
            raise ValueError("模型用途无效")
        return {"ok": True}


def apply_image_rules() -> dict:
    with _lock:
        group = ab.call("GET", "/api/v1/plugins/config?plugin_id=astrbot_plugin_group_chat_plus").get("config")
        if not isinstance(group, dict):
            raise ValueError("无法读取群聊插件设置")
        if group.get("image_to_text_prompt") != VISION_PROMPT:
            group["image_to_text_prompt"] = VISION_PROMPT
            ab.call("PUT", "/api/v1/plugins/config",
                    {"plugin_id": "astrbot_plugin_group_chat_plus", "config": group})
        return {"ok": True}


def test(provider_id: str) -> dict:
    if not any(p["id"] == provider_id for p in snapshot()["models"]):
        raise ValueError("模型不存在")
    result = ab.call("POST", "/api/v1/providers/test", {"provider_id": provider_id}, timeout=125)
    # Provider errors can contain headers or URLs. Keep the public failure text generic.
    return {"ok": result.get("status") == "available",
            "message": "模型验证通过" if result.get("status") == "available" else
                       "验证未通过，请检查密钥、模型权限、接口协议和网络；密钥不会回显"}


def remote_models(source_id: str) -> dict:
    known = _items(ab.call("GET", "/api/v1/provider-sources"), "provider_sources")
    if not any(s["id"] == source_id for s in known):
        raise ValueError("连接不存在")
    try:
        data = ab.call("GET", "/api/v1/provider-sources/models?source_id=" +
                       urllib.parse.quote(source_id, safe=""), timeout=30)
        return {"ok": True, "models": data.get("models", []) if isinstance(data, dict) else data}
    except Exception:
        return {"ok": False, "message": "这个接口暂未提供可读取的模型列表，可以手动填写模型ID"}
