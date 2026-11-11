"""大肥鱼记忆：核心逻辑。不依赖 AstrBot，插件和控制台共用同一份（控制台从整合包里直接导入）。

记忆分层（每轮放进上下文的内容越往上越稳定，命中缓存的部分越多）：
- 档案 facts：关于某个对象的稳定信息。主人的档案所有人设共用（scope=owner）；每个群一份（g:群号）；其他私聊对象各一份（u:QQ号）。
  每条带档位 tier（permanent 永久 / long 长期 / normal 一般 / temp 临时）、重要性、被提到的次数和最近一次被提到的时间。
- 日记 diaries：某个人设和某个对象之间每天发生的事（kind=day），每周合并成周记（kind=week）。按人设分开。
- 待跟进 followups：带日期的约定和事件（考试、出行……），到期时主动关心。
- 原文 messages：私聊和群聊的原始记录，只用于整理和查询，不放进上下文，默认保留 30 天。

遗忘：保持度 R = 0.5 ^ (距上次被提到的天数 / 半衰期)，得分 = 重要性 × R。永久档不衰减。
R 低于阈值的事实移入归档（不删除，可以恢复）；档案注入有篇幅上限，按档位和得分取前面的。

所有改动都写一条 history，控制台可以查看、撤回。meta.version 每次改动加 1，插件据此刷新缓存的注入内容。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import time
from pathlib import Path

SCHEMA_VER = 1
OWNER = "owner"
TIERS = ("permanent", "long", "normal", "temp")
TIER_NAMES = {"permanent": "永久", "long": "长期", "normal": "一般", "temp": "临时"}
DEFAULT_HALFLIFE = {"long": 180.0, "normal": 30.0, "temp": 3.0}
CATEGORIES = ("身份", "偏好", "习惯", "人际", "经历", "健康", "计划", "群规", "称呼", "其他")
ARCHIVE_BELOW = 0.08          # 保持度低于这个值就归档
WEEKDAYS = "一二三四五六日"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL, umo TEXT,
  persona TEXT, role TEXT NOT NULL, sender_id TEXT, sender_name TEXT, owner INTEGER DEFAULT 0, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_msg_scope_day ON messages(scope, day);
CREATE INDEX IF NOT EXISTS ix_msg_day ON messages(day);
CREATE TABLE IF NOT EXISTS diaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, persona TEXT NOT NULL, kind TEXT NOT NULL,
  day TEXT NOT NULL, day_end TEXT, text TEXT NOT NULL DEFAULT '', mood TEXT DEFAULT '', tags TEXT DEFAULT '[]',
  msg_count INTEGER DEFAULT 0, upto_ts REAL DEFAULT 0, edited INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
  created REAL, updated REAL, UNIQUE(scope, persona, kind, day));
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, category TEXT DEFAULT '其他', text TEXT NOT NULL,
  tier TEXT DEFAULT 'normal', importance REAL DEFAULT 0.5, mentions INTEGER DEFAULT 1, last_seen REAL,
  created REAL, updated REAL, source TEXT DEFAULT '', evidence TEXT DEFAULT '', tags TEXT DEFAULT '[]',
  persona TEXT DEFAULT '', status TEXT DEFAULT 'active', replaced_by INTEGER);
CREATE INDEX IF NOT EXISTS ix_fact_scope ON facts(scope, status);
CREATE TABLE IF NOT EXISTS followups(
  id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, persona TEXT DEFAULT '', due TEXT NOT NULL,
  text TEXT NOT NULL, status TEXT DEFAULT 'open', created REAL, updated REAL, asked_at REAL, source TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS names(scope TEXT PRIMARY KEY, name TEXT, umo TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, tbl TEXT, row_id INTEGER, action TEXT,
  before TEXT, after TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, args TEXT, status TEXT DEFAULT 'pending',
  result TEXT DEFAULT '', finished REAL);
"""


# ---------------------------------------------------------------- 连接与基础

def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    if get_meta(con, "schema") is None:
        set_meta(con, "schema", str(SCHEMA_VER))
        set_meta(con, "version", "1")
    con.commit()
    return con


def get_meta(con, k: str, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(con, k: str, v) -> None:
    con.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def bump(con) -> int:
    v = int(get_meta(con, "version", "0") or 0) + 1
    set_meta(con, "version", v)
    return v


def version(con) -> int:
    return int(get_meta(con, "version", "0") or 0)


def _row(r) -> dict:
    return dict(r) if r is not None else {}


def record(con, actor: str, tbl: str, row_id: int, action: str, before: dict | None, after: dict | None) -> None:
    con.execute("INSERT INTO history(ts, actor, tbl, row_id, action, before, after) VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor, tbl, row_id, action,
                 json.dumps(before, ensure_ascii=False) if before else "",
                 json.dumps(after, ensure_ascii=False) if after else ""))


def today(now: float | None = None) -> str:
    return dt.date.fromtimestamp(now or time.time()).isoformat()


def day_label(day: str, with_week: bool = True) -> str:
    d = dt.date.fromisoformat(day)
    return f"{d.month}月{d.day}日" + (f"（周{WEEKDAYS[d.weekday()]}）" if with_week else "")


def norm(s: str) -> str:
    return re.sub(r"[\s，。,.!！?？、:：;；~～「」『』“”\"'（）()\[\]【】…—\-_*#]+", "", s or "").lower()


def bigram_sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i:i + 2] for i in range(max(1, len(a) - 1))}
    gb = {b[i:i + 2] for i in range(max(1, len(b) - 1))}
    return len(ga & gb) / max(1, len(ga | gb))


def scope_kind(scope: str) -> str:
    if scope == OWNER:
        return "owner"
    return {"g": "group", "u": "user"}.get(scope.split(":", 1)[0], "other")


