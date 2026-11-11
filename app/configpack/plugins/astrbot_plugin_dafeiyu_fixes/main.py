"""大肥鱼兼容修复。

1. 单独指定的人设不生效
   AstrBot 自己回消息时，优先用「会话规则」里给这个群/私聊指定的人设。
   但群聊增强（group_chat_plus）、主动聊天（proactive_chat）、记忆（livingmemory）等插件
   取人设时调用的是 persona_manager.get_default_persona_v3(umo)，它只看默认人设，
   于是出现「群里指定了大肥鱼，默认是小柚，群里却用小柚的口吻说话」。
   这里把 get_default_persona_v3 包一层：这个会话有单独指定的人设，就返回它；没有就照旧。
   AstrBot 核心自己不调用这个函数，所以不影响核心的选人设逻辑。

2. 群里没轮到她回话的图片，她看不到
   群聊增强插件里，没通过概率筛选的消息先存进缓存，等她下次开口时一起作为上下文。
   其中的图片要靠「平台图片描述提取」转成文字，但这一步读的是旧版 AstrBot 的 ltm 对象，
   新版核心已经改名为 group_chat_context，所以永远取不到描述，纯图片消息被直接丢掉。
   结果是别人发的题目截图她完全没看见，只能根据别人打的字去猜。
   这里把 PlatformLTMHelper.extract_image_caption_from_platform 包一层：原逻辑取不到时，
   用群聊插件配置的「图片转文字提供商」（没填就用 AstrBot 的「图片转述模型」）自己转成文字，
   格式和原逻辑一致（[图片内容: …]），并沿用群聊插件的图片描述缓存，同一张图不重复花钱。
"""

import copy
import hashlib
import json
import sys

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

GCP_NAME = "astrbot_plugin_group_chat_plus"
GROUP_UNUSED_TOOLS = {"llm_view_feed", "llm_publish_feed"}


class ScopedImageCache:
    """Reuse captions only when model and instructions match."""

    def __init__(self, cache, fingerprint):
        self.cache, self.fingerprint = cache, fingerprint

    @property
    def enabled(self):
        return self.cache.enabled

    def lookup(self, url):
        return self.cache.lookup(f"dafeiyu-caption-v1:{self.fingerprint}:{url}")

    def save(self, url, description):
        return self.cache.save(f"dafeiyu-caption-v1:{self.fingerprint}:{url}", description)


