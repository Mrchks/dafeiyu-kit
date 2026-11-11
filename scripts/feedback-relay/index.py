"""大肥鱼整合包 · 问题反馈中转（部署在阿里云函数计算，不随整合包分发）。

控制台把反馈 POST 到这个函数，函数用发件邮箱通过 SMTP 发到作者的收件邮箱。
发件邮箱的授权码只存在函数的环境变量里，整合包和公开源码里只有函数地址，拿到地址最多只能往作者邮箱发反馈。

部署：函数计算 3.0「事件函数」，运行环境 Python 3.10，请求处理程序 index.handler，HTTP 触发器（无需认证，允许 GET/POST），
最大实例数设为 1（频率限制记在内存里，只有一个实例才可靠）。

环境变量（在函数配置里填，作者本人填写）：
  SMTP_HOST   发件服务器，例如 smtp.163.com
  SMTP_PORT   默认 465（SSL）
  SMTP_USER   发件邮箱地址
  SMTP_PASS   发件邮箱的授权码（不是登录密码）
  MAIL_TO     收件邮箱地址
  DAILY_LIMIT 全部用户每天最多转发几封，默认 50
  IP_HOURLY   同一 IP 每小时最多几封，默认 3

请求（JSON）：
  {"app": "dafeiyu-kit", "kind": "问题|建议|其他", "desc": "描述", "contact": "选填", "version": "1.5.1",
   "system": "Windows 11 …", "device": "16 位设备码", "report": {"name": "问题报告-….zip", "data": "<base64>"}  // report 选填
  }
返回：{"ok": true} 或 {"ok": false, "error": "…"}；GET 返回服务状态，控制台可用来测试连通。
"""

from __future__ import annotations

import base64
import json
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

APP_ID = "dafeiyu-kit"                     # 不是密码，只用来挡住网上随机扫描的请求
KINDS = ("问题", "建议", "其他")
MAX_BODY = 8 * 1024 * 1024                 # 请求最大 8MB
MAX_REPORT = 5 * 1024 * 1024               # 问题报告最大 5MB
MAX_DESC = 5000

_lock = threading.Lock()
_ip_hits: dict[str, list[float]] = {}
_day = {"date": "", "count": 0}


def _resp(code: int, obj: dict) -> dict:
    return {"statusCode": code, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(obj, ensure_ascii=False), "isBase64Encoded": False}