def set_name(con, scope: str, name: str, umo: str = "") -> None:
    if not name and not umo:
        return
    con.execute("INSERT INTO names(scope, name, umo, updated) VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                "name=COALESCE(NULLIF(excluded.name, ''), names.name), umo=COALESCE(NULLIF(excluded.umo, ''), names.umo), "
                "updated=excluded.updated", (scope, name or "", umo or "", time.time()))


def names(con) -> dict[str, dict]:
    return {r["scope"]: dict(r) for r in con.execute("SELECT * FROM names")}


def scope_title(con, scope: str) -> str:
    if scope == OWNER:
        return "主人"
    n = con.execute("SELECT name FROM names WHERE scope=?", (scope,)).fetchone()
    num = scope.split(":", 1)[-1]
    k = scope_kind(scope)
    nm = (n["name"] if n else "") or ""
    if k == "group":
        return f"群「{nm}」" if nm else f"群 {num}"
    return f"{nm}（{num}）" if nm else num


# ---------------------------------------------------------------- 原文

def log_message(con, *, scope: str, umo: str, persona: str, role: str, text: str, sender_id: str = "",
                sender_name: str = "", owner: bool = False, ts: float | None = None) -> None:
    text = (text or "").strip()
    if not text:
        return
    ts = ts or time.time()
    con.execute("INSERT INTO messages(ts, day, scope, umo, persona, role, sender_id, sender_name, owner, text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, today(ts), scope, umo, persona or "", role, str(sender_id or ""), sender_name or "",
                 1 if owner else 0, text[:2000]))
    con.commit()


def prune_messages(con, keep_days: int) -> int:
    cut = (dt.date.today() - dt.timedelta(days=max(1, keep_days))).isoformat()
    n = con.execute("DELETE FROM messages WHERE day < ?", (cut,)).rowcount
    con.commit()
    return n


def day_messages(con, scope: str, day: str, persona: str | None = None) -> list[dict]:
    """某个对象某一天的原文。persona 不为空时，只取那天她用这个人设时的对话（对方的话跟着她当时的人设走）。"""
    rows = [dict(r) for r in con.execute("SELECT * FROM messages WHERE scope=? AND day=? ORDER BY ts, id", (scope, day))]
    if persona is None:
        return rows
    return [r for r in rows if (r["persona"] or "") == persona]


def transcript(rows: list[dict], kind: str) -> str:
    lines = []
    for r in rows:
        t = time.strftime("%H:%M", time.localtime(r["ts"]))
        if r["role"] == "bot":
            who = "她"
        elif kind == "owner" or r["owner"]:
            who = "主人"
        else:
            who = f"{r['sender_name'] or '对方'}({r['sender_id']})" if kind == "group" else "对方"
        lines.append(f"{t} {who}：{r['text']}")
    return "\n".join(lines)


def personas_in_day(con, scope: str, day: str) -> list[str]:
    return [r["persona"] for r in con.execute(
        "SELECT persona, COUNT(*) n FROM messages WHERE scope=? AND day=? AND persona != '' GROUP BY persona ORDER BY n DESC",
        (scope, day))]


def pending_days(con, before_day: str, lookback: int = 3) -> list[tuple[str, str, str]]:
    """还没写日记的（对象, 人设, 日期），只看 before_day 之前 lookback 天内、有原文的。"""
    start = (dt.date.fromisoformat(before_day) - dt.timedelta(days=lookback)).isoformat()
    out = []
    for r in con.execute("SELECT DISTINCT scope, persona, day FROM messages WHERE day >= ? AND day < ? AND persona != '' "
                         "ORDER BY day", (start, before_day)):
        d = con.execute("SELECT upto_ts FROM diaries WHERE scope=? AND persona=? AND kind='day' AND day=?",
                        (r["scope"], r["persona"], r["day"])).fetchone()
        last = con.execute("SELECT MAX(ts) m FROM messages WHERE scope=? AND persona=? AND day=?",
                           (r["scope"], r["persona"], r["day"])).fetchone()["m"]
        if d is None or (last or 0) > (d["upto_ts"] or 0) + 1:
            out.append((r["scope"], r["persona"], r["day"]))
    return out


def idle_today(con, day: str, idle_minutes: int, min_new: int, now: float | None = None) -> list[tuple[str, str, str]]:
    """今天的对话停下来一段时间、而且日记之后又有足够多新消息的（对象, 人设, 日期）：先写一版今天的日记。"""
    now = now or time.time()
    out = []
    for r in con.execute("SELECT scope, persona, MAX(ts) last FROM messages WHERE day=? AND persona != '' "
                         "GROUP BY scope, persona", (day,)):
        if now - (r["last"] or 0) < idle_minutes * 60:
            continue
        d = con.execute("SELECT upto_ts FROM diaries WHERE scope=? AND persona=? AND kind='day' AND day=?",
                        (r["scope"], r["persona"], day)).fetchone()
        upto = d["upto_ts"] if d else 0
        n = con.execute("SELECT COUNT(*) n FROM messages WHERE scope=? AND persona=? AND day=? AND ts > ?",
                        (r["scope"], r["persona"], day, upto)).fetchone()["n"]
        if n >= min_new:
            out.append((r["scope"], r["persona"], day))
    return out


# ---------------------------------------------------------------- 遗忘打分

def retention(f: dict, now: float | None = None, halflife: dict | None = None) -> float:
    if f.get("tier") == "permanent":
        return 1.0
    hl = (halflife or DEFAULT_HALFLIFE).get(f.get("tier") or "normal", DEFAULT_HALFLIFE["normal"])
    days = max(0.0, ((now or time.time()) - float(f.get("last_seen") or f.get("created") or time.time())) / 86400)
    return 0.5 ** (days / max(0.1, hl))


