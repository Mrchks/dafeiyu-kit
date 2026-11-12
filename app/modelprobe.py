"""模型与 API：配置时直接请求服务商，检测连接、读取模型列表，并对所选模型发起一次最小请求。

只用标准库。不经过 AstrBot，因此安装向导阶段（AstrBot 尚未运行）也能使用。
任何结果、报错和日志中都不出现密钥；服务商的原始报错不直接展示，统一翻译成可处理的说明。
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15
ANTHROPIC_VERSION = "2023-06-01"
_inflight: set[str] = set()
_inflight_lock = threading.Lock()

# 失败原因：前端按 status 显示，message 是给用户看的说明
MESSAGES = {
    "auth": "密钥无效或没有权限。请确认 API Key 填写完整、未过期，并且属于当前服务商和地域。",
    "notfound": "地址或接口协议不匹配：服务商没有找到对应的接口。请检查 API 基础地址和接口协议。",
    "model": "服务商不认识所选的模型 ID，或当前密钥无权使用该模型。请重新选择模型或核对模型 ID。",
    "quota": "余额不足或请求过于频繁。请到服务商控制台确认余额和限额，稍后再试。",
    "rejected": "服务商拒绝了验证请求，可能是该型号不支持当前参数。请检查「其他参数」和思考程度。",
    "server": "服务商暂时无法处理请求（服务端错误），请稍后再试。",
    "timeout": "请求超时（15 秒内没有响应）。请检查网络，或确认地址是否可以访问。",
    "network": "无法连接到服务商。请检查网络连接，以及 API 基础地址是否填写正确。",
    "tls": "证书校验失败或代理拦截了连接。请检查系统代理设置；使用中转站时，请确认地址以 https:// 开头且证书有效。",
    "busy": "该连接正在检测中，请等待上一次检测完成。",
    "unknown": "检测未完成，原因未知。请稍后重试；如果持续失败，可以生成问题报告。",
}


class ProbeError(Exception):
    def __init__(self, status: str, code: int = 0):
        super().__init__(status)
        self.status, self.code = status, code


def _base(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _anthropic_root(base: str) -> str:
    """Anthropic 的地址通常写到域名为止；写成 .../v1 时也能用。"""
    return base[:-3] if base.endswith("/v1") else base


def _gemini_root(base: str) -> str:
    for suffix in ("/v1beta", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _headers(protocol: str, key: str, extra: dict | None) -> dict:
    h = {"User-Agent": "dafeiyu-console", "Accept": "application/json"}
    if protocol == "anthropic":
        h.update({"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
    elif protocol == "gemini":
        h["x-goog-api-key"] = key
    else:
        h["Authorization"] = "Bearer " + key
    for k, v in (extra or {}).items():
        if isinstance(k, str) and isinstance(v, str) and "\n" not in k + v and "\r" not in k + v:
            h[k] = v
    return h


def _request(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = TIMEOUT):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4_000_000)
            try:
                return r.status, json.loads(raw.decode("utf-8") or "null")
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            text = e.read(20000).decode("utf-8", "replace")
        except Exception:
            text = ""
        raise ProbeError(_classify_http(e.code, text), e.code) from None
    except (socket.timeout, TimeoutError):
        raise ProbeError("timeout") from None
    except urllib.error.URLError as e:
        raise ProbeError(_classify_reason(e.reason)) from None
    except ssl.SSLError:
        raise ProbeError("tls") from None
    except (ConnectionError, OSError):
        raise ProbeError("network") from None


def _classify_reason(reason) -> str:
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper() or "SSL" in str(reason).upper():
        return "tls"
    text = str(reason).lower()
    if "tunnel" in text:
        # 经系统代理连接时，目标地址不可达也会表现为隧道建立失败；只有代理要求认证（407）才算代理问题
        return "tls" if "407" in text else "network"
    if "proxy" in text:
        return "tls"
    return "network"


MODEL_WORDS = ("model_not_found", "model not found", "does not exist", "not exist", "no such model", "invalid model",
               "unknown model", "model is not", "模型不存在", "不存在该模型", "无权访问该模型", "not supported model")


def _classify_http(code: int, text: str) -> str:
    low = (text or "").lower()
    if code in (401, 403):
        return "model" if any(w in low for w in MODEL_WORDS) and "key" not in low else "auth"
    if code == 402 or code == 429:
        return "quota"
    if code == 404:
        return "model" if any(w in low for w in MODEL_WORDS) else "notfound"
    if code in (400, 422):
        if any(w in low for w in MODEL_WORDS):
            return "model"
        if any(w in low for w in ("insufficient", "balance", "quota", "余额", "欠费", "arrears")):
            return "quota"
        if any(w in low for w in ("api key", "api_key", "apikey", "unauthorized", "authentication", "invalid key")):
            return "auth"
        return "rejected"
    if code >= 500:
        return "server"
    return "unknown"


def _redact(text: str, key: str) -> str:
    return text.replace(key, "[已隐藏]") if key else text


def _models_from(protocol: str, data) -> list[dict]:
    items = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or (data.get("output") or {}).get("models") or []
    elif isinstance(data, list):
        items = data
    out, seen = [], set()
    for x in items if isinstance(items, list) else []:
        if isinstance(x, str):
            mid, info = x, {}
        elif isinstance(x, dict):
            mid, info = x.get("id") or x.get("name") or x.get("model") or "", x
        else:
            continue
        mid = str(mid)
        if protocol == "gemini":
            if "generateContent" not in (info.get("supportedGenerationMethods") or ["generateContent"]):
                continue
            mid = mid.split("/", 1)[1] if mid.startswith("models/") else mid
        if not mid or mid in seen:
            continue
        seen.add(mid)
        m = {"id": mid}
        caps = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
        vision = info.get("supports_image_in", caps.get("image_input", caps.get("vision")))
        if isinstance(vision, dict):
            vision = vision.get("supported")
        if isinstance(vision, bool):
            m["vision"] = vision
        reasoning = info.get("supports_reasoning", caps.get("thinking", caps.get("reasoning")))
        if isinstance(reasoning, dict):
            reasoning = reasoning.get("supported")
        if isinstance(reasoning, bool):
            m["reasoning"] = reasoning
        out.append(m)
    return out


def _list_url(protocol: str, base: str) -> str:
    if protocol == "anthropic":
        return _anthropic_root(base) + "/v1/models?limit=1000"
    if protocol == "gemini":
        return _gemini_root(base) + "/v1beta/models?pageSize=1000"
    return base + "/models"


def _guard(fp: str):
    with _inflight_lock:
        if fp in _inflight:
            raise ProbeError("busy")
        _inflight.add(fp)


def _release(fp: str):
    with _inflight_lock:
        _inflight.discard(fp)


def _fingerprint(protocol: str, base: str, key: str) -> str:
    import hashlib
    return hashlib.sha256(f"{protocol}\n{base}\n{key}".encode("utf-8")).hexdigest()


def _fail(status: str, started: float, **extra) -> dict:
    return {"ok": False, "status": status, "message": MESSAGES.get(status, MESSAGES["unknown"]),
            "ms": int((time.monotonic() - started) * 1000), **extra}


def probe(protocol: str, base_url: str, key: str, headers: dict | None = None, timeout: float = TIMEOUT,
          expect_list: bool = False) -> dict:
    """检测连接并读取模型列表。服务商不提供列表时返回 listed=False，前端改为手动填写模型 ID。
    expect_list：官方文档说明提供列表接口的服务商，列表接口不存在就说明地址或协议不对。"""
    base, key = _base(base_url), str(key or "").strip()
    started = time.monotonic()
    if not base:
        return _fail("notfound", started)
    if not key:
        return _fail("auth", started)
    fp = _fingerprint(protocol, base, key)
    try:
        _guard(fp)
    except ProbeError:
        return _fail("busy", started)
    try:
        _code, data = _request("GET", _list_url(protocol, base), _headers(protocol, key, headers), timeout=timeout)
        models = _models_from(protocol, data)
        ms = int((time.monotonic() - started) * 1000)
        if not models:
            return {"ok": True, "status": "nolist", "listed": False, "models": [], "ms": ms,
                    "message": "已连接到服务商，但该服务商未提供模型列表。请手动填写模型 ID，保存前会再验证一次。"}
        return {"ok": True, "status": "ok", "listed": True, "models": models, "ms": ms,
                "message": f"连接正常，密钥有效 · 响应 {ms / 1000:.1f} 秒 · 读取到 {len(models)} 个模型"}
    except ProbeError as e:
        if e.status == "notfound" and not expect_list:
            # 列表接口不存在：多数情况是服务商没有提供列表，也可能是地址填错，交给保存前的验证区分
            ms = int((time.monotonic() - started) * 1000)
            return {"ok": True, "status": "nolist", "listed": False, "models": [], "ms": ms,
                    "message": "该服务商未提供模型列表。请手动填写模型 ID，保存前会对所选模型验证一次；如果验证提示地址不匹配，请检查 API 基础地址。"}
        return _fail(e.status, started, code=e.code)
    except Exception:
        return _fail("unknown", started)
    finally:
        _release(fp)


def _verify_request(protocol: str, base: str, model: str, extra_body: dict | None):
    """最小请求：一句很短的输入，限制输出长度。OpenAI 格式会带上其他参数，以便一并检查思考参数是否被接受。"""
    if protocol == "anthropic":
        return _anthropic_root(base) + "/v1/messages", {
            "model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "OK"}]}
    if protocol == "gemini":
        return (_gemini_root(base) + "/v1beta/models/" + urllib.parse.quote(model, safe="") + ":generateContent",
                {"contents": [{"role": "user", "parts": [{"text": "OK"}]}], "generationConfig": {"maxOutputTokens": 16}})
    if protocol == "responses":
        body = {"model": model, "input": "OK", "max_output_tokens": 16}
        extra = dict(extra_body or {})
        effort = extra.pop("reasoning_effort", None)
        if effort is not None and "reasoning" not in extra:
            extra["reasoning"] = {"effort": effort}
        body.update({k: v for k, v in extra.items() if k not in ("model", "input")})
        return base + "/responses", body
    body = {"model": model, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 16}
    body.update({k: v for k, v in (extra_body or {}).items() if k not in ("model", "messages", "stream")})
    return base + "/chat/completions", body


def verify(protocol: str, base_url: str, key: str, model: str, extra_body: dict | None = None,
           headers: dict | None = None, timeout: float = TIMEOUT) -> dict:
    """对所选模型发起一次最小请求（可能产生极少量用量）。"""
    base, key, model = _base(base_url), str(key or "").strip(), str(model or "").strip()
    started = time.monotonic()
    if not key:
        return _fail("auth", started)
    if not model:
        return _fail("model", started)
    fp = _fingerprint(protocol, base, key) + ":verify"
    try:
        _guard(fp)
    except ProbeError:
        return _fail("busy", started)
    try:
        url, body = _verify_request(protocol, base, model, extra_body)
        _request("POST", url, _headers(protocol, key, headers), body, timeout=timeout)
        ms = int((time.monotonic() - started) * 1000)
        return {"ok": True, "status": "ok", "ms": ms, "message": f"连接正常，所选模型可以使用 · 响应 {ms / 1000:.1f} 秒"}
    except ProbeError as e:
        return _fail(e.status, started, code=e.code)
    except Exception:
        return _fail("unknown", started)
    finally:
        _release(fp)


__all__ = ["probe", "verify", "MESSAGES", "_redact"]
