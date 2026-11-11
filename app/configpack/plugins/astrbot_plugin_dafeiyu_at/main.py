"""群聊里的 @ 和引用，只在群聊里生效：

1. 回复里的 [at:QQ号] / [At:QQ号|名字] 换成真正的 @；
   [at:all] / [at:全体成员] 换成 @全体成员——只有主人发话触发的回复才换，别人引她 @全体 一律删掉。
   （机器人得是群管理员/群主，QQ 才会真的 @ 到全体，而且每天次数有限。）
2. 群里回复时引用对方那句话（QQ 原生引用），并 @ 对方（可在插件设置里分别关）。
"""

import re

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

AT_RE = re.compile(r"\[\s*at\s*[:：]\s*(\d{5,12}|all|全体成员|全体)(?:\s*\|[^\]]*)?\s*\]", re.I)
ALL_WORDS = {"all", "全体成员", "全体"}


class DafeiyuAt(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.conf = config or {}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        try:
            ids = self.context.get_config(umo=event.unified_msg_origin).get("admins_id") or []
        except Exception:
            ids = []
        return str(event.get_sender_id()) in {str(i) for i in ids}

    @filter.on_decorating_result(priority=99000)
    async def convert_at(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return
        is_group = bool(event.get_group_id())
        allow_all = is_group and self._is_owner(event)
        new_chain, hits, blocked = [], 0, 0
        for comp in result.chain:
            if not isinstance(comp, Plain) or not AT_RE.search(comp.text or ""):
                new_chain.append(comp)
                continue
            pos = 0
            for m in AT_RE.finditer(comp.text):
                before = comp.text[pos:m.start()]
                if before.strip():
                    new_chain.append(Plain(before))
                target = m.group(1).lower()
                if target in ALL_WORDS:
                    if allow_all:
                        new_chain.append(At(qq="all"))
                        hits += 1
                    elif is_group:
                        blocked += 1
                elif is_group:
                    new_chain.append(At(qq=m.group(1)))
                    hits += 1
                pos = m.end()
            rest = comp.text[pos:]
            if rest.strip():
                new_chain.append(Plain(rest))
        if new_chain != result.chain:
            result.chain = new_chain
            if hits:
                logger.info(f"[dafeiyu_at] 回复里有 {hits} 处 @，已换成真正的 @")
            if blocked:
                logger.info(f"[dafeiyu_at] 不是主人叫的，{blocked} 处 @全体成员 删掉了")

    @filter.on_decorating_result(priority=-2000)
    async def quote_reply(self, event: AstrMessageEvent):
        """群里回复时引用触发她回复的那条消息，并 @ 对方。排在语音插件之后，变成语音的回复不加（QQ 语音带不了引用和 @）。"""
        if not event.get_group_id():
            return
        want_quote = self.conf.get("group_quote_reply", True)
        want_at = self.conf.get("group_quote_at", True)
        if not (want_quote or want_at):
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        if any(isinstance(c, Reply) for c in result.chain):
            return  # 已经带了引用（别的插件加的），不重复
        if not all(isinstance(c, (Plain, Image, At)) for c in result.chain):
            return  # 语音等不能带引用
        if not any(isinstance(c, Plain) and (c.text or "").strip() for c in result.chain):
            return  # 只发表情包时不引用
        head = []
        mid = getattr(event.message_obj, "message_id", None)
        if want_quote and mid:
            head.append(Reply(id=mid))
        sender = str(event.get_sender_id() or "")
        if want_at and sender and not any(isinstance(c, At) and str(c.qq) == "all" for c in result.chain):
            # 她自己已经写了 [at:对方] 的，挪到最前面，别 @ 两次
            result.chain = [c for c in result.chain if not (isinstance(c, At) and str(c.qq) == sender)]
            head.append(At(qq=sender, name=event.get_sender_name()))
        result.chain[0:0] = head