def score(f: dict, now: float | None = None, halflife: dict | None = None) -> float:
    # 被提到得多的事实更牢：每多提一次，相当于重要性乘 1.1（最多 1.5 倍）
    boost = min(1.5, 1.0 + 0.1 * max(0, int(f.get("mentions") or 1) - 1))
    return float(f.get("importance") or 0.5) * boost * retention(f, now, halflife)


def maintain(con, now: float | None = None, halflife: dict | None = None, followup_grace: int = 3) -> dict:
    """每日维护：保持度太低的事实归档；过期太久的待跟进标记为过期。"""
    now = now or time.time()
    archived = 0
    for r in con.execute("SELECT * FROM facts WHERE status='active' AND tier != 'permanent'").fetchall():
        f = dict(r)
        if retention(f, now, halflife) < ARCHIVE_BELOW:
            con.execute("UPDATE facts SET status='archived', updated=? WHERE id=?", (now, f["id"]))
            record(con, "auto", "facts", f["id"], "archive", f, {**f, "status": "archived"})
            archived += 1
    cut = (dt.date.fromtimestamp(now) - dt.timedelta(days=followup_grace)).isoformat()
    expired = con.execute("UPDATE followups SET status='expired', updated=? WHERE status IN ('open','asked') AND due < ?",
                          (now, cut)).rowcount
    if archived or expired:
        bump(con)
    con.commit()
    return {"archived": archived, "expired": expired}


# ---------------------------------------------------------------- 查询

def active_facts(con, scope: str, now: float | None = None, halflife: dict | None = None) -> list[dict]:
    rows = [dict(r) for r in con.execute("SELECT * FROM facts WHERE scope=? AND status='active'", (scope,))]
    rank = {t: i for i, t in enumerate(TIERS)}
    for f in rows:
        f["retention"] = round(retention(f, now, halflife), 3)
        f["score"] = round(score(f, now, halflife), 4)
    rows.sort(key=lambda f: (rank.get(f["tier"], 9), -f["score"], f["id"]))
    return rows


def diaries(con, scope: str, persona: str, kind: str = "day", since: str = "", until: str = "9999") -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM diaries WHERE scope=? AND persona=? AND kind=? AND status='active' AND day >= ? AND day <= ? "
        "AND text != '' ORDER BY day", (scope, persona, kind, since, until))]


