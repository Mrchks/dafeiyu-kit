"""控制台「记忆」页的后端：直接读写记忆插件的数据库（data/plugin_data/astrbot_plugin_dafeiyu_memory/memory.db）。

逻辑和插件共用整合包里的 memcore.py，不另写一套。整理需要调用模型，由插件在 AstrBot 里做：
控制台往数据库的 jobs 表放一条任务，插件每分钟取一次。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from core import PACK_DIR, astrbot_data

PLUGIN = "astrbot_plugin_dafeiyu_memory"
sys.path.insert(0, str(PACK_DIR / "plugins" / PLUGIN))
import memcore as mc  # noqa: E402

_con = None
_con_path = None


class MemoryError_(Exception):
    pass


def db_path() -> Path | None:
    d = astrbot_data()
    return d / "plugin_data" / PLUGIN / "memory.db" if d else None


def con():
    global _con, _con_path
    p = db_path()
    if not p:
        raise MemoryError_("还没装 AstrBot，没有记忆数据。")
    if _con is None or _con_path != p:
        _con = mc.connect(p)
        _con_path = p
    return _con


def plugin_conf() -> dict:
    d = astrbot_data()
    f = d / "config" / f"{PLUGIN}_config.json" if d else None
    try:
        return json.loads(f.read_text(encoding="utf-8-sig")) if f and f.exists() else {}
    except (OSError, ValueError):
        return {}


def _halflife(c: dict) -> dict:
    return {"long": float(c.get("halflife_long") or 180), "normal": float(c.get("halflife_normal") or 30),
            "temp": float(c.get("halflife_temp") or 3)}


def installed() -> bool:
    d = astrbot_data()
    return bool(d and (d / "plugins" / PLUGIN).is_dir())


def overview() -> dict:
    c = con()
    sc = mc.scopes(c)
    if mc.OWNER not in sc:
        sc.insert(0, mc.OWNER)
    return {"installed": installed(), "stats": mc.stats(c), "personas": mc.personas(c),
            "scopes": [{"scope": s, "title": mc.scope_title(c, s), "kind": mc.scope_kind(s)} for s in sc],
            "owner_persona": mc.get_meta(c, "owner_persona", ""), "version": mc.version(c)}


def graph(persona: str = "", archived: bool = False) -> dict:
    return mc.graph(con(), persona, halflife=_halflife(plugin_conf()), show_archived=archived)


def facts(scope: str, include_inactive: bool = False) -> list[dict]:
    c = con()
    hl = _halflife(plugin_conf())
    rows = mc.active_facts(c, scope, halflife=hl)
    if include_inactive:
        for r in c.execute("SELECT * FROM facts WHERE scope=? AND status IN ('archived','deleted','superseded') "
                           "ORDER BY updated DESC LIMIT 200", (scope,)):
            f = dict(r)
            f["retention"] = round(mc.retention(f, halflife=hl), 3)
            f["score"] = round(mc.score(f, halflife=hl), 4)
            rows.append(f)
    return rows


def diaries(scope: str, persona: str) -> list[dict]:
    c = con()
    rows = [dict(r) for r in c.execute("SELECT * FROM diaries WHERE scope=? AND persona=? AND status='active' "
                                        "ORDER BY day DESC, kind LIMIT 400", (scope, persona))]
    for r in rows:
        r["label"] = mc.day_label(r["day"]) if r["kind"] == "day" else f"{mc.day_label(r['day'], False)} 那一周"
    return rows


def followups(scope: str) -> list[dict]:
    return [dict(r) for r in con().execute(
        "SELECT * FROM followups WHERE scope=? AND status != 'deleted' ORDER BY "
        "CASE status WHEN 'open' THEN 0 WHEN 'asked' THEN 1 ELSE 2 END, due LIMIT 200", (scope,))]


def edit(tbl: str, row_id: int, fields: dict) -> dict:
    if tbl not in ("facts", "diaries", "followups"):
        raise MemoryError_("不能改这个")
    return mc.edit(con(), tbl, int(row_id), fields, actor="console")


def add_fact(scope: str, text: str, category: str, tier: str) -> int:
    fid, _ = mc.add_fact(con(), scope, text, category=category or "其他", tier=tier or "long", actor="console")
    return fid


def add_followup(scope: str, due: str, text: str) -> int | None:
    return mc.add_followup(con(), scope, due, text, actor="console")


def preview(scope: str, persona: str) -> str:
    c = plugin_conf()
    return mc.build_block(con(), scope, persona, profile_budget=int(c.get("profile_budget") or 800),
                          timeline_budget=int(c.get("timeline_budget") or 1500), halflife=_halflife(c),
                          principles=c.get("principles_enable", True) is not False)


def history(limit: int = 80) -> list[dict]:
    out = []
    for r in con().execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)):
        h = dict(r)
        for k in ("before", "after"):
            try:
                h[k] = json.loads(h[k]) if h[k] else None
            except ValueError:
                pass
        out.append(h)
    return out


def enqueue(kind: str) -> int:
    if not installed():
        raise MemoryError_("记忆插件还没装进 AstrBot。重启一次控制台会自动装上。")
    return mc.enqueue(con(), kind, {})


def job(job_id: int) -> dict:
    r = con().execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    return dict(r) if r else {}


# 回放测试：在「对话测试」里问一组固定问题，看她答得对不对。对话测试按主人身份读记忆，不写记忆。
REPLAY = [
    "昨天我们聊了什么？",
    "你还记得我是哪个学校的吗？",
    "我最近有什么要紧的事吗？",
    "我喜欢吃什么？",
    "上周我状态怎么样？",
]


def replay(questions: list[str] | None = None) -> list[dict]:
    import astrbot_api
    qs = [q.strip() for q in (questions or REPLAY) if q and q.strip()][:10]
    out = []
    media = re.compile(r"^(\s*\[(语音|表情包/图片|文件|视频)\]\s*)+$")
    for q in qs:
        t0 = time.time()
        for attempt in range(3):      # 她有一定概率用语音回，语音看不到文字，换个新对话再问（最多 3 次）
            sid = f"dafeiyu-memory-replay-{int(time.time() * 1000)}"      # 每题一个新对话，只靠记忆回答
            try:
                a = astrbot_api.test_chat(sid, q, timeout=120)
            except Exception as e:  # noqa: BLE001
                a = f"（出错：{e}）"
            if not media.match(a):
                break
        else:
            a += "（连续三次都用语音或表情回答，看不到文字内容）"
        out.append({"q": q, "a": a, "secs": round(time.time() - t0, 1)})
    return out
