"""问题反馈：控制台 → 阿里云函数中转（scripts/feedback-relay）→ 作者邮箱。

- 整合包里只有中转地址（core.FEEDBACK_URL），没有任何密码；地址要换时随自动更新下发。
- 发送的内容：类型、描述、联系方式（选填）、控制台版本、Windows 版本、匿名设备码，以及可选的问题报告
  （report.build 生成，QQ 号、群号、密钥、用户名已隐去）。界面上「查看将发送的内容」列出的就是全部。
- 设备码是第一次反馈时随机生成的 16 位十六进制，存在 state.json，不含任何个人或硬件信息；
  作者以后给采纳了建议的用户发体验权益时用它对应到这台电脑。
- 控制台自己也限制频率（每小时 3 次，和中转服务一致）。没发出去时把反馈打包存到 logs/导出，提示用户手动发给作者。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import platform
import re
import secrets
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import logtools
import report
from core import FEEDBACK_URL, VERSION, load_state, log, update_state

APP_ID = "dafeiyu-kit"
KINDS = ("问题", "建议", "其他")
HOURLY = 3
MAX_REPORT = 5 * 1024 * 1024
MAX_DESC = 5000


def device_id() -> str:
    d = str(load_state().get("device_id") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", d):
        d = secrets.token_hex(8)
        update_state(device_id=d)
    return d


def system_info() -> str:
    try:
        s = f"{platform.system()} {platform.release()}（{platform.version()}）"
    except Exception:  # noqa: BLE001
        s = "未知"
    return s[:120]


def preview(with_report: bool, context: dict | None = None) -> dict:
    """「查看将发送的内容」：要发出去的每一项。勾了问题报告就先生成一份，发送时用同一份。"""
    out = {"version": VERSION, "system": system_info(), "device": device_id(), "report": None}
    if with_report:
        r = report.build(context, reveal=False)
        with zipfile.ZipFile(r["path"]) as z:
            files = z.namelist()
        out["report"] = {"name": r["name"], "path": r["path"], "size": r["size"], "files": files}
    return out


def _recent(now: float | None = None) -> list[float]:
    now = now or time.time()
    return [t for t in (load_state().get("feedback_sent") or []) if isinstance(t, (int, float)) and now - t < 3600]


def _report_ok(path: str | None) -> Path | None:
    """只接受控制台自己生成在 logs/导出 里的问题报告。"""
    if not path:
        return None
    p = Path(path).resolve()
    exp = Path(logtools.EXPORT).resolve()
    if p.parent != exp or not p.name.startswith("问题报告-") or p.suffix != ".zip" or not p.exists():
        raise ValueError("未找到问题报告，请重新勾选「附带问题报告」")
    if p.stat().st_size > MAX_REPORT:
        raise ValueError("问题报告超过 5MB，请取消附带后发送，或将报告文件手动发送给作者")
    return p


def save_local(fields: dict, rep: Path | None) -> Path:
    """没发出去时把反馈存成一个压缩包，方便用户手动发给作者。"""
    out = Path(logtools.EXPORT)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"反馈-{dt.datetime.now():%Y%m%d-%H%M%S}.zip"
    text = (f"类型：{fields['kind']}\n版本：{fields['version']}\n系统：{fields['system']}\n设备码：{fields['device']}\n"
            f"联系方式：{fields['contact'] or '未填写'}\n时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"———— 描述 ————\n{fields['desc']}\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("反馈.txt", "﻿" + text)
        if rep:
            z.write(rep, rep.name)
    return path


def _post(payload: dict, timeout: int = 40) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(FEEDBACK_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": f"DafeiyuKit/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:          # 中转服务拒绝时（太频繁、内容不对）会带上原因
        try:
            return json.loads(e.read().decode("utf-8", "replace") or "{}") | {"ok": False}
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"反馈服务返回 {e.code}"}


def send(kind: str, desc: str, contact: str = "", report_path: str | None = None) -> dict:
    desc = str(desc or "").strip()[:MAX_DESC]
    contact = str(contact or "").strip()[:100]
    if len(desc) < 2:
        return {"ok": False, "error": "请填写描述"}
    try:
        rep = _report_ok(report_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    fields = {"kind": kind if kind in KINDS else "其他", "desc": desc, "contact": contact,
              "version": VERSION, "system": system_info(), "device": device_id()}
    recent = _recent()
    if len(recent) >= HOURLY:
        err = "发送太频繁了，请一小时后再试"
    else:
        payload = {"app": APP_ID, **fields}
        if rep:
            payload["report"] = {"name": rep.name, "data": base64.b64encode(rep.read_bytes()).decode("ascii")}
        try:
            r = _post(payload)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"连不上反馈服务器（{type(e).__name__}）"}
            log(f"问题反馈发送失败：{e}")
        if r.get("ok"):
            update_state(feedback_sent=(recent + [time.time()])[-HOURLY:])
            log(f"问题反馈已发送（{fields['kind']}，{'附带' if rep else '未附带'}问题报告）")
            return {"ok": True, "msg": "反馈已发送给作者。"}
        err = str(r.get("error") or "反馈发送失败")
    saved = save_local(fields, rep)
    log(f"问题反馈没发出去：{err}；已保存到 {saved.name}")
    return {"ok": False, "error": err, "saved": str(saved), "name": saved.name}