def open_followups(con, scope: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM followups WHERE scope=? AND status IN ('open','asked') ORDER BY due, id", (scope,))]


# ---------------------------------------------------------------- 每轮放进上下文的内容

PRINCIPLES_PRIVATE = (
    "【相处原则】\n"
    "- 先看对方现在的状态再决定怎么回：对方累了、难过、焦虑时，少开玩笑，先接住情绪，多听少讲道理，回复短一点、稳一点；对方开心时陪着一起开心。\n"
    "- 关心对方的现实生活：适时提醒休息、吃饭、早点睡；也鼓励对方和朋友、家人多来往。不要让对方觉得只能依靠你，也不说「只属于你」这类话。\n"
    "- 对方流露出不想活了、想伤害自己这类念头时，认真对待，不开玩笑、不转移话题：温和地说你在乎对方，建议马上联系信任的家人朋友，"
    "或拨打心理援助热线 12356；情况紧急时拨打 120 或 110。")
PRINCIPLES_GROUP = (
    "【相处原则】有人流露出不想活了、想伤害自己这类念头时，认真对待，不开玩笑；温和地关心对方，建议联系信任的人，"
    "或拨打心理援助热线 12356，紧急时拨打 120 或 110。")
USAGE_NOTE = ("以上是你记得的事。自然地用，不要逐条复述，也不要说「根据记忆」「我的记录里」。"
              "记不清的事用 read_diary（按日期翻日记）或 memory_search（按关键词查）查一下，查不到就说不记得，不要编。")


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def build_block(con, scope: str, persona: str, *, now: float | None = None, profile_budget: int = 800,
                timeline_budget: int = 1500, halflife: dict | None = None, principles: bool = True,
                include_followups: bool = True) -> str:
    """组装注入内容。内容只取决于数据库和日期，同一天里数据没变就逐字相同（便于命中缓存）。"""
    now = now or time.time()
    kind = scope_kind(scope)
    td = today(now)
    parts = []

    # 档案
    head = {"owner": "【关于主人】", "group": "【关于这个群】", "user": "【关于对方】"}.get(kind, "【关于对方】")
    lines, used = [], 0
    for f in active_facts(con, scope, now, halflife):
        ln = f"- {f['text']}"
        if used + len(ln) > profile_budget:
            break
        lines.append(ln)
        used += len(ln) + 1
    if lines:
        parts.append(head + "\n" + "\n".join(lines))

    # 时间线：最近 7 天的日记（今天的写「今天」）+ 更早的周记，超出篇幅就丢最早的
    since7 = (dt.date.fromtimestamp(now) - dt.timedelta(days=6)).isoformat()
    days = diaries(con, scope, persona, "day", since7, td)
    weeks = [w for w in diaries(con, scope, persona, "week", "", td) if (w["day_end"] or w["day"]) < since7]
    entries = []
    for d in reversed(days):            # 从新到旧放，篇幅满了就停
        label = "今天" if d["day"] == td else day_label(d["day"])
        mood = f"（主人状态：{d['mood']}）" if d.get("mood") and kind == "owner" else ""
        entries.append((d["day"], f"- {label}：{d['text']}{mood}"))
    for w in reversed(weeks):
        a, b = dt.date.fromisoformat(w["day"]), dt.date.fromisoformat(w["day_end"] or w["day"])
        entries.append((w["day"], f"- {a.month}月{a.day}日–{b.month}月{b.day}日：{w['text']}"))
    keep, used = [], 0
    for day, ln in entries:
        if used + len(ln) > timeline_budget:
            break
        keep.append((day, ln))
        used += len(ln) + 1
    if keep:
        keep.sort(key=lambda x: x[0])
        parts.append("【最近的日子】\n" + "\n".join(ln for _, ln in keep))

    # 待跟进
    if include_followups:
        fl = []
        horizon = (dt.date.fromtimestamp(now) + dt.timedelta(days=14)).isoformat()
        for f in open_followups(con, scope):
            if f["due"] > horizon:
                continue
            if f["due"] < td:
                note = "（已经过了，还不知道结果，可以找机会问一句）"
            elif f["due"] == td:
                note = "（就是今天，可以自然地问一句或关心一下）"
            else:
                note = ""
            if f["status"] == "asked":
                note = "（已经主动问过了，等对方回）"
            fl.append(f"- {day_label(f['due'], False)}：{f['text']}{note}")
        if fl:
            parts.append("【记着要跟进的事】\n" + "\n".join(fl))

    if parts:
        parts.append(USAGE_NOTE)
    if principles:
        parts.append(PRINCIPLES_GROUP if kind == "group" else PRINCIPLES_PRIVATE)
    if not parts:
        return ""
    return "\n\n<她的记忆>\n" + "\n\n".join(parts) + "\n</她的记忆>"


# ---------------------------------------------------------------- 整理（写日记、更新档案）

FACT_RULES = {
    "owner": ("主人", "只记主人自己明确说出的、关于主人本人的稳定信息：身份、学校工作、作息习惯、口味偏好、重要的人、经历、计划。"
                      "不记猜测、玩笑、反话、她（机器人）说的话、一时的情绪。健康类信息只有主人明确说出时才记。"),
    "user": ("对方", "只记对方自己明确说出的、关于对方本人的稳定信息。不记猜测、玩笑、她（机器人）说的话、一时的情绪。"),
    "group": ("这个群", "只记关于这个群本身的稳定信息：群友之间的称呼和外号、群里的梗、群规、大家常聊的话题、群友明确说出的自身信息。"
                        "不记猜测、玩笑、她（机器人）说的话、一时的事。"),
}

DAY_PROMPT = """你负责为一个陪伴聊天机器人整理某一天的聊天记录。机器人当时用的人设叫「{persona}」，记录里的「她」就是机器人。
日期：{day}（周{weekday}）。聊天对象：{who}。

已有的档案（id: 内容）：
{facts}

还在跟进的事（id: 日期 内容）：
{followups}

当天的聊天记录：
<记录>
{log}
</记录>

请只输出一个 JSON 对象，不要输出其他内容。字段：
- "diary": 当天发生了什么。用第三人称、客观、按时间顺序写，可以写她和{target}聊了什么、{target}做了什么、有什么情绪，150 字以内；没有值得记的就写空字符串。
- "mood": {target}当天的整体状态，10 字以内，例如「累但心情不错」；看不出来就写空字符串。
- "tags": 当天出现的重要人物、事物或话题，最多 6 个，每个 2 到 6 个字。
- "facts": 新的档案条目列表。{fact_rule}每条格式：{{"text": "陈述句，30 字以内", "category": "{categories}之一", "tier": "permanent（身份、生日、称呼等不会变的）/ long（长期偏好、习惯、关系）/ normal（近期的情况）/ temp（几天内有效的）", "importance": 0.1 到 1.0, "evidence": "从记录里逐字摘录的原话片段，必须是{target}说的", "replaces": 如果是更新某条已有档案，填那条的 id，否则填 null}}。最多 {max_facts} 条，没有就给空列表。已有档案里已经有的不要重复写。
- "mentioned": 当天再次提到、而且仍然成立的已有档案 id 列表。
- "followups": 需要以后跟进的、有明确日期的事（考试、面试、出行、看病、约定等），格式 {{"due": "YYYY-MM-DD", "text": "20 字以内"}}。只写今天以后（含今天）的，没有就给空列表。
- "followups_done": 已经有结果、不用再跟进的事的 id 列表。
"""

WEEK_PROMPT = """把下面这一周的日记合并成一段周记：用第三人称、客观，保留重要的事、{target}的状态变化和约定，200 字以内。只输出周记正文。
{week}
"""


def build_day_prompt(con, scope: str, persona: str, day: str, *, max_chars: int = 14000, max_facts: int = 8,
                     halflife: dict | None = None) -> tuple[str, str] | None:
    """返回 (提示词, 用于核对原话的文本)。当天没有记录返回 None。"""
    kind = scope_kind(scope)
    rows = day_messages(con, scope, day, persona)
    if not rows:
        return None
    log = transcript(rows, kind)
    if len(log) > max_chars:                  # 太长就保留最后一段（一天聊太多时，后面的内容更可能还有用）
        log = "（前面的记录太长，已省略）\n" + log[-max_chars:]
    facts = active_facts(con, scope, halflife=halflife)[:60]
    fl = open_followups(con, scope)
    target, rule = FACT_RULES.get(kind, FACT_RULES["user"])
    who = {"owner": "主人（机器人的主人）", "group": scope_title(con, scope), "user": scope_title(con, scope)}.get(kind, "")
    prompt = DAY_PROMPT.format(
        persona=persona, day=day, weekday=WEEKDAYS[dt.date.fromisoformat(day).weekday()], who=who,
        facts="\n".join(f"{f['id']}: {f['text']}" for f in facts) or "（暂无）",
        followups="\n".join(f"{f['id']}: {f['due']} {f['text']}" for f in fl) or "（暂无）",
        log=log, target=target, fact_rule=rule, categories="、".join(CATEGORIES), max_facts=max_facts)
    if kind == "group":
        said = "\n".join(r["text"] for r in rows if r["role"] != "bot")
    else:
        said = "\n".join(r["text"] for r in rows if r["role"] != "bot")
    return prompt, said


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            return None
        t = t[a:b + 1]
    try:
        d = json.loads(t)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _valid_date(s: str) -> str | None:
    try:
        return dt.date.fromisoformat(str(s).strip()[:10]).isoformat()
    except ValueError:
        return None


def apply_day(con, scope: str, persona: str, day: str, result: dict, said: str, *, upto_ts: float, msg_count: int,
              max_facts: int = 8, actor: str = "auto", now: float | None = None) -> dict:
    """把模型的整理结果写进数据库，经过写入门：原话核对、去重、长度和数量限制。返回统计。"""
    now = now or time.time()
    stats = {"diary": False, "facts_new": 0, "facts_updated": 0, "facts_mentioned": 0, "facts_rejected": 0,
             "followups_new": 0, "followups_done": 0}
    said_n = norm(said)

    # 日记：用户在控制台改过的那篇不覆盖
    diary = _clip(str(result.get("diary") or ""), 300)
    mood = _clip(str(result.get("mood") or ""), 20)
    tags = [_clip(str(t), 8) for t in (result.get("tags") or []) if isinstance(t, str) and t.strip()][:6]
    old = con.execute("SELECT * FROM diaries WHERE scope=? AND persona=? AND kind='day' AND day=?",
                      (scope, persona, day)).fetchone()
    if old is None:
        cur = con.execute("INSERT INTO diaries(scope, persona, kind, day, text, mood, tags, msg_count, upto_ts, created, updated) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (scope, persona, "day", day, diary, mood, json.dumps(tags, ensure_ascii=False), msg_count,
                           upto_ts, now, now))
        record(con, actor, "diaries", cur.lastrowid, "create", None, {"text": diary, "mood": mood})
        stats["diary"] = True
    elif not old["edited"]:
        con.execute("UPDATE diaries SET text=?, mood=?, tags=?, msg_count=?, upto_ts=?, updated=?, status='active' WHERE id=?",
                    (diary, mood, json.dumps(tags, ensure_ascii=False), msg_count, upto_ts, now, old["id"]))
        record(con, actor, "diaries", old["id"], "update", {"text": old["text"], "mood": old["mood"]}, {"text": diary, "mood": mood})
        stats["diary"] = True
    else:
        con.execute("UPDATE diaries SET upto_ts=?, msg_count=? WHERE id=?", (upto_ts, msg_count, old["id"]))

    # 档案
    existing = {f["id"]: f for f in active_facts(con, scope)}
    added = 0
    for item in (result.get("facts") or [])[:max_facts * 2]:
        if not isinstance(item, dict):
            continue
        text = _clip(str(item.get("text") or ""), 60)
        ev = str(item.get("evidence") or "").strip()
        if len(norm(text)) < 4 or len(norm(ev)) < 2 or norm(ev) not in said_n:
            stats["facts_rejected"] += 1            # 原话对不上：宁可漏记，不可错记
            continue
        tier = item.get("tier") if item.get("tier") in TIERS else "normal"
        cat = item.get("category") if item.get("category") in CATEGORIES else "其他"
        try:
            imp = min(1.0, max(0.1, float(item.get("importance") or 0.5)))
        except (TypeError, ValueError):
            imp = 0.5
        dup = next((f for f in existing.values() if bigram_sim(f["text"], text) >= 0.75), None)
        if dup:
            con.execute("UPDATE facts SET mentions=mentions+1, last_seen=?, updated=? WHERE id=?", (now, now, dup["id"]))
            stats["facts_mentioned"] += 1
            continue
        if added >= max_facts:
            break
        rep = item.get("replaces")
        try:
            rep = int(rep) if rep not in (None, "", "null") else None
        except (TypeError, ValueError):
            rep = None
        cur = con.execute("INSERT INTO facts(scope, category, text, tier, importance, mentions, last_seen, created, updated, "
                          "source, evidence, tags, persona) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (scope, cat, text, tier, imp, 1, now, now, now, f"diary:{day}", _clip(ev, 120),
                           json.dumps(tags, ensure_ascii=False), persona))
        nid = cur.lastrowid
        new = {"id": nid, "text": text, "tier": tier, "category": cat, "importance": imp}
        if rep and rep in existing:
            con.execute("UPDATE facts SET status='superseded', replaced_by=?, updated=? WHERE id=?", (nid, now, rep))
            record(con, actor, "facts", rep, "supersede", existing[rep], {"replaced_by": nid})
            stats["facts_updated"] += 1
            existing.pop(rep, None)
        else:
            stats["facts_new"] += 1
        record(con, actor, "facts", nid, "create", None, new)
        existing[nid] = {**new, "status": "active"}
        added += 1
    for fid in result.get("mentioned") or []:
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        if fid in existing:
            con.execute("UPDATE facts SET mentions=mentions+1, last_seen=? WHERE id=? AND status='active'", (now, fid))
            stats["facts_mentioned"] += 1

    # 待跟进
    opens = open_followups(con, scope)
    lo = (dt.date.fromisoformat(day)).isoformat()
    hi = (dt.date.fromisoformat(day) + dt.timedelta(days=366)).isoformat()
    for item in (result.get("followups") or [])[:5]:
        if not isinstance(item, dict):
            continue
        due = _valid_date(item.get("due") or "")
        text = _clip(str(item.get("text") or ""), 40)
        if not due or not (lo <= due <= hi) or len(norm(text)) < 2:
            continue
        if any(o["due"] == due and bigram_sim(o["text"], text) >= 0.5 for o in opens):
            continue
        cur = con.execute("INSERT INTO followups(scope, persona, due, text, status, created, updated, source) "
                          "VALUES(?,?,?,?,?,?,?,?)", (scope, persona, due, text, "open", now, now, f"diary:{day}"))
        record(con, actor, "followups", cur.lastrowid, "create", None, {"due": due, "text": text})
        opens.append({"due": due, "text": text})
        stats["followups_new"] += 1
    ids = {o.get("id") for o in opens if o.get("id")}
    for fid in result.get("followups_done") or []:
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        if fid in ids:
            con.execute("UPDATE followups SET status='done', updated=? WHERE id=?", (now, fid))
            record(con, actor, "followups", fid, "done", None, None)
            stats["followups_done"] += 1
    bump(con)
    con.commit()
    return stats


