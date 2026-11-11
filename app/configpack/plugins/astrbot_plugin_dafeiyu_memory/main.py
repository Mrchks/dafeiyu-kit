"""大肥鱼记忆：日记 + 档案 + 待跟进，常驻在上下文里，后台整理，会遗忘。核心逻辑在 memcore.py。

和 AstrBot 的接线：
- 收到消息（私聊、群聊）和她每次回复，都记一条原文（只用于整理和查询，不放进上下文）。
- 每次调用模型前，把「档案 + 最近几天的日记 + 待跟进 + 相处原则」接在系统提示后面。
  内容只在数据变了或换天时才变，其余时候逐字相同，能命中模型的缓存，几乎不花钱。
- 后台每分钟看一次：
  - 每天凌晨（daily_hour）把前几天还没整理的对话写成日记，更新档案和待跟进；过完的周合并成周记；淡忘、清理旧原文。
  - 今天的对话停下来一阵（idle_minutes）、又攒了足够多新消息，就先写一版今天的日记。
  - 控制台点「立即整理」会往 jobs 表里放一条任务，这里取出来做。
  - 待跟进的事到了日子，傍晚（followup_time 之后）私聊主人问一句。
- 工具：memory_save / memory_forget / memory_search / read_diary / followup_add。

数据在 data/plugin_data/astrbot_plugin_dafeiyu_memory/memory.db，控制台的「记忆」页直接读写这个文件。
主人的档案所有人设共用；日记按人设分开；群里只看得到这个群自己的档案和日记，看不到主人私聊的内容。
"""

import asyncio
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from astrbot import logger
from astrbot.api import sp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memcore as mc  # noqa: E402

NAME = "astrbot_plugin_dafeiyu_memory"
JUNK = re.compile(r"<\s*/?\s*(system_reminder|think|thinking)[^>]*>.*?(<\s*/\s*\1\s*>|$)", re.S | re.I)
SYS_JSON = "你是严谨的记录整理员。只根据给出的记录写，不编造，只输出要求的格式。"


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


