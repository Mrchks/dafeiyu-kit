"""用量统计：只读 AstrBot 的 provider_stats；一行可能累计一次回复内多轮工具调用的用量。

AstrBot 后台自带的统计接口只能看最近 1 / 3 / 7 天，而且按「会话」排行：网页里的「对话测试」每开一次新对话
就是一个新会话，排行里会出现一串以后台登录名命名的会话。这里改成按「人 / 群」合并：
- QQ 私聊按 QQ 号合并，群聊按群号合并（机器人平台改过名字、开过「群里每人单独会话」也算同一个群）；
- 网页里的对话测试全部合成一行「对话测试」。
另外可以看任意一个人或群有史以来的全部用量。

只读（query_only），不会改 AstrBot 的数据；AstrBot 正在写的时候最多等几秒。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core import astrbot_data, load_state

DB_OVERRIDE: Path | None = None   # 测试用：指定数据库文件


class UsageError(Exception):
    pass


def _db_path() -> Path | None:
    if DB_OVERRIDE:
        return DB_OVERRIDE
    d = astrbot_data()
    return d / "data_v4.db" if d else None


def _connect() -> sqlite3.Connection:
    p = _db_path()
    if not p or not p.exists():
        raise UsageError("还没有用量数据：AstrBot 装好、她回过消息之后这里才有记录。")
    # AstrBot 的库是 WAL 模式，只读模式（mode=ro）在 AstrBot 停着时可能打不开；普通方式打开 + query_only 一样不会写
    con = sqlite3.connect(str(p), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    try:
        con.execute("SELECT 1 FROM provider_stats LIMIT 1")
    except sqlite3.Error:
        con.close()
        raise UsageError("这个版本的 AstrBot 还没有用量记录表（provider_stats），更新 AstrBot 后才能统计。")
    return con


# ---------------------------------------------------------------- 会话 → 人 / 群

def target_of(umo: str) -> tuple[str, str, str]:
    """会话 ID（平台名:消息类型:会话号）→（合并键, 种类, 号码）。种类：user 私聊 / group 群聊 / test 对话测试 / other。"""
    parts = (umo or "").split(":", 2)
    if len(parts) < 3:
        return "o:" + (umo or "unknown"), "other", umo or ""
    plat, typ, sid = parts
    if plat == "webchat" or sid.startswith("webchat!"):
        return "test", "test", ""
    if typ == "GroupMessage":
        gid = sid.rsplit("_", 1)[-1] if "_" in sid else sid      # 群里每人单独会话时是「QQ号_群号」
        return "g:" + gid, "group", gid
    if typ == "FriendMessage":
        return "u:" + sid, "user", sid
    return f"o:{typ}:{sid}", "other", sid


def _names(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """合并键 → (名字, 更新时间)。名字来自 AstrBot 记的群名 / 昵称（用户在后台改过的别名优先）。"""
    out: dict[str, tuple[str, str]] = {}
    try:
        rows = con.execute("SELECT umo, auto_name, user_alias, updated_at FROM umo_aliases").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        key, _kind, num = target_of(r["umo"])
        name = (r["user_alias"] or r["auto_name"] or "").strip()
        if not name or name == num:
            continue
        at = str(r["updated_at"] or "")
        if key not in out or at >= out[key][1]:
            out[key] = (name, at)
    return out


def _label(kind: str, num: str, name: str) -> str:
    if kind == "test":
        return "对话测试"
    return name or num or "未知会话"


# ---------------------------------------------------------------- 时间

def _utc(dt_local: datetime) -> str:
    """本地时间 → 数据库里存的 UTC 字符串（AstrBot 存的是不带时区的 UTC 时间）。"""
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "")[:26])
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone()


def _range_start(rng: str) -> datetime | None:
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if rng == "today":
        return today
    if rng in ("7", "30"):
        return today - timedelta(days=int(rng) - 1)
    return None


def _cost(tokens: int):
    # Prices differ by provider, cache writes, cache reads and output. Never multiply one universal rate.
    return None


TOK = "(token_input_other + token_input_cached + token_output)"


def _daily(con, where: str, args: list) -> dict[str, int]:
    rows = con.execute(f"SELECT date(created_at, 'localtime') d, SUM({TOK}) t FROM provider_stats "
                       f"WHERE {where} GROUP BY d", args).fetchall()
    return {r["d"]: int(r["t"] or 0) for r in rows if r["d"]}


def _day_points(daily: dict[str, int], start: date, end: date) -> tuple[str, list[dict]]:
    """按天补齐；跨度超过两个月按周合并，柱子不会细成一条线。"""
    span = (end - start).days + 1
    pts = []
    if span <= 62:
        for i in range(span):
            d = start + timedelta(days=i)
            k = d.isoformat()
            pts.append({"label": f"{d.month}/{d.day}", "tip": f"{d.month}月{d.day}日", "v": daily.get(k, 0)})
        return "day", pts
    d = start - timedelta(days=start.weekday())          # 从那周的周一开始
    while d <= end:
        v = sum(daily.get((d + timedelta(days=i)).isoformat(), 0) for i in range(7))
        e = d + timedelta(days=6)
        pts.append({"label": f"{d.month}/{d.day}", "tip": f"{d.month}月{d.day}日–{e.month}月{e.day}日", "v": v})
        d += timedelta(days=7)
    return "week", pts


# ---------------------------------------------------------------- 对外

def overview(rng: str = "7") -> dict:
    """某段时间（today / 7 / 30 / all）的总量、趋势和按人 / 群的排行。"""
    if rng not in ("today", "7", "30", "all"):
        rng = "7"
    start = _range_start(rng)
    where, args = "agent_type = 'internal'", []
    if start:
        where += " AND created_at >= ?"
        args.append(_utc(start))
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT umo, COUNT(*) n, SUM(token_input_other) i, SUM(token_input_cached) c, SUM(token_output) o, "
            f"MAX(created_at) last FROM provider_stats WHERE {where} GROUP BY umo", args).fetchall()
        names = _names(con)
        owners = set(str(x) for x in load_state().get("owners") or [])
        merged: dict[str, dict] = {}
        tot = {"i": 0, "c": 0, "o": 0, "n": 0}
        for r in rows:
            key, kind, num = target_of(r["umo"])
            i, c, o, n = int(r["i"] or 0), int(r["c"] or 0), int(r["o"] or 0), int(r["n"] or 0)
            for k, v in (("i", i), ("c", c), ("o", o), ("n", n)):
                tot[k] += v
            t = merged.setdefault(key, {"key": key, "kind": kind, "id": num, "tokens": 0, "calls": 0, "last": ""})
            t["tokens"] += i + c + o
            t["calls"] += n
            t["last"] = max(t["last"], str(r["last"] or ""))
        targets = []
        for t in merged.values():
            t["name"] = _label(t["kind"], t["id"], names.get(t["key"], ("", ""))[0])
            t["owner"] = t["kind"] == "user" and t["id"] in owners
            lt = _local(t["last"])
            t["last"] = lt.strftime("%Y-%m-%d %H:%M") if lt else ""
            targets.append(t)
        targets.sort(key=lambda x: x["tokens"], reverse=True)

        # 趋势
        if rng == "today":
            hrs = con.execute(f"SELECT CAST(strftime('%H', created_at, 'localtime') AS INTEGER) h, SUM({TOK}) t "
                              f"FROM provider_stats WHERE {where} GROUP BY h", args).fetchall()
            hv = {int(r["h"]): int(r["t"] or 0) for r in hrs if r["h"] is not None}
            unit, pts = "hour", [{"label": f"{h}点", "tip": f"{h}:00–{h}:59", "v": hv.get(h, 0)} for h in range(24)]
        else:
            daily = _daily(con, where, args)
            end = datetime.now().astimezone().date()
            if start:
                first = start.date()
            else:
                first = min((date.fromisoformat(d) for d in daily), default=end)
            unit, pts = _day_points(daily, first, end)
        first_any = con.execute("SELECT MIN(created_at) m FROM provider_stats WHERE agent_type = 'internal'").fetchone()["m"]
        model_rows = con.execute(
            f"SELECT COALESCE(provider_model,provider_id) m, SUM(token_input_other) i, "
            f"SUM(token_input_cached) c, SUM(token_output) o, COUNT(*) n FROM provider_stats WHERE {where} GROUP BY m",
            args).fetchall()
    finally:
        con.close()
    total = tot["i"] + tot["c"] + tot["o"]
    fa = _local(first_any)
    return {"range": rng, "total": total, "calls": tot["n"], "input": tot["i"], "cached": tot["c"], "output": tot["o"],
            "avg": round(total / tot["n"]) if tot["n"] else 0, "cost": _cost(total),
            "cache_rate": round(tot["c"] / (tot["i"] + tot["c"]) * 100, 2) if tot["i"] + tot["c"] else None,
            "cache_models": [{"model": m["m"], "uncached": int(m["i"] or 0), "cached": int(m["c"] or 0),
                              "output": int(m["o"] or 0), "records": int(m["n"] or 0),
                              "rate": round((m["c"] or 0) / ((m["i"] or 0) + (m["c"] or 0)) * 100, 2)
                              if (m["i"] or 0) + (m["c"] or 0) else None} for m in model_rows],
            "since": fa.strftime("%Y-%m-%d") if fa else "", "trend": {"unit": unit, "points": pts}, "targets": targets}


def target(key: str) -> dict:
    """一个人或一个群有史以来的全部用量。"""
    con = _connect()
    try:
        umos = [r["umo"] for r in con.execute("SELECT DISTINCT umo FROM provider_stats WHERE agent_type = 'internal'")]
        mine = [u for u in umos if target_of(u)[0] == key]
        names = _names(con)
        _k, kind, num = target_of(mine[0]) if mine else (key, "other", "")
        if not mine:
            kind = {"u": "user", "g": "group", "test": "test"}.get(key.split(":", 1)[0], "other")
            num = key.split(":", 1)[1] if ":" in key else ""
        out = {"key": key, "kind": kind, "id": num, "name": _label(kind, num, names.get(key, ("", ""))[0]),
               "owner": kind == "user" and num in set(str(x) for x in load_state().get("owners") or []),
               "total": 0, "calls": 0, "input": 0, "cached": 0, "output": 0, "cost": None, "avg": 0,
               "first": "", "last": "", "active_days": 0, "share": 0.0, "recent": [], "models": []}
        end = datetime.now().astimezone().date()
        if not mine:
            out["recent"] = _day_points({}, end - timedelta(days=29), end)[1]
            return out
        ph = ",".join("?" * len(mine))
        where, args = f"agent_type = 'internal' AND umo IN ({ph})", list(mine)
        r = con.execute(f"SELECT COUNT(*) n, SUM(token_input_other) i, SUM(token_input_cached) c, SUM(token_output) o, "
                        f"MIN(created_at) f, MAX(created_at) l FROM provider_stats WHERE {where}", args).fetchone()
        i, c, o, n = int(r["i"] or 0), int(r["c"] or 0), int(r["o"] or 0), int(r["n"] or 0)
        all_tot = con.execute(f"SELECT SUM({TOK}) t FROM provider_stats WHERE agent_type = 'internal'").fetchone()["t"] or 0
        daily = _daily(con, where, args)
        models = con.execute(f"SELECT COALESCE(provider_model, provider_id) m, SUM({TOK}) t, COUNT(*) n FROM provider_stats "
                             f"WHERE {where} GROUP BY m ORDER BY t DESC", args).fetchall()
    finally:
        con.close()
    total = i + c + o
    f, l = _local(r["f"]), _local(r["l"])
    out.update(total=total, calls=n, input=i, cached=c, output=o, cost=_cost(total), avg=round(total / n) if n else 0,
               first=f.strftime("%Y-%m-%d") if f else "", last=l.strftime("%Y-%m-%d %H:%M") if l else "",
               active_days=sum(1 for v in daily.values() if v > 0),
               share=round(total / all_tot * 100, 1) if all_tot else 0.0,
               recent=_day_points(daily, end - timedelta(days=29), end)[1],
               models=[{"model": m["m"] or "未知模型", "tokens": int(m["t"] or 0), "calls": int(m["n"] or 0)} for m in models])
    return out