def weeks_to_merge(con, now: float | None = None) -> list[tuple[str, str, str, str]]:
    """已经过完、还没合并成周记的周：(对象, 人设, 周一, 周日)。只看最近 8 周。"""
    td = dt.date.fromtimestamp(now or time.time())
    this_monday = td - dt.timedelta(days=td.weekday())
    out = []
    for back in range(1, 9):
        mon = this_monday - dt.timedelta(days=7 * back)
        sun = mon + dt.timedelta(days=6)
        for r in con.execute("SELECT DISTINCT scope, persona FROM diaries WHERE kind='day' AND status='active' AND text != '' "
                             "AND day >= ? AND day <= ?", (mon.isoformat(), sun.isoformat())):
            w = con.execute("SELECT id FROM diaries WHERE scope=? AND persona=? AND kind='week' AND day=?",
                            (r["scope"], r["persona"], mon.isoformat())).fetchone()
            if w is None:
                out.append((r["scope"], r["persona"], mon.isoformat(), sun.isoformat()))
    return out


def build_week_prompt(con, scope: str, persona: str, mon: str, sun: str) -> str | None:
    ds = diaries(con, scope, persona, "day", mon, sun)
    if not ds:
        return None
    target = FACT_RULES.get(scope_kind(scope), FACT_RULES["user"])[0]
    week = "\n".join(f"{day_label(d['day'])}：{d['text']}" + (f"（状态：{d['mood']}）" if d.get("mood") else "") for d in ds)
    return WEEK_PROMPT.format(target=target, week=week)


