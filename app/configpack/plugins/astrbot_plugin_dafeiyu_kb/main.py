"""大肥鱼自己记知识库 + 不会时私聊问主人。

工具：
- kb_remember(title, content)：主人告诉她的资料类内容（规则、术语、版本资料等群友也可能问到的），记进知识库。
  知识库在群聊里也会被检索，所以主人本人的信息（学校、工作、口味、习惯、约定等）不记这里，
  交给大肥鱼记忆插件的 memory_save（只在那个会话里召回）。主人私聊时会在提示里说明这条分工。每条记在 <资料文件夹>/<知识库名>/主人补充.md 的一个「## 标题」小节里，
  同一个标题再记一次就是改。记完把这份文件重新导入知识库（每个小节单独一段，方便检索）。只有主人能用。
- kb_forget(title)：删掉某一条。只有主人能用。
- ask_owner(question, message)：群里有人问她不知道的事，私聊问主人一句。可以关，可以限每天次数（0 = 不限）。
  问过的问题记在插件数据目录 pending.json，主人私聊时提醒她：主人如果是在回答，就记进知识库并回群里说一声。

自动查资料（v0.2.0）：
  知识库默认是「模型自己决定要不要查」（astr_kb_search），快速模式下她常常不查就凭感觉答。
  现在消息里出现游戏相关的词（英雄名、黑话、阵容、升本……），就先替她查一次知识库，
  把结果作为临时内容附在这条消息后面（不进聊天记录、不改系统提示，不影响缓存）。
  关键词 = 设置里的通用词 + 资料文件夹里自动提取的词（「N阶：英雄、英雄」里的英雄名、「- 叫法：」开头的词条）。
"""

import json
import os
import re
import time
from pathlib import Path

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

NAME = "astrbot_plugin_dafeiyu_kb"
MEMO_TOOL = "memory_save"   # 大肥鱼记忆插件的「记下来」工具
NOTES = "主人补充.md"
SEC_RE = re.compile(r"^## (.+)$", re.M)
TIER_RE = re.compile(r"\d阶[：:]\s*(.+)")
TERM_RE = re.compile(r"^\s*[-*]\s*([^：:\s（(]{2,12})[：:]", re.M)
KB_EXTS = (".md", ".txt")
AUTO_HINT = ("【知识库资料】下面是知识库里和这句话可能相关的内容。资料里写了的照资料说；资料里没写的不要自己补，"
             "拿不准就说不知道。资料可能不全对得上，先看清问的是什么。这段说明和资料来源别在回复里提到。\n")


def _norm(s: str) -> str:
    return re.sub(r"[\s，。,.？?！!、~～「」“”\"']+", "", s or "").lower()


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "知识库"