class DafeiyuMemory(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = Path(str(StarTools.get_data_dir(NAME)))
        self.con = mc.connect(self.data_dir / "memory.db")
        self._persona: dict[str, tuple[str, float]] = {}      # 会话 → (人设, 记下的时间)
        self._blocks: dict[tuple, tuple[int, str]] = {}        # (对象, 人设, 日期) → (数据版本, 注入内容)
        self._busy = asyncio.Lock()
        self._tick = 0
        self._task = None
        try:
            self._task = asyncio.get_event_loop().create_task(self._loop())
        except Exception as e:  # noqa: BLE001
            logger.error(f"[dafeiyu_memory] 后台任务没起来：{e!r}")

    async def terminate(self):
        if self._task:
            self._task.cancel()
        try:
            self.con.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 配置与身份
    def _c(self, key: str, default):
        try:
            v = self.conf.get(key, default)
        except Exception:
            v = default
        return default if v is None or v == "" else v

    def _halflife(self) -> dict:
        return {"long": float(self._c("halflife_long", 180)), "normal": float(self._c("halflife_normal", 30)),
                "temp": float(self._c("halflife_temp", 3))}

    def _owners(self, umo: str | None = None) -> set[str]:
        try:
            ids = self.context.get_config(umo=umo).get("admins_id") or []
        except Exception:
            ids = []
        return {str(i) for i in ids if _digits(i) and str(i) != "astrbot"}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        return event.is_admin() or str(event.get_sender_id()) in self._owners(event.unified_msg_origin)

    @staticmethod
    def _webchat(event: AstrMessageEvent) -> bool:
        return (event.get_platform_name() or "") == "webchat" or event.unified_msg_origin.startswith("webchat:")

    def _scope(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        if gid:
            return f"g:{gid}"
        if self._webchat(event) or self._is_owner(event):
            return mc.OWNER                      # 控制台的对话测试按主人算（只读，不记原文）
        return f"u:{event.get_sender_id()}"

    async def _resolve_persona(self, event: AstrMessageEvent) -> str:
        """当前会话用的人设：先看这次请求里 AstrBot 选定的，再看会话设置、对话绑定、默认人设。"""
        umo = event.unified_msg_origin
        pid = event.get_extra("_trace_persona_id")
        if pid:
            self._persona[umo] = (pid, time.time())
            return pid
        cached = self._persona.get(umo)
        if cached and time.time() - cached[1] < 600:
            return cached[0]
        pid = ""
        try:
            cfg = await sp.get_async(scope="umo", scope_id=umo, key="session_service_config", default={})
            pid = (cfg or {}).get("persona_id") or ""
            if not pid:
                cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(umo, cid)
                    pid = (conv.persona_id if conv else "") or ""
                    if pid == "[%None]":
                        pid = ""
            if not pid:
                d = await self.context.persona_manager.get_default_persona_v3(umo=umo)
                pid = (d or {}).get("name") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[dafeiyu_memory] 取人设失败：{e!r}")
        pid = pid or "default"
        self._persona[umo] = (pid, time.time())
        return pid

    # ------------------------------------------------------------ 记原文
    @staticmethod
    def _incoming_text(event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if text:
            return text
        kinds = {type(c).__name__ for c in (event.message_obj.message or [])}
        if "Image" in kinds:
            return "[图片]"
        if "Record" in kinds:
            return "[语音]"
        if "Face" in kinds:
            return "[表情]"
        return ""

    @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if self._webchat(event):
                return
            gid = event.get_group_id()
            if gid and not self._c("record_groups", True):
                return
            text = self._incoming_text(event)
            if not text or str(event.get_sender_id()) == str(event.get_self_id()):
                return
            scope = self._scope(event)
            persona = await self._resolve_persona(event)
            owner = self._is_owner(event)
            mc.log_message(self.con, scope=scope, umo=event.unified_msg_origin, persona=persona, role="user", text=text,
                           sender_id=event.get_sender_id(), sender_name=event.get_sender_name(), owner=owner)
            if gid:
                grp = getattr(event.message_obj, "group", None)
                mc.set_name(self.con, scope, getattr(grp, "group_name", "") or "", event.unified_msg_origin)
            elif scope == mc.OWNER:
                mc.set_meta(self.con, "owner_umo", event.unified_msg_origin)
                mc.set_meta(self.con, "owner_persona", persona)
                mc.set_name(self.con, scope, "主人", event.unified_msg_origin)
            else:
                mc.set_name(self.con, scope, event.get_sender_name() or "", event.unified_msg_origin)
            self.con.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_memory] 记原文失败：{e!r}")

    @filter.on_llm_response()
    async def on_reply(self, event: AstrMessageEvent, resp):
        try:
            if self._webchat(event):
                return
            text = JUNK.sub("", getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return
            scope = self._scope(event)
            persona = await self._resolve_persona(event)
            mc.log_message(self.con, scope=scope, umo=event.unified_msg_origin, persona=persona, role="bot", text=text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_memory] 记回复失败：{e!r}")

    # ------------------------------------------------------------ 每轮注入
    def block(self, scope: str, persona: str) -> str:
        key = (scope, persona, mc.today())
        ver = mc.version(self.con)
        hit = self._blocks.get(key)
        if hit and hit[0] == ver:
            return hit[1]
        text = mc.build_block(self.con, scope, persona, profile_budget=int(self._c("profile_budget", 800)),
                              timeline_budget=int(self._c("timeline_budget", 1500)), halflife=self._halflife(),
                              principles=bool(self._c("principles_enable", True)))
        if len(self._blocks) > 200:
            self._blocks.clear()
        self._blocks[key] = (ver, text)
        return text

    @filter.on_llm_request(priority=-10)
    async def inject(self, event: AstrMessageEvent, req):
        if not self._c("enable_inject", True):
            return
        try:
            scope = self._scope(event)
            persona = await self._resolve_persona(event)
            blk = self.block(scope, persona)
            if blk:
                req.system_prompt = (req.system_prompt or "") + blk
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_memory] 组装记忆失败：{e!r}")

    # ------------------------------------------------------------ 工具
    def _write_scope(self, event: AstrMessageEvent) -> tuple[str | None, str]:
        """能不能写、写到哪：主人私聊写主人档案；群里只有主人能让她记群的事；其他人私聊只能记关于自己的事。"""
        if self._webchat(event):
            return None, "这里是控制台的对话测试，不往记忆里写。跟对方说这是测试，不记。"
        scope = self._scope(event)
        if scope.startswith("g:") and not self._is_owner(event):
            return None, "群里只有主人能让你记事。这次不记，用你的口吻随口带过。"
        return scope, ""

    @filter.llm_tool(name="memory_save")
    async def memory_save(self, event: AstrMessageEvent, content: str, category: str = "", keep: str = ""):
        '''把一条关于对方的稳定信息记进你的记忆（档案）。只在私聊或当前群里召回，群里查不到主人私聊里说的事。
        对方明确让你记住时调用，例如学校、工作、口味、习惯、重要的人、约定。日程类、有日期的事用 followup_add。
        规则、术语、版本资料这类群友也会问到的资料，用 kb_remember 记进知识库，不用这个。
        不记猜测、玩笑和一时的情绪。记完用一句话说记好了。

        Args:
            content(string): 要记的内容，陈述句，30 字以内，比如「主人周末一般在宿舍打游戏」
            category(string): 分类：身份、偏好、习惯、人际、经历、健康、计划、称呼、群规、其他。不确定就留空
            keep(string): 能记多久：永久（生日、称呼这类不会变的）、长期（默认）、一般（近期情况）、临时（几天内有效）
        '''
        scope, why = self._write_scope(event)
        if not scope:
            return why
        tier = {"永久": "permanent", "长期": "long", "一般": "normal", "临时": "temp"}.get((keep or "").strip(), "long")
        persona = await self._resolve_persona(event)
        fid, kind = mc.add_fact(self.con, scope, content, category=(category or "其他").strip(), tier=tier,
                                persona=persona, actor="tool", evidence=event.message_str or "")
        logger.info(f"[dafeiyu_memory] 记下（{scope}）：{content}")
        return "这条之前就记着，已经加深了印象。" if kind == "dup" else "记好了。用一句话告诉对方记住了。"

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, keyword: str):
        '''对方说之前让你记的某件事不对、不要记了，就把它从记忆里删掉。

        Args:
            keyword(string): 那件事里的关键词，比如「宿舍」「土豆」
        '''
        scope, why = self._write_scope(event)
        if not scope:
            return why
        gone = mc.forget_facts(self.con, scope, keyword, actor="tool")
        if not gone:
            return f"记忆里没有包含「{keyword}」的内容。问问对方是哪一条。"
        return "删掉了：" + "；".join(f["text"] for f in gone) + "。用一句话告诉对方忘掉了。"

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, keyword: str):
        '''按关键词在你的记忆里查：档案、日记和最近一个月的原话。对方问起你不确定的往事时先查，查不到就说不记得。

        Args:
            keyword(string): 关键词，越具体越好，比如「考试」「生日」「猫」
        '''
        scope = self._scope(event)
        persona = await self._resolve_persona(event)
        hits = mc.search(self.con, scope, keyword, persona)
        if not hits:
            return f"记忆里没查到「{keyword}」。照实说不记得，不要编。"
        return "查到这些（照着说，不要添油加醋）：\n" + "\n".join(hits)

    @filter.llm_tool(name="read_diary")
    async def read_diary(self, event: AstrMessageEvent, date: str):
        '''翻日记：看某一天或某几天发生了什么。对方问「昨天聊了什么」「上周怎么样」「11月2日那天」时调用，照日记回答，没有就说不记得。

        Args:
            date(string): 「今天」「昨天」「前天」「这周」「上周」「最近7天」「11月2日」或「2026-11-02」
        '''
        scope = self._scope(event)
        persona = await self._resolve_persona(event)
        return mc.read_diary(self.con, scope, persona, date)

    @filter.llm_tool(name="followup_add")
    async def followup_add(self, event: AstrMessageEvent, date: str, content: str):
        '''记下一件有日期、以后要关心的事（考试、面试、出行、看病、约定）。到了那天你会想起来问一句。只在私聊里用。

        Args:
            date(string): 日期，YYYY-MM-DD；对方说「下周三」这种，先换算成具体日期，拿不准就问对方是几号
            content(string): 什么事，20 字以内，比如「内科考试」
        '''
        if event.get_group_id():
            return "群里不记这种事。"
        scope, why = self._write_scope(event)
        if not scope:
            return why
        persona = await self._resolve_persona(event)
        fid = mc.add_followup(self.con, scope, date, content, persona=persona)
        if not fid:
            return "日期没看懂，没记上。问对方具体是几号。"
        return f"记下了：{date} {content}。到时候你会想起来问。用一句话告诉对方。"

    # ------------------------------------------------------------ 后台
    async def _loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[dafeiyu_memory] 后台整理出错：{e!r}")
            await asyncio.sleep(60)

    async def _step(self):
        self._tick += 1
        # 控制台放进来的任务
        for job in self.con.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id").fetchall():
            self.con.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
            self.con.commit()
            try:
                args = json.loads(job["args"] or "{}")
                if job["kind"] == "consolidate":
                    msg = await self.run_daily(include_today=bool(args.get("today", True)), source="手动")
                elif job["kind"] == "followup_now":
                    msg = await self._followups(force=True)
                else:
                    msg = f"不认识的任务：{job['kind']}"
                self.con.execute("UPDATE jobs SET status='done', result=?, finished=? WHERE id=?", (msg, time.time(), job["id"]))
            except Exception as e:  # noqa: BLE001
                self.con.execute("UPDATE jobs SET status='error', result=?, finished=? WHERE id=?", (repr(e), time.time(), job["id"]))
            self.con.commit()
        now = dt.datetime.now()
        td = mc.today()
        if now.hour >= int(self._c("daily_hour", 4)) and mc.get_meta(self.con, "last_daily") != td:
            mc.set_meta(self.con, "last_daily", td)
            self.con.commit()
            await self.run_daily(include_today=False, source="每日")
        if self._tick % 10 == 0:            # 每 10 分钟看看今天的对话有没有停下来
            for scope, persona, day in mc.idle_today(self.con, td, int(self._c("idle_minutes", 90)),
                                                     int(self._c("min_new_messages", 8))):
                await self._consolidate(scope, persona, day)
        if self._tick % 5 == 0:
            await self._followups()

    def _budget_ok(self) -> bool:
        k = f"llm:{mc.today()}"
        n = int(mc.get_meta(self.con, k, "0") or 0)
        if n >= int(self._c("daily_llm_limit", 80)):
            return False
        mc.set_meta(self.con, k, n + 1)
        self.con.commit()
        return True

    def _provider(self, scope: str = ""):
        pid = str(self._c("provider_id", "")).strip()
        if pid:
            p = self.context.get_provider_by_id(pid)
            if p:
                return p
        umo = ""
        if scope:
            r = self.con.execute("SELECT umo FROM names WHERE scope=?", (scope,)).fetchone()
            umo = (r["umo"] if r else "") or ""
        try:
            return self.context.get_using_provider(umo or None)
        except Exception:
            return self.context.get_using_provider()

    async def _ask(self, scope: str, prompt: str, system: str = SYS_JSON) -> str:
        prov = self._provider(scope)
        if prov is None:
            raise RuntimeError("没有可用的对话模型")
        if not self._budget_ok():
            raise RuntimeError("今天整理用的模型调用次数到上限了")
        resp = await prov.text_chat(prompt=prompt, system_prompt=system)
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _consolidate(self, scope: str, persona: str, day: str) -> dict | None:
        built = mc.build_day_prompt(self.con, scope, persona, day, halflife=self._halflife())
        if not built:
            return None
        prompt, said = built
        rows = mc.day_messages(self.con, scope, day, persona)
        result = None
        for _ in range(2):
            result = mc.parse_json(await self._ask(scope, prompt))
            if result:
                break
        if not result:
            logger.warning(f"[dafeiyu_memory] {scope} {persona} {day} 整理结果不是 JSON，跳过")
            return None
        st = mc.apply_day(self.con, scope, persona, day, result, said, upto_ts=max(r["ts"] for r in rows),
                          msg_count=len(rows))
        logger.info(f"[dafeiyu_memory] 整理 {scope} · {persona} · {day}：{st}")
        return st

    async def run_daily(self, include_today: bool = False, source: str = "") -> str:
        if self._busy.locked():
            return "已经在整理了"
        async with self._busy:
            td = mc.today()
            done, err = 0, 0
            todo = mc.pending_days(self.con, td, lookback=int(self._c("lookback_days", 3)))
            if include_today:
                todo += mc.idle_today(self.con, td, 0, 1)
            for scope, persona, day in todo:
                try:
                    if await self._consolidate(scope, persona, day):
                        done += 1
                except Exception as e:  # noqa: BLE001
                    err += 1
                    logger.warning(f"[dafeiyu_memory] 整理 {scope} {persona} {day} 失败：{e!r}")
            weeks = 0
            for scope, persona, mon, sun in mc.weeks_to_merge(self.con):
                p = mc.build_week_prompt(self.con, scope, persona, mon, sun)
                if not p:
                    continue
                try:
                    text = await self._ask(scope, p, "你是严谨的记录整理员。只根据给出的日记写，不编造。")
                    if text:
                        mc.save_week(self.con, scope, persona, mon, sun, text)
                        weeks += 1
                except Exception as e:  # noqa: BLE001
                    err += 1
                    logger.warning(f"[dafeiyu_memory] 合并周记失败：{e!r}")
            m = mc.maintain(self.con, halflife=self._halflife())
            pruned = mc.prune_messages(self.con, int(self._c("keep_raw_days", 30)))
            msg = (f"{source}整理：写了 {done} 篇日记、{weeks} 篇周记，淡忘 {m['archived']} 条档案，"
                   f"过期 {m['expired']} 件待跟进，清理 {pruned} 条旧原文" + (f"，{err} 处失败（看日志）" if err else ""))
            mc.set_meta(self.con, "last_run", time.strftime("%Y-%m-%d %H:%M"))
            mc.set_meta(self.con, "last_run_msg", msg)
            self.con.commit()
            logger.info(f"[dafeiyu_memory] {msg}")
            return msg

    # ------------------------------------------------------------ 约定跟进：到了日子主动问一句
    async def _followups(self, force: bool = False) -> str:
        if not self._c("followup_enable", True) and not force:
            return "没开"
        now = dt.datetime.now()
        hm = now.strftime("%H:%M")
        if not force and not (str(self._c("followup_time", "19:30")) <= hm <= str(self._c("followup_end", "22:30"))):
            return "不在时段内"
        td = mc.today()
        sent = int(mc.get_meta(self.con, f"fu_sent:{td}", "0") or 0)
        if not force and sent >= int(self._c("followup_daily_limit", 2)):
            return "今天已经问够了"
        umo = mc.get_meta(self.con, "owner_umo", "")
        persona = mc.get_meta(self.con, "owner_persona", "") or "default"
        if not umo:
            return "还不知道主人的私聊会话"
        lo = (now.date() - dt.timedelta(days=2)).isoformat()
        due = [f for f in mc.open_followups(self.con, mc.OWNER) if f["status"] == "open" and lo <= f["due"] <= td]
        if not due:
            return "今天没有要跟进的事"
        last = self.con.execute("SELECT MAX(ts) m FROM messages WHERE scope=? AND role='user'", (mc.OWNER,)).fetchone()["m"]
        if not force and last and time.time() - last < 30 * 60:
            return "主人正在聊，这时候她自己会提"
        pm = self.context.persona_manager.get_persona_v3_by_id(persona) or {}
        system = (pm.get("prompt") or "") + self.block(mc.OWNER, persona)
        things = "；".join(f"{f['due']} {f['text']}" for f in due[:2])
        prompt = (f"现在是 {now.strftime('%m月%d日 %H:%M')}。你想主动私聊主人，关心一下这件事：{things}。"
                  "用你平时的口吻发一条消息，自然地问问结果或关心一下，一到三句，每句单独一行。"
                  "不要提到「记录」「待跟进」「提醒」这些词。只输出要发的话。")
        text = JUNK.sub("", await self._ask(mc.OWNER, prompt, system)).strip()
        if not text:
            return "没生成出来"
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
        for i, ln in enumerate(lines):
            if i:
                await asyncio.sleep(1.5)
            await self.context.send_message(umo, MessageChain().message(ln))
        mc.log_message(self.con, scope=mc.OWNER, umo=umo, persona=persona, role="bot", text="\n".join(lines))
        for f in due[:2]:
            self.con.execute("UPDATE followups SET status='asked', asked_at=?, updated=? WHERE id=?",
                             (time.time(), time.time(), f["id"]))
        mc.set_meta(self.con, f"fu_sent:{td}", sent + 1)
        mc.bump(self.con)
        self.con.commit()
        logger.info(f"[dafeiyu_memory] 主动问了主人：{things}")
        return f"问了：{things}"