def save_week(con, scope: str, persona: str, mon: str, sun: str, text: str, actor: str = "auto") -> None:
    now = time.time()
    text = _clip(text.strip().strip("\"'"), 400)
    cur = con.execute("INSERT OR IGNORE INTO diaries(scope, persona, kind, day, day_end, text, created, updated) "
                      "VALUES(?,?,?,?,?,?,?,?)", (scope, persona, "week", mon, sun, text, now, now))
    if cur.rowcount:
        record(con, actor, "diaries", cur.lastrowid, "create", None, {"week": mon, "text": text})
        bump(con)
    con.commit()


# ---------------------------------------------------------------- 工具用的读写

def add_fact(con, scope: str, text: str, *, category: str = "其他", tier: str = "long", importance: float = 0.7,
             persona: str = "", actor: str = "tool", evidence: str = "") -> tuple[int, str]:
    """主动记一条。和已有的相似就当作再次提到。返回 (id, 'new' | 'dup')。"""
    now = time.time()
    text = _clip(text, 80)
    for f in active_facts(con, scope):
        if bigram_sim(f["text"], text) >= 0.75:
            con.execute("UPDATE facts SET mentions=mentions+1, last_seen=?, updated=? WHERE id=?", (now, now, f["id"]))
            bump(con)
            con.commit()
            return f["id"], "dup"
    cur = con.execute("INSERT INTO facts(scope, category, text, tier, importance, mentions, last_seen, created, updated, "
                      "source, evidence, persona) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (scope, category if category in CATEGORIES else "其他", text, tier if tier in TIERS else "long",
                       min(1.0, max(0.1, importance)), 1, now, now, now, actor, _clip(evidence, 120), persona))
    record(con, actor, "facts", cur.lastrowid, "create", None, {"text": text, "tier": tier})
    bump(con)
    con.commit()
    return cur.lastrowid, "new"


def forget_facts(con, scope: str, keyword: str, actor: str = "tool") -> list[dict]:
    """按关键词找到并删除（标记为已删除，控制台可以恢复）。"""
    k = norm(keyword)
    if len(k) < 2:
        return []
    hit = [f for f in active_facts(con, scope) if k in norm(f["text"])]
    now = time.time()
    for f in hit:
        con.execute("UPDATE facts SET status='deleted', updated=? WHERE id=?", (now, f["id"]))
        record(con, actor, "facts", f["id"], "delete", f, {**f, "status": "deleted"})
    if hit:
        bump(con)
    con.commit()
    return hit


def add_followup(con, scope: str, due: str, text: str, persona: str = "", actor: str = "tool") -> int | None:
    d = _valid_date(due)
    if not d or len(norm(text)) < 2:
        return None
    now = time.time()
    for o in open_followups(con, scope):
        if o["due"] == d and bigram_sim(o["text"], text) >= 0.5:
            return o["id"]
    cur = con.execute("INSERT INTO followups(scope, persona, due, text, status, created, updated, source) VALUES(?,?,?,?,?,?,?,?)",
                      (scope, persona, d, _clip(text, 40), "open", now, now, actor))
    record(con, actor, "followups", cur.lastrowid, "create", None, {"due": d, "text": text})
    bump(con)
    con.commit()
    return cur.lastrowid