def _client_ip(ev: dict) -> str:
    h = {k.lower(): v for k, v in (ev.get("headers") or {}).items()}
    xff = str(h.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or str(((ev.get("requestContext") or {}).get("http") or {}).get("sourceIp") or "unknown")


def _rate_ok(ip: str, now: float | None = None) -> str:
    """通过返回空字符串，否则返回原因。"""
    now = now or time.time()
    per_ip = int(os.environ.get("IP_HOURLY") or 3)
    per_day = int(os.environ.get("DAILY_LIMIT") or 50)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    with _lock:
        if _day["date"] != today:
            _day.update(date=today, count=0)
        hits = [t for t in _ip_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= per_ip:
            _ip_hits[ip] = hits
            return "发送过于频繁，请一小时后再试"
        if _day["count"] >= per_day:
            return "今日反馈数量已达上限，请明天再试"
        hits.append(now)
        _ip_hits[ip] = hits
        _day["count"] += 1
    return ""


def _refund(ip: str) -> None:
    """邮件没发出去时退回这次计数，免得用户重试被频率限制挡住。"""
    with _lock:
        if _ip_hits.get(ip):
            _ip_hits[ip].pop()
        _day["count"] = max(0, _day["count"] - 1)


def _clean(s, limit: int) -> str:
    s = str(s or "").replace("\r\n", "\n").strip()
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)[:limit]


def validate(data: dict) -> tuple[dict, bytes | None, str]:
    """返回 (整理后的字段, 问题报告字节或 None, 错误原因)。"""
    if not isinstance(data, dict) or data.get("app") != APP_ID:
        return {}, None, "请求不是由大肥鱼控制台发出"
    f = {"kind": data.get("kind") if data.get("kind") in KINDS else "其他",
         "desc": _clean(data.get("desc"), MAX_DESC), "contact": _clean(data.get("contact"), 100),
         "version": _clean(data.get("version"), 20), "system": _clean(data.get("system"), 200),
         "device": re.sub(r"[^0-9a-f]", "", str(data.get("device") or "").lower())[:16]}
    if len(f["desc"]) < 2:
        return {}, None, "请填写描述"
    rep = data.get("report")
    blob = None
    if rep:
        name = _clean((rep or {}).get("name"), 120)
        if not re.fullmatch(r"[\w\-. （）()一-鿿]+\.zip", name or ""):
            return {}, None, "问题报告的文件名不符合要求"
        try:
            blob = base64.b64decode(str(rep.get("data") or ""), validate=True)
        except Exception:  # noqa: BLE001
            return {}, None, "问题报告的内容无法解析"
        if len(blob) > MAX_REPORT or not blob.startswith(b"PK"):
            return {}, None, "问题报告超过大小限制，或不是压缩包"
        f["report_name"] = name
    return f, blob, ""


def build_mail(f: dict, blob: bytes | None, ip: str, req_id: str) -> EmailMessage:
    first = f["desc"].split("\n", 1)[0][:30]
    m = EmailMessage()
    m["Subject"] = f"[大肥鱼反馈][{f['version'] or '?'}][{f['kind']}] {first}"
    m["From"] = os.environ["SMTP_USER"]
    m["To"] = os.environ["MAIL_TO"]
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["contact"]):
        m["Reply-To"] = f["contact"]
    masked_ip = re.sub(r"\.\d+$", ".*", ip)
    m.set_content(
        f"类型：{f['kind']}\n版本：{f['version'] or '未知'}\n系统：{f['system'] or '未知'}\n"
        f"联系方式：{f['contact'] or '未填写'}\n设备码：{f['device'] or '未知'}\n问题报告：{f.get('report_name') or '未附带'}\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（服务器时间）\n来源：{masked_ip}\n编号：{req_id}\n\n"
        f"———— 描述 ————\n{f['desc']}\n")
    if blob:
        m.add_attachment(blob, maintype="application", subtype="zip", filename=f["report_name"])
    return m


def send(m: EmailMessage) -> None:
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(m)


def handler(event, context):
    ev = json.loads(event) if isinstance(event, (bytes, str)) else (event or {})
    method = str(((ev.get("requestContext") or {}).get("http") or {}).get("method") or "POST").upper()
    if method == "GET":
        ready = all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
        return _resp(200, {"ok": True, "service": "dafeiyu-feedback", "ready": ready})
    if method != "POST":
        return _resp(405, {"ok": False, "error": "仅支持 POST 请求"})
    body = ev.get("body") or ""
    if len(body) > MAX_BODY * 4 // 3 + 16:
        return _resp(413, {"ok": False, "error": "内容超过大小限制"})
    try:
        if ev.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return _resp(400, {"ok": False, "error": "内容格式不正确"})
    f, blob, err = validate(data)
    if err:
        return _resp(400, {"ok": False, "error": err})
    ip = _client_ip(ev)
    why = _rate_ok(ip)
    if why:
        return _resp(429, {"ok": False, "error": why})
    req_id = str((ev.get("requestContext") or {}).get("requestId") or getattr(context, "request_id", "") or "")
    try:
        send(build_mail(f, blob, ip, req_id))
    except KeyError:
        _refund(ip)
        return _resp(500, {"ok": False, "error": "反馈服务尚未完成发件邮箱配置"})
    except Exception as e:  # noqa: BLE001
        print(f"发送失败：{type(e).__name__}: {e}")
        _refund(ip)
        return _resp(502, {"ok": False, "error": "邮件发送失败，请稍后重试"})
    return _resp(200, {"ok": True, "id": req_id})