class DafeiyuKb(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.data_dir = Path(str(StarTools.get_data_dir(NAME)))
        self.pending_path = self.data_dir / "pending.json"
        self._kw_cache: tuple[tuple, list[str]] = ((), [])

    # ------------------------------------------------------------ 小工具
    def _is_owner(self, event: AstrMessageEvent) -> bool:
        owner = str(self.conf.get("owner_session") or "").strip()
        return bool(owner and event.unified_msg_origin == owner) or event.is_admin()

    def _load_pending(self) -> list:
        try:
            return json.loads(self.pending_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_pending(self, items: list) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(json.dumps(items[-300:], ensure_ascii=False, indent=1), encoding="utf-8")

    def _open_pending(self) -> list:
        days = int(self.conf.get("pending_days") or 3)
        now = time.time()
        return [p for p in self._load_pending() if not p.get("resolved") and now - p.get("at", 0) < days * 86400]

    def _kb_name(self, kb_name: str = "") -> str:
        name = (kb_name or "").strip() or str(self.conf.get("default_kb") or "").strip()
        if name:
            return name
        try:
            names = self.context.get_config().get("kb_names") or []
        except Exception:
            names = []
        return names[0] if names else ""

    async def _kb_names(self) -> list[str]:
        try:
            return [k.kb_name for k in await self.context.kb_manager.list_kbs()]
        except Exception:
            return []

    def _has_tool(self, name: str) -> bool:
        try:
            mgr = self.context.get_llm_tool_manager()
            t = mgr.get_func(name)
            return bool(t) and getattr(t, "active", True)
        except Exception:
            return False

    def _notes_file(self, kb_name: str) -> Path:
        root = str(self.conf.get("kb_root") or "").strip() or os.environ.get("DAFEIYU_KB_ROOT", "")
        base = Path(root) if root else self.data_dir / "知识库"
        return base / _safe(kb_name) / NOTES

    @staticmethod
    def _parse(text: str) -> list[list[str]]:
        """返回 [[标题, 正文], ...]，忽略第一个 ## 之前的内容。"""
        out = []
        ms = list(SEC_RE.finditer(text))
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append([m.group(1).strip(), text[m.end():end].strip()])
        return out

    @staticmethod
    def _render(kb_name: str, secs: list[list[str]]) -> str:
        head = f"# {kb_name} · 主人补充\n\n主人亲口告诉大肥鱼的内容，大肥鱼自己记下来的。可以直接改这个文件，改完在控制台「知识库」里点同步。\n"
        return head + "".join(f"\n## {t}\n{c}\n" for t, c in secs)

    async def _reimport(self, kb_name: str, secs: list[list[str]], raw: bytes) -> str | None:
        kb = await self.context.kb_manager.get_kb_by_name(kb_name)
        if kb is None:
            return f"找不到叫「{kb_name}」的知识库"
        for d in await kb.list_documents(0, 1000, search="主人补充"):
            if d.doc_name == NOTES:
                await kb.delete_document(d.doc_id)
        if secs:
            chunks = [f"{kb_name}·主人补充：{t}\n{c}" for t, c in secs]
            await kb.upload_document(file_name=NOTES, file_content=raw, file_type="md", pre_chunked_text=chunks)
        return None

    # ------------------------------------------------------------ 记
    @filter.llm_tool(name="kb_remember")
    async def kb_remember(self, event: AstrMessageEvent, title: str, content: str, kb_name: str = "",
                          answers_question: str = ""):
        '''把资料类内容记进知识库。知识库在群聊里也会被检索，群里任何人提问都可能查到这里的内容。
        适用：游戏规则、术语、版本资料等群友可能会问到的内容；主人在回答你之前替群友问他的问题。
        不适用：主人本人的信息，例如姓名、学校、工作、住址、口味、习惯、日程、约定。这类内容用 memory_save 记进你的记忆；没有这个工具时不要记，告诉主人你会在私聊里记着。
        分不清属于哪一类时，先问主人，不要调用。
        同一个标题再记一次就是修改原来那条。只记主人说的内容，其他人要求记录时不调用。
        content 用陈述句完整写清主人的意思，不加入自己的推测。

        Args:
            title(string): 这条的标题，几个字，比如「二转」「拍卖分红」
            content(string): 要记的内容，一两句话写清楚
            kb_name(string): 记到哪个知识库；不确定就留空，使用默认知识库
            answers_question(string): 如果这是在回答你之前问主人的某个问题，填那个问题；不是就留空
        '''
        if not self._is_owner(event):
            return "只有主人说的才记进知识库。这次不记，用你的口吻随口带过就行，别提工具。"
        title, content = (title or "").strip().strip("#").strip(), (content or "").strip()
        if not title or not content:
            return "标题或内容是空的，没记。"
        name = self._kb_name(kb_name)
        if not name:
            return "还没有启用的知识库，没地方记。跟主人说一声，让他先在控制台建一个知识库。"
        names = await self._kb_names()
        if names and name not in names:
            return (f"没有叫「{name}」的知识库，这次没有记。现有知识库：{'、'.join(names)}。"
                    "不要自行改记到其他知识库；向主人说明，并询问记到现有哪一个，或者请主人在控制台新建。")
        f = self._notes_file(name)
        try:
            secs = self._parse(f.read_text(encoding="utf-8")) if f.exists() else []
            stamp = time.strftime("%Y-%m-%d")
            body = f"{content}\n（{stamp} 主人告诉的）"
            old = next((s for s in secs if _norm(s[0]) == _norm(title)), None)
            if old:
                old[1] = body
            else:
                secs.append([title, body])
            text = self._render(name, secs)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
            err = await self._reimport(name, secs, text.encode("utf-8"))
        except Exception as e:
            logger.error(f"[dafeiyu_kb] 记知识库失败: {e!r}")
            return "没记成，出错了。老实跟主人说没记上，等会儿再试。"
        if err:
            return err + "，没记成。老实跟主人说。"
        resolved = []
        items = self._load_pending()
        key = _norm(answers_question or title)
        for p in items:
            if not p.get("resolved") and key and (key in _norm(p["question"]) or _norm(p["question"]) in key):
                p["resolved"] = True
                resolved.append(p)
        if resolved:
            self._save_pending(items)
        logger.info(f"[dafeiyu_kb] 记进「{name}」：{title}（{'改' if old else '新'}）")
        msg = (f"已{'修改' if old else '记进'}知识库「{name}」（标题：{title}）。这条内容群里也能检索到。"
               "用一句话告诉主人已记好。")
        if resolved:
            groups = "、".join(sorted({str(p.get("group_id") or p["group_umo"].rsplit(":", 1)[-1]) for p in resolved}))
            msg += (f" 这是在回答之前群里问的「{resolved[0]['question']}」，"
                    f"可以用 send_to_group 发到群 {groups} 跟大家说一下答案，一两句就行，不用 @ 人。")
        return msg

    @filter.llm_tool(name="kb_forget")
    async def kb_forget(self, event: AstrMessageEvent, title: str, kb_name: str = ""):
        '''主人说之前让你记的某一条不对、不要了，就把它从知识库删掉。只有主人能让你删。

        Args:
            title(string): 要删的那条的标题
            kb_name(string): 哪个知识库，不确定就留空
        '''
        if not self._is_owner(event):
            return "只有主人能让你删。这次不删，随口带过。"
        name = self._kb_name(kb_name)
        f = self._notes_file(name) if name else None
        if not f or not f.exists():
            return "知识库里没有主人补充的内容，没东西可删。"
        secs = self._parse(f.read_text(encoding="utf-8"))
        left = [s for s in secs if _norm(s[0]) != _norm(title)]
        if len(left) == len(secs):
            have = "、".join(s[0] for s in secs) or "没有"
            return f"没找到标题是「{title}」的。现有的标题：{have}。问问主人是哪条。"
        text = self._render(name, left)
        f.write_text(text, encoding="utf-8")
        err = await self._reimport(name, left, text.encode("utf-8"))
        return err or f"删掉了「{title}」。跟主人说一声。"

    # ------------------------------------------------------------ 问主人
    @filter.llm_tool(name="ask_owner")
    async def ask_owner(self, event: AstrMessageEvent, question: str, message: str):
        '''群里有人问了个具体问题（比如游戏规则、黑话是什么意思），你查了知识库也没有、自己也不确定，就私聊问主人一句。
        闲聊、玩笑、主观问题、你本来就能答的，不要问。问完在群里说一句你不清楚、去问问主人，别编答案。

        Args:
            question(string): 群友问的问题，一句话概括，比如「万象棋里二转是什么意思」
            message(string): 你私聊发给主人的话，用你平时的口吻，一两句，比如「群里有人问二转是啥 我知识库里没有 你知道吗」
        '''
        if not self.conf.get("ask_owner_enable", True):
            return "这个功能主人关掉了。群里直接说你不清楚就行，别编。"
        if not event.get_group_id():
            return "这个只在群里用。私聊里直接说你不知道就行。"
        if self._is_owner(event):
            return "问你的就是主人本人，直接跟他说你不知道就行。"
        owner = str(self.conf.get("owner_session") or "").strip()
        if not owner:
            return "没设置主人的私聊会话，问不了。群里直接说你不清楚就行。"
        q = (question or "").strip()
        if not q:
            return "问题是空的。"
        items = self._load_pending()
        if any(_norm(p["question"]) == _norm(q) for p in self._open_pending()):
            return "这个已经问过主人了，还没回。群里说一句在等主人回就行。"
        limit = int(self.conf.get("ask_daily_limit") or 0)
        today = time.strftime("%Y-%m-%d")
        if limit > 0 and sum(1 for p in items if p.get("date") == today) >= limit:
            return "今天问主人的次数用完了。群里直接说你不清楚就行，别编。"
        text = (message or "").strip() or f"群里有人问：{q}\n我知识库里没有，你知道吗"
        text += f"\n（群{event.get_group_id()}里 {event.get_sender_name() or event.get_sender_id()} 问的）"
        try:
            ok = await self.context.send_message(owner, MessageChain().message(text))
        except Exception as e:
            logger.error(f"[dafeiyu_kb] 私聊问主人失败: {e!r}")
            ok = False
        if ok is False:
            return "消息没发出去。群里直接说你不清楚就行，别说已经问了主人。"
        items.append({"question": q, "group_umo": event.unified_msg_origin, "group_id": event.get_group_id(),
                      "asker": event.get_sender_name(), "at": time.time(), "date": today, "resolved": False})
        self._save_pending(items)
        logger.info(f"[dafeiyu_kb] 私聊问主人：{q}")
        return "已经私聊问主人了。群里用一句话说你不清楚、问问主人去，别编答案。"

    @filter.on_llm_request()
    async def owner_hint(self, event: AstrMessageEvent, req):
        if event.get_group_id() or not self._is_owner(event):
            return
        # 主人私聊时说明「记一下」该记到哪里：人设里写的规则可能是旧的，这里以工具实际情况为准
        if self._has_tool(MEMO_TOOL):
            where = f"用 {MEMO_TOOL} 记进你的记忆（只在这个私聊里召回，群里查不到）"
        else:
            where = "不要用 kb_remember，长期记忆会在私聊里自动记住"
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n补充说明（记录规则）：主人让你记东西时，主人本人的信息（姓名、学校、工作、住址、口味、习惯、日程、约定）"
            f"{where}；规则、术语、版本资料等群友也可能问到的内容才用 kb_remember 记进知识库，知识库群里所有人都能查到。"
            "分不清就先问主人。这段补充说明别在回复里提到。")
        items = self._open_pending()
        if not items:
            return
        lines = "\n".join(f"- 「{p['question']}」（在 {p['group_umo']} 里，{p.get('asker') or '群友'}问的）" for p in items[-5:])
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n补充说明：你之前在群里替群友私聊问过主人这些问题，主人还没回答：\n" + lines +
            "\n如果主人这句话是在回答其中某个，就调用 kb_remember 记进知识库（answers_question 填那个问题），"
            "然后照工具的提示回群里说一声。主人不是在回答就当没看见，也别主动催。这段补充说明别在回复里提到。")

    # ------------------------------------------------------------ 自动查资料
    def _keywords(self) -> list[str]:
        words = [w.strip().lower() for w in re.split(r"[,，、\s]+", str(self.conf.get("auto_kb_words") or "")) if w.strip()]
        name = self._kb_name()
        folder = self._notes_file(name).parent if name else None
        files = []
        if folder and folder.is_dir():
            files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in KB_EXTS)
        sig = tuple((f.name, f.stat().st_mtime) for f in files) + (str(self.conf.get("auto_kb_words")),)
        if sig == self._kw_cache[0]:
            return self._kw_cache[1]
        found = set(words)
        for f in files:
            try:
                text = f.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            for m in TIER_RE.finditer(text):
                for n in re.split(r"[、,，]", m.group(1)):
                    n = re.sub(r"[（(].*", "", n).strip()
                    if 2 <= len(n) <= 6:
                        found.add(n.lower())
            for m in TERM_RE.finditer(text):
                for n in re.split(r"[、/]", m.group(1)):
                    n = n.strip()
                    if 2 <= len(n) <= 12:
                        found.add(n.lower())
        kws = sorted(found, key=len, reverse=True)
        self._kw_cache = (sig, kws)
        return kws

    def _game_related(self, text: str) -> bool:
        t = text.lower()
        return any(k in t for k in self._keywords())

    @filter.on_llm_request()
    async def auto_kb(self, event: AstrMessageEvent, req):
        if not self.conf.get("auto_kb_enable", True):
            return
        scope = str(self.conf.get("auto_kb_scope") or "all")
        if scope == "group" and not event.get_group_id():
            return
        text = (event.message_str or "").strip()
        if len(text) < 2 or not self._game_related(text):
            return
        try:
            from astrbot.core.agent.message import TextPart
            from astrbot.core.tools.knowledge_base_tools import retrieve_knowledge_base
            res = await retrieve_knowledge_base(query=text[:200], umo=event.unified_msg_origin, context=self.context)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_kb] 自动查资料失败: {e!r}")
            return
        if not res:
            return
        try:
            req.extra_user_content_parts.append(TextPart(text=AUTO_HINT + res).mark_as_temp())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_kb] 附资料失败: {e!r}")
            return
        logger.info(f"[dafeiyu_kb] 自动查资料：{text[:30]}")