def search(con, scope: str, keyword: str, persona: str | None = None, limit: int = 20) -> list[str]:
    k = (keyword or "").strip()
    if not k:
        return []
    like = f"%{k}%"
    out = []
    for f in con.execute("SELECT * FROM facts WHERE scope=? AND status IN ('active','archived') AND text LIKE ? "
                         "ORDER BY updated DESC LIMIT ?", (scope, like, limit)):
        out.append(f"[档案{'·已淡忘' if f['status'] == 'archived' else ''}] {f['text']}")
    q = "SELECT * FROM diaries WHERE scope=? AND status='active' AND text LIKE ?"
    args = [scope, like]
    if persona:
        q += " AND persona=?"
        args.append(persona)
    for d in con.execute(q + " ORDER BY day DESC LIMIT ?", (*args, limit)):
        label = day_label(d["day"]) if d["kind"] == "day" else f"{day_label(d['day'], False)}那一周"
        out.append(f"[日记·{label}] {d['text']}")
    for m in con.execute("SELECT * FROM messages WHERE scope=? AND text LIKE ? ORDER BY ts DESC LIMIT ?", (scope, like, limit)):
        who = "她" if m["role"] == "bot" else (m["sender_name"] or "对方")
        out.append(f"[原话·{time.strftime('%m-%d %H:%M', time.localtime(m['ts']))}] {who}：{_clip(m['text'], 80)}")
    return out[:limit * 2]


