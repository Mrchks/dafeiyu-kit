"""Manage model connections through AstrBot's existing provider APIs."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from uuid import uuid4

import astrbot_api as ab
import modelprobe
from core import APP, astrbot_data, load_state, update_state
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


# ---------------------------------------------------------------- 思考程度
# 预设在 model_presets.json 的 thinking 中描述每个档位写入的参数：
#   where=extra    写进 custom_extra_body（OpenAI 兼容和 Responses 适配器把它原样并入请求体）
#   where=provider 写进模型配置本身（Anthropic 用 anth_thinking_config，Gemini 用 gm_thinking_config）
# 没有写参数时按 default 显示（即服务商或 AstrBot 实际采用的默认档位）。无法识别时显示「自定义」，用户改选前不覆盖。
THINK_EXTRA_KEYS = {"thinking", "reasoning_effort", "enable_thinking", "reasoning", "thinking_budget"}
EMPTY_PROVIDER_THINKING = ({}, {"type": "", "budget": 0, "effort": ""}, {"budget": 0, "level": ""})


def thinking_spec(preset_id: str, protocol: str) -> dict | None:
    cat = catalog()
    preset = next((p for p in cat["presets"] if p["id"] == preset_id), None)
    if preset and preset.get("thinking") and preset["protocol"] == protocol:
        return preset["thinking"]
    return cat.get("thinking_generic", {}).get(protocol)


def _owned(spec: dict) -> set[str]:
    return {k for m in spec.get("levels", {}).values() for k in m}


def thinking_parse(spec: dict | None, extra: dict | None, provider: dict | None) -> str | None:
    """配置 → 界面档位。返回档位名、custom（无法识别），或 None（该型号不提供档位）。"""
    if not spec or spec.get("always"):
        return None
    extra, provider = extra or {}, provider or {}
    owned = _owned(spec)
    if spec["where"] == "extra":
        if any(k in extra for k in THINK_EXTRA_KEYS - owned):
            return "custom"
        present = {k: extra[k] for k in owned if k in extra}
    else:
        present = {k: provider[k] for k in owned if k in provider and provider[k] not in EMPTY_PROVIDER_THINKING}
        if any(k in extra for k in THINK_EXTRA_KEYS):
            return "custom"
    if not present:
        return spec["default"]
    for level, mapping in spec["levels"].items():
        if mapping and present == mapping:
            return level
    return "custom"


def thinking_apply(spec: dict, level: str, extra: dict, provider: dict) -> tuple[dict, dict]:
    """界面档位 → 配置。先去掉该型号所有档位用到的参数，再写入所选档位的参数。"""
    if level not in spec.get("levels", {}):
        raise ValueError("该型号不支持所选的思考程度")
    extra, provider = copy.deepcopy(extra or {}), copy.deepcopy(provider or {})
    target = extra if spec["where"] == "extra" else provider
    for k in _owned(spec):
        target.pop(k, None)
    target.update(copy.deepcopy(spec["levels"][level]))
    return extra, provider


def _merged_thinking(source: dict, provider: dict) -> dict:
    """AstrBot 读取模型配置时，模型自身的字段覆盖连接（provider source）上的同名字段。"""
    return {**{k: source[k] for k in ("anth_thinking_config", "gm_thinking_config") if k in source}, **provider}


# ---------------------------------------------------------------- 验证记录（只存在本机 state.json，不改 AstrBot 配置）
def _checks() -> dict:
    c = load_state().get("model_checks")
    return c if isinstance(c, dict) else {}


def _record(provider_id: str, result: dict) -> dict:
    rec = {"ok": bool(result.get("ok")), "status": result.get("status", "unknown"),
           "message": result.get("message", ""), "at": int(time.time())}
    checks = _checks()
    checks[provider_id] = rec
    update_state(model_checks=checks)
    return rec


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
    checks = _checks()
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
                       "thinking": thinking_parse(thinking_spec(source.get("dafeiyu_preset", "custom"), protocol),
                                                  p.get("custom_extra_body", {}), _merged_thinking(source, p)),
                       "check": checks.get(p["id"]),
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
    spec = thinking_spec(preset["id"], protocol)
    mode = body.get("thinking")
    if spec and not spec.get("always") and mode not in (None, "", "custom"):
        if mode == "keep":
            # 档位未改动：界面上隐藏了思考参数，这里原样放回，不产生任何配置变化
            if spec["where"] == "extra":
                old_extra = (old_model or {}).get("custom_extra_body") or {}
                extra = {**extra, **{k: old_extra[k] for k in _owned(spec) if k in old_extra}}
        else:
            extra, cfg = thinking_apply(spec, mode, extra, cfg)
    vision = bool(body.get("vision", model == preset["model"] and preset.get("vision", False))) and model.lower() not in NON_VISUAL
    cfg.update({"id": provider_id or source_id + "/" + model, "provider_source_id": source_id, "model": model,
                "enable": True, "modalities": ["text", "tool_use"] + (["image"] if vision else []),
                "custom_extra_body": extra})
    return source, cfg


def _saved_key(source: dict | None) -> str:
    keys = (source or {}).get("key") or []
    return str(keys[0]) if keys else ""


def _probe_target(body: dict) -> tuple[str, str, str, dict]:
    """检测和验证用的地址、协议、密钥、请求头。编辑已有连接且未重新填写密钥时，由后端读取已保存的密钥。"""
    preset = next((p for p in catalog()["presets"] if p["id"] == body.get("preset")), None) or {}
    protocol = body.get("protocol") or preset.get("protocol") or "openai"
    if protocol not in PROTOCOLS:
        raise ValueError("请选择受支持的接口协议")
    base = _url(str(body.get("base_url") or preset.get("base_url") or ""))
    key = str(body.get("key") or "").strip()
    headers = body.get("headers") if isinstance(body.get("headers"), dict) else None
    if body.get("id") and (not key or headers is None):
        old = ab.call("GET", "/api/v1/providers/by-id?merged=false&provider_id=" +
                      urllib.parse.quote(str(body["id"]), safe="")).get("provider", {})
        src = ab.call("GET", "/api/v1/provider-sources/by-id?source_id=" +
                      urllib.parse.quote(old.get("provider_source_id", ""), safe="")).get("provider_source", {})
        if not key:
            if base != str(src.get("api_base", "")).rstrip("/"):
                raise ValueError("更换 API 地址时，请重新填写该地址对应的密钥")
            key = _saved_key(src)
        if headers is None:
            headers = src.get("custom_headers") or {}
    return protocol, base, key, headers or {}


def verify_draft(body: dict) -> dict:
    """安装向导用：AstrBot 尚未运行时，直接对填写的内容发起最小请求验证。"""
    protocol, base, key, headers = _probe_target({k: v for k, v in body.items() if k != "id"})
    spec = thinking_spec(str(body.get("preset") or ""), protocol)
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    if spec and spec.get("where") == "extra" and body.get("thinking") in (spec.get("levels") or {}):
        extra, _ = thinking_apply(spec, body["thinking"], extra, {})
    return modelprobe.verify(protocol, base, key, str(body.get("model") or ""), extra, headers)


def probe(body: dict) -> dict:
    """配置时检测：直接请求服务商，返回是否连通、密钥是否有效、耗时和模型列表。"""
    protocol, base, key, headers = _probe_target(body)
    preset = next((p for p in catalog()["presets"] if p["id"] == body.get("preset")), None) or {}
    expect = bool(preset.get("list")) and protocol == preset.get("protocol")
    return modelprobe.probe(protocol, base, key, headers, expect_list=expect)


def save(body: dict) -> dict:
    """Create or edit a connection without changing active session assignments."""
    old_source, old_model = None, None
    check = None
    if body.get("verify") and not body.get("skip_verify"):
        protocol, base, key, headers = _probe_target(body)
        spec = thinking_spec(str(body.get("preset") or ""), protocol)
        extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
        if spec and spec.get("where") == "extra" and body.get("thinking") in (spec.get("levels") or {}):
            extra, _ = thinking_apply(spec, body["thinking"], extra, {})
        check = modelprobe.verify(protocol, base, key, str(body.get("model") or ""), extra, headers)
        if not check["ok"]:
            return {"ok": False, "verify_failed": True, **check}
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
    if check:
        _record(provider_id, check)
    else:
        checks = _checks()
        if checks.pop(provider_id, None) is not None:
            update_state(model_checks=checks)
    return {"ok": True, "id": provider_id, "verified": bool(check),
            "message": "已保存并验证通过；原有会话的模型分配未改变" if check else "已保存，尚未验证；原有会话的模型分配未改变"}


def delete(provider_id: str) -> dict:
    """删除连接。正在作为默认聊天、识图或某个会话的模型时，要求先修改分配。"""
    with _lock:
        view = snapshot()
        model = next((p for p in view["models"] if p["id"] == provider_id), None)
        if model is None:
            raise ValueError("连接不存在")
        uses = []
        if view["default_chat"] == provider_id:
            uses.append("默认聊天模型")
        if view["default_image"] == provider_id:
            uses.append("识图模型")
        uses += [s["label"] for s in view["sessions"] if s["provider"] == provider_id and not s["inherited"]]
        if uses:
            return {"ok": False, "in_use": uses,
                    "message": "该连接正在用作" + "、".join(uses) + "。请先在「用途分配」中改为其他连接，再删除。"}
        ab.call("DELETE", "/api/v1/providers/by-id?provider_id=" + urllib.parse.quote(provider_id, safe=""))
        if model["source_id"] and not any(p["source_id"] == model["source_id"] and p["id"] != provider_id for p in view["models"]):
            ab.call("DELETE", "/api/v1/provider-sources/by-id?source_id=" + urllib.parse.quote(model["source_id"], safe=""))
        checks = _checks()
        if checks.pop(provider_id, None) is not None:
            update_state(model_checks=checks)
    return {"ok": True}


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
    """验证已保存的连接：用保存的密钥对该模型发起一次最小请求，结果记入本机，列表中显示状态和时间。"""
    model = ab.call("GET", "/api/v1/providers/by-id?merged=false&provider_id=" +
                    urllib.parse.quote(provider_id, safe="")).get("provider", {})
    if not model.get("provider_source_id"):
        raise ValueError("连接不存在，或需要在 AstrBot 原版后台中验证")
    src = ab.call("GET", "/api/v1/provider-sources/by-id?source_id=" +
                  urllib.parse.quote(model["provider_source_id"], safe="")).get("provider_source", {})
    protocol = next((k for k, v in PROTOCOLS.items() if v[1] == src.get("type")), "openai")
    result = modelprobe.verify(protocol, str(src.get("api_base", "")), _saved_key(src), str(model.get("model", "")),
                               model.get("custom_extra_body") or {}, src.get("custom_headers") or {})
    rec = _record(provider_id, result)
    return {**result, "check": rec}


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