class DafeiyuFixes(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._seen: dict[str, str] = {}
        self._orig = None
        pm = getattr(context, "persona_manager", None)
        if pm is None or not hasattr(pm, "get_default_persona_v3"):
            logger.warning("[dafeiyu_fixes] 找不到 persona_manager，跳过人设修复")
            return
        if getattr(pm, "_dafeiyu_orig_default_persona", None) is not None:
            self._orig = pm._dafeiyu_orig_default_persona   # 插件重载过，沿用最初的原函数
        else:
            self._orig = pm.get_default_persona_v3
            pm._dafeiyu_orig_default_persona = self._orig
        orig = self._orig

        async def get_default_persona_v3(umo=None):
            if umo:
                key = str(umo)
                try:
                    cfg = await sp.get_async(scope="umo", scope_id=key, key="session_service_config", default={}) or {}
                    pid = cfg.get("persona_id")
                    if pid:
                        persona = pm.get_persona_v3_by_id(pid)
                        if persona:
                            if self._seen.get(key) != pid:
                                self._seen[key] = pid
                                logger.info(f"[dafeiyu_fixes] {key} 用单独指定的人设「{pid}」")
                            return persona
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[dafeiyu_fixes] 读会话人设失败 {key}: {e!r}")
            return await orig(umo)

        pm.get_default_persona_v3 = get_default_persona_v3
        logger.info("[dafeiyu_fixes] 已启用：群聊/主动聊天/记忆插件也会用单独指定的人设")

    # ---------- 2. 没轮到她回话的图片也转成文字 ----------

    def _gcp_instance(self):
        try:
            for md in self.context.get_all_stars():
                if getattr(md, "name", "") == GCP_NAME and getattr(md, "activated", True):
                    return getattr(md, "star_cls", None)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _ensure_image_patch(self) -> None:
        helper = image_handler = None
        for name, mod in list(sys.modules.items()):
            if GCP_NAME not in name or mod is None:
                continue
            if helper is None and name.endswith("platform_ltm_helper"):
                helper = getattr(mod, "PlatformLTMHelper", None)
            if image_handler is None and name.endswith("image_handler"):
                image_handler = getattr(mod, "ImageHandler", None)
        if helper is None or image_handler is None:
            return
        if not getattr(image_handler, "_dafeiyu_cache_patched", False):
            original_convert = image_handler._convert_images_to_text

            async def convert(message_chain, context, provider_id, prompt, image_components, timeout,
                              image_description_cache=None, session_id="", self_id=None):
                if image_description_cache is not None:
                    cfg = context.get_config()
                    model = next((p for p in cfg.get("provider", []) if p.get("id") == provider_id), {})
                    source = next((p for p in cfg.get("provider_sources", [])
                                   if p.get("id") == model.get("provider_source_id")), {})
                    signature = [provider_id, model.get("model"), source.get("type"), source.get("api_base"), prompt]
                    fingerprint = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:24]
                    image_description_cache = ScopedImageCache(image_description_cache, fingerprint)
                return await original_convert(message_chain, context, provider_id, prompt, image_components, timeout,
                                              image_description_cache, session_id, self_id=self_id)

            image_handler._convert_images_to_text = staticmethod(convert)
            image_handler._dafeiyu_cache_patched = True
        if getattr(helper, "_dafeiyu_patched", False):
            return
        orig = helper.extract_image_caption_from_platform
        fixes = self

        async def extract_image_caption_from_platform(context, event, original_text, *args, **kwargs):
            try:
                ok, text = await orig(context, event, original_text, *args, **kwargs)
                if ok and text:
                    return ok, text
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[dafeiyu_fixes] 原图片描述提取出错: {e!r}")
            return await fixes._caption_myself(context, event, image_handler)

        helper.extract_image_caption_from_platform = staticmethod(extract_image_caption_from_platform)
        helper._dafeiyu_patched = True
        logger.info("[dafeiyu_fixes] 已启用：群里没轮到她回话的图片也会转成文字放进上下文")

    async def _caption_myself(self, context, event, image_handler):
        gcp = self._gcp_instance()
        provider_id = getattr(gcp, "image_to_text_provider_id", "") if gcp else ""
        if not provider_id:
            try:
                cfg = context.get_config(umo=event.unified_msg_origin)
                provider_id = cfg.get("provider_settings", {}).get("default_image_caption_provider_id", "")
            except Exception:  # noqa: BLE001
                provider_id = ""
        if not provider_id:
            return False, None
        try:
            ok, text, _urls, _kept = await image_handler.process_message_images(
                event,
                context,
                True,
                "all",
                provider_id,
                getattr(gcp, "image_to_text_prompt", "") or "请用中文简要描述这张图片的内容，图里有文字就把文字原样写出来",
                False,
                False,
                getattr(gcp, "image_to_text_timeout", 60) or 60,
                getattr(gcp, "image_description_cache", None),
                getattr(gcp, "max_images_per_message", 10) or 10,
                self_id=str(event.get_self_id()),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[dafeiyu_fixes] 图片转文字失败: {e!r}")
            return False, None
        if ok and text and "[图片内容:" in text:
            logger.info(f"[dafeiyu_fixes] 已把图片转成文字: {text[:80]}")
            return True, text
        return False, None

    @filter.on_llm_request(priority=-2)
    async def stable_request(self, event: AstrMessageEvent, req):
        """Stabilize request prefixes after group-chat rewriting, before memory.

        Args:
            event: The current chat event.
            req: The provider-facing request; shared tool registries stay intact.
        """
        if event.get_group_id():
            mood = event.get_extra("_group_chat_plus_mood_hint", None)
            suffix = f"\n\n{mood}" if mood else ""
            if suffix and (req.system_prompt or "").endswith(suffix):
                req.system_prompt = req.system_prompt[:-len(suffix)]
                if not any(getattr(p, "text", None) == mood for p in req.extra_user_content_parts):
                    req.extra_user_content_parts.append(TextPart(text=mood).mark_as_temp())
            # The provider already receives full tool schemas. Remove only the
            # marked duplicate reminder, keeping all surrounding instructions.
            if event.get_extra("_group_chat_plus_request", False):
                start, end = "[系统提示-工具提醒开始]", "[系统提示-工具提醒结束]"
                before, found, rest = (req.system_prompt or "").partition(start)
                if found and end in rest:
                    _, _, after = rest.partition(end)
                    tail = "\n(以上是当前会话中你可以调用的所有工具，根据需要选择合适的工具使用)"
                    if after.startswith(tail):
                        after = after[len(tail):]
                    req.system_prompt = before.rstrip("\n") + after
        tools = getattr(req, "func_tool", None)
        if tools is not None and hasattr(tools, "tools"):
            selected = list(tools.tools)
            if event.get_group_id():
                selected = [t for t in selected if t.name not in GROUP_UNUSED_TOOLS
                            and not t.name.startswith("qqprofile_")]
            req.func_tool = copy.copy(tools)
            req.func_tool.tools = sorted(selected, key=lambda t: t.name)

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
        self._ensure_image_patch()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def _on_group_message(self, event: AstrMessageEvent):
        # 群聊插件重载后类会换新，这里每条群消息顺手确认一次（已打过补丁时几乎不耗时）
        self._ensure_image_patch()

    async def terminate(self):
        pm = getattr(self.context, "persona_manager", None)
        if pm is not None and self._orig is not None:
            pm.get_default_persona_v3 = self._orig