def parse_day_spec(spec: str, now: float | None = None) -> tuple[str, str] | None:
    """「昨天」「前天」「今天」「上周」「最近7天」「11月2日」「2026-11-02」「11-02」→ (起, 止)。"""
    td = dt.date.fromtimestamp(now or time.time())
    s = (spec or "").strip()
    rel = {"今天": 0, "昨天": 1, "前天": 2, "大前天": 3}
    if s in rel:
        d = (td - dt.timedelta(days=rel[s])).isoformat()
        return d, d
    m = re.fullmatch(r"(?:最近|近)(\d{1,2})天", s)
    if m:
        return (td - dt.timedelta(days=int(m.group(1)) - 1)).isoformat(), td.isoformat()
    if s in ("这周", "本周"):
        return (td - dt.timedelta(days=td.weekday())).isoformat(), td.isoformat()
    if s == "上周":
        mon = td - dt.timedelta(days=td.weekday() + 7)
        return mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
    m = re.fullmatch(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?", s)
    if m:
        y = int(m.group(1) or td.year)
        try:
            d = dt.date(y, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if not m.group(1) and d > td:
            d = d.replace(year=y - 1)
        return d.isoformat(), d.isoformat()
    return None


def read_diary(con, scope: str, persona: str, spec: str, now: float | None = None) -> str:
    rng = parse_day_spec(spec, now)
    if not rng:
        return "日期没看懂。可以写「昨天」「前天」「上周」「最近7天」「11月2日」或「2026-11-02」。"
    a, b = rng
    td = today(now)
    out = []
    for d in diaries(con, scope, persona, "day", a, b):
        mood = f"（状态：{d['mood']}）" if d.get("mood") else ""
        out.append(f"{day_label(d['day'])}：{d['text']}{mood}")
    covered = {d["day"] for d in diaries(con, scope, persona, "day", a, b)}
    if not out:
        for w in diaries(con, scope, persona, "week", "", b):
            if (w["day_end"] or w["day"]) >= a:
                out.append(f"{day_label(w['day'], False)}那一周：{w['text']}")
    if a <= td <= b and td not in covered:      # 今天的日记可能还没写，直接给最近的原话
        rows = [r for r in day_messages(con, scope, td) if not persona or r["persona"] in ("", persona)][-40:]
        if rows:
            out.append("今天到现在的聊天（原话）：\n" + transcript(rows, scope_kind(scope)))
    return "\n".join(out) if out else f"{a} 到 {b} 没有记录。照实说不记得，不要编。"


# ---------------------------------------------------------------- 控制台用：编辑、恢复、脑图

def edit(con, tbl: str, row_id: int, fields: dict, actor: str = "console") -> dict:
    allowed = {
        "facts": {"text", "category", "tier", "importance", "status"},
        "diaries": {"text", "mood", "status"},
        "followups": {"due", "text", "status"},
    }[tbl]
    old = _row(con.execute(f"SELECT * FROM {tbl} WHERE id=?", (row_id,)).fetchone())
    if not old:
        raise KeyError("没有这一条")
    upd = {k: v for k, v in fields.items() if k in allowed}
    if "tier" in upd and upd["tier"] not in TIERS:
        upd.pop("tier")
    if "category" in upd and upd["category"] not in CATEGORIES:
        upd.pop("category")
    if "due" in upd:
        upd["due"] = _valid_date(upd["due"]) or old["due"]
    if tbl == "diaries" and ("text" in upd or "mood" in upd):
        upd["edited"] = 1
    if tbl == "facts" and upd.get("status") == "active" and old.get("status") != "active":
        upd["last_seen"] = time.time()          # 恢复的事实重新开始计算遗忘
    if not upd:
        return old
    upd["updated"] = time.time()
    con.execute(f"UPDATE {tbl} SET " + ", ".join(f"{k}=?" for k in upd) + " WHERE id=?", (*upd.values(), row_id))
    new = _row(con.execute(f"SELECT * FROM {tbl} WHERE id=?", (row_id,)).fetchone())
    record(con, actor, tbl, row_id, "edit", old, new)
    bump(con)
    con.commit()
    return new


def personas(con) -> list[str]:
    s = {r["persona"] for r in con.execute("SELECT DISTINCT persona FROM diaries WHERE persona != ''")}
    s |= {r["persona"] for r in con.execute("SELECT DISTINCT persona FROM messages WHERE persona != '' AND day >= ?",
                                             ((dt.date.today() - dt.timedelta(days=30)).isoformat(),))}
    return sorted(s)


def scopes(con) -> list[str]:
    s = {r["scope"] for r in con.execute("SELECT DISTINCT scope FROM facts")}
    s |= {r["scope"] for r in con.execute("SELECT DISTINCT scope FROM diaries")}
    s |= {r["scope"] for r in con.execute("SELECT DISTINCT scope FROM messages")}
    order = {"owner": 0, "user": 1, "group": 2}
    return sorted(s, key=lambda x: (order.get(scope_kind(x), 9), x))


def graph(con, persona: str = "", *, now: float | None = None, halflife: dict | None = None,
          show_archived: bool = False, max_tags: int = 60) -> dict:
    """脑图数据。persona 为空时显示所有人设。
    结构：人设（中心）→ 对象（主人 / 群 / 其他人）→ 档案分类 → 档案；对象 → 日记（按日期串起来）；
    档案和日记共同提到的人物、话题作为「标签」节点，把不同的记忆连起来。"""
    now = now or time.time()
    nodes, edges = {}, []

    def node(nid, **kw):
        if nid not in nodes:
            nodes[nid] = {"id": nid, **kw}
        return nid

    plist = [persona] if persona else personas(con)
    tag_links: dict[str, list[str]] = {}
    for p in plist:
        pn = node(f"p:{p}", type="persona", label=p, size=1.0, alpha=1.0)
        used_scopes = {r["scope"] for r in con.execute("SELECT DISTINCT scope FROM diaries WHERE persona=?", (p,))}
        used_scopes |= {r["scope"] for r in con.execute("SELECT DISTINCT scope FROM messages WHERE persona=?", (p,))}
        used_scopes.add(OWNER)
        for sc in sorted(used_scopes):
            sn = node(f"s:{sc}", type="scope", label=scope_title(con, sc), scope=sc, size=0.8, alpha=1.0)
            edges.append({"a": pn, "b": sn, "w": 1.0})
            prev = None
            for d in diaries(con, sc, p, "day") + diaries(con, sc, p, "week"):
                did = node(f"d:{d['id']}", type="diary" if d["kind"] == "day" else "week",
                           label=day_label(d["day"], False) + ("" if d["kind"] == "day" else " 周记"),
                           detail=d["text"], mood=d.get("mood") or "", day=d["day"], row=d["id"], scope=sc, persona=p,
                           size=0.35 if d["kind"] == "day" else 0.5,
                           alpha=max(0.25, 0.5 ** ((now - (d["created"] or now)) / 86400 / 30)))
                edges.append({"a": sn, "b": did, "w": 0.3})
                if prev and d["kind"] == "day":
                    edges.append({"a": prev, "b": did, "w": 0.6, "kind": "time"})
                if d["kind"] == "day":
                    prev = did
                for t in json.loads(d.get("tags") or "[]"):
                    tag_links.setdefault(t, []).append(did)
    # 档案（人设之间共用，挂在对象下面）
    for sid in [n for n in list(nodes) if n.startswith("s:")]:
        sc = nodes[sid]["scope"]
        stat = "status IN ('active','archived')" if show_archived else "status='active'"
        for r in con.execute(f"SELECT * FROM facts WHERE scope=? AND {stat}", (sc,)):
            f = dict(r)
            cid = node(f"c:{sc}:{f['category']}", type="category", label=f["category"], scope=sc, size=0.5, alpha=1.0)
            if not any(e["a"] == sid and e["b"] == cid for e in edges):
                edges.append({"a": sid, "b": cid, "w": 0.8})
            ret = retention(f, now, halflife)
            fid = node(f"f:{f['id']}", type="fact", label=_clip(f["text"], 14), detail=f["text"], row=f["id"], scope=sc,
                       tier=f["tier"], category=f["category"], importance=f["importance"], mentions=f["mentions"],
                       retention=round(ret, 3), status=f["status"], size=0.3 + 0.5 * float(f["importance"] or 0.5),
                       alpha=max(0.12, ret) if f["status"] == "active" else 0.1)
            edges.append({"a": cid, "b": fid, "w": 0.5})
            for t in json.loads(f.get("tags") or "[]"):
                tag_links.setdefault(t, []).append(fid)
        for o in open_followups(con, sc):
            hub = node(f"h:{sc}:follow", type="hub", label="待跟进", scope=sc, size=0.45, alpha=1.0)
            if not any(e["a"] == sid and e["b"] == hub for e in edges):
                edges.append({"a": sid, "b": hub, "w": 0.8})
            oid = node(f"o:{o['id']}", type="followup", label=f"{day_label(o['due'], False)} {_clip(o['text'], 10)}",
                       detail=o["text"], due=o["due"], row=o["id"], scope=sc, status=o["status"], size=0.4, alpha=1.0)
            edges.append({"a": hub, "b": oid, "w": 0.5})
    # 标签：出现两次以上的才画（只出现一次的连不起别的记忆）
    tags = sorted(((t, ids) for t, ids in tag_links.items() if len(set(ids)) >= 2), key=lambda x: -len(set(x[1])))[:max_tags]
    for t, ids in tags:
        tid = node(f"t:{t}", type="tag", label=t, size=0.25 + min(0.5, 0.05 * len(set(ids))), alpha=0.9)
        for i in set(ids):
            edges.append({"a": tid, "b": i, "w": 0.2, "kind": "tag"})
    return {"nodes": list(nodes.values()), "edges": edges, "personas": personas(con)}


def stats(con) -> dict:
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    return {"facts": q("SELECT COUNT(*) FROM facts WHERE status='active'"),
            "archived": q("SELECT COUNT(*) FROM facts WHERE status='archived'"),
            "diaries": q("SELECT COUNT(*) FROM diaries WHERE kind='day' AND status='active' AND text != ''"),
            "weeks": q("SELECT COUNT(*) FROM diaries WHERE kind='week' AND status='active'"),
            "followups": q("SELECT COUNT(*) FROM followups WHERE status IN ('open','asked')"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "last_run": get_meta(con, "last_run", ""), "last_run_msg": get_meta(con, "last_run_msg", "")}


def enqueue(con, kind: str, args: dict | None = None) -> int:
    cur = con.execute("INSERT INTO jobs(ts, kind, args) VALUES(?,?,?)", (time.time(), kind, json.dumps(args or {})))
    con.commit()
    return cur.lastrowid
