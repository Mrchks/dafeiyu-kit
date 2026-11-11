"""Offline regressions: no QQ connection, provider calls, or production database writes."""

import importlib.util
import ast
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "app/configpack/plugins/astrbot_plugin_dafeiyu_common"


def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj


class Filter:
    EventMessageType = NS(GROUP_MESSAGE=1)

    def __getattr__(self, name):
        return lambda *args, **kw: lambda fn: fn


class Part:
    def __init__(self, text="", **kw):
        self.text = text
        self.__dict__.update(kw)


class Star:
    def __init__(self, context):
        self.context = context


module("astrbot", logger=logging.getLogger("test"))
module("astrbot.api")
module(
    "astrbot.api.event",
    AstrMessageEvent=object,
    MessageChain=lambda **kw: NS(**kw),
    filter=Filter(),
)
module("astrbot.api.message_components", At=Part, Plain=Part)
module("astrbot.api.star", Context=object, Star=Star)
web = module("astrbot.api.web", request=NS(json=AsyncMock()))
module("astrbot.core")
module("astrbot.core.agent")
module("astrbot.core.agent.message", Message=lambda **kw: NS(**kw), TextPart=Part, ThinkPart=Part)
module("astrbot.core.utils")
paths = module("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: "")
package = module("test_common")
package.__path__ = [str(PLUGIN)]
spec = importlib.util.spec_from_file_location("test_common.main", PLUGIN / "main.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class FakeContext:
    def __init__(self):
        self.sent = []
        self.generated = []
        self.scopes = []
        self.rules = {
            "bot:GroupMessage:10000": "群搭子",
            "bot:FriendMessage:123456": "小柚",
        }
        self.personas = [
            {"name": "小柚", "prompt": "YUZU_STYLE"},
            {"name": "群搭子", "prompt": "GROUP_STYLE"},
        ]
        self.persona_manager = NS(
            personas_v3=self.personas,
            get_persona_v3_by_id=self.persona,
            resolve_selected_persona=self.resolve,
        )
        self.conversation_manager = NS(
            get_curr_conversation_id=AsyncMock(return_value=None)
        )
        self.client = NS(call_action=AsyncMock(side_effect=self.action))
        platform = NS(
            meta=lambda: NS(id="bot", name="aiocqhttp"), get_client=lambda: self.client
        )
        self.platform_manager = NS(platform_insts=[platform])
        self.llm_generate = AsyncMock(side_effect=self.generate)

    def persona(self, pid):
        return next((p for p in self.personas if p["name"] == pid), None)

    async def resolve(self, *, umo, **kw):
        p = self.persona(self.rules.get(umo, "小柚"))
        return p["name"], p, None, False

    async def get_current_chat_provider_id(self, umo):
        return "group-think" if ":GroupMessage:" in umo else "private-plain"

    def get_all_providers(self):
        return [
            NS(meta=lambda: NS(id="private-plain")),
            NS(meta=lambda: NS(id="group-think")),
        ]

    def get_config(self, **kw):
        return {
            "admins_id": ["123456"],
            "platform_settings": {"id_whitelist": ["bot:GroupMessage:10000"]},
        }

    def register_web_api(self, *args):
        pass

    def get_all_stars(self):
        def block(scope, persona):
            self.scopes.append((scope, persona))
            return "OWNER_SECRET" if scope == "owner" else "GROUP_MEMORY"

        return [
            NS(
                name="astrbot_plugin_dafeiyu_memory",
                activated=True,
                star_cls=NS(block=block),
            )
        ]

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True

    async def generate(self, **kw):
        self.generated.append(kw)
        return NS(completion_text="群里自然说话")

    async def action(self, action, **kw):
        if action == "get_group_list":
            return [{"group_id": "10000", "group_name": "测试群"}]
        raise AssertionError(action)


def event(sender="123456"):
    return NS(
        is_admin=lambda: False,
        get_sender_id=lambda: sender,
        get_self_id=lambda: "888888",
        get_platform_id=lambda: "bot",
        get_group_id=lambda: "",
        bot=None,
        unified_msg_origin="bot:FriendMessage:123456",
        message_str="看看你在群里说过什么",
    )


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        paths.get_astrbot_data_path = lambda: self.tmp.name
        # dialogue imported the same callable by value; keep it in sync with the fixture.
        sys.modules["test_common.dialogue"].get_astrbot_data_path = lambda: (
            self.tmp.name
        )
        common.RULES = common.GroupRules()
        self.ctx = FakeContext()
        self.plugin = common.DafeiyuCommon(self.ctx)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def test_default_delivery_uses_destination_persona_and_model(self):
        result = await self.plugin.send_to_group(event(), "10000", "问大家今晚几点")
        self.assertIn("群搭子", result)
        self.assertEqual(self.ctx.generated[0]["chat_provider_id"], "group-think")
        self.assertIn("GROUP_STYLE", self.ctx.generated[0]["system_prompt"])
        self.assertNotIn("OWNER_SECRET", self.ctx.generated[0]["system_prompt"])
        self.assertEqual(self.ctx.sent[0][0], "bot:GroupMessage:10000")

    async def test_verbatim_preserves_all_lines_spaces_and_markers(self):
        original = "  原话\n\n[at:123456]\n三\n四\n五\n六\n七  "
        await self.plugin.send_to_group(event(), "10000", original, mode="verbatim")
        self.ctx.llm_generate.assert_not_called()
        self.assertEqual(self.ctx.sent[0][1].chain[-1].text, original)

    async def test_explicit_persona_is_public_and_does_not_rebind_group(self):
        await self.plugin.send_to_group(
            event(), "10000", "向大家打招呼", mode="persona", persona="小柚"
        )
        self.assertIn("YUZU_STYLE", self.ctx.generated[0]["system_prompt"])
        self.assertIn("QQ群公开", self.ctx.generated[0]["system_prompt"])
        self.assertNotIn("OWNER_SECRET", self.ctx.generated[0]["system_prompt"])
        self.assertEqual(self.ctx.rules["bot:GroupMessage:10000"], "群搭子")

    async def test_non_owner_cannot_send(self):
        await self.plugin.send_to_group(event("654321"), "10000", "发句话")
        self.assertFalse(self.ctx.sent)
        self.ctx.llm_generate.assert_not_called()

    async def test_failed_generation_never_falls_back_to_source_text(self):
        self.ctx.llm_generate.side_effect = RuntimeError("provider failed")
        result = await self.plugin.send_to_group(event(), "10000", "私聊风格的内容")
        self.assertIn("没有发送", result)
        self.assertFalse(self.ctx.sent)

    async def test_silent_group_is_checked_before_generation(self):
        common.RULES.mute("10000", 60)
        await self.plugin.send_to_group(event(), "10000", "发句话")
        self.assertFalse(self.ctx.sent)
        self.ctx.llm_generate.assert_not_called()

    async def test_history_pages_back_and_matches_qq_not_nickname(self):
        newest = [
            {
                "message_id": str(100 + i),
                "time": 2000 + i,
                "sender": {"user_id": "123456", "nickname": "小柚"},
                "raw_message": "群友的话",
            }
            for i in range(15)
        ]
        earlier = [
            {
                "message_id": "90",
                "time": 1000,
                "sender": {"user_id": "888888", "nickname": "旧角色"},
                "raw_message": "机器人说过的话",
            }
        ]

        async def action(name, **kw):
            if name == "get_group_list":
                return [{"group_id": "10000"}]
            if kw.get("message_seq"):
                self.assertTrue(kw.get("reverse_order"))
            return {"messages": earlier if kw.get("message_seq") else newest}

        self.ctx.client.call_action.side_effect = action
        result = await self.plugin.group_history(event(), "10000", "1")
        self.assertIn("机器人自己：机器人说过的话", result)
        self.assertNotIn("机器人自己：群友的话", result)
        self.assertIn("扫描16条", result)
        self.assertIn("没查到不等于从未", result)

    async def test_history_stops_when_qq_repeats_same_page(self):
        async def action(name, **kw):
            if name == "get_group_list":
                return [{"group_id": "10000"}]
            return {
                "messages": [
                    {"message_id": "1", "time": 2000, "sender": {"user_id": "123456"}}
                ]
            }

        self.ctx.client.call_action.side_effect = action
        result = await self.plugin.group_history(event(), "10000")
        self.assertIn("本次覆盖范围内没有查到", result)
        self.assertLessEqual(self.ctx.client.call_action.await_count, 3)

    async def test_group_simulation_cannot_read_owner_rows_or_send(self):
        p = Path(self.tmp.name) / "plugin_data/astrbot_plugin_dafeiyu_memory/memory.db"
        p.parent.mkdir(parents=True)
        with sqlite3.connect(p) as db:
            db.execute(
                "create table messages(id integer,scope text,role text,sender_name text,text text)"
            )
            db.executemany(
                "insert into messages values(?,?,?,?,?)",
                [
                    (1, "owner", "user", "主人", "PRIVATE_ROW"),
                    (2, "g:10000", "user", "群友", "PUBLIC_ROW"),
                ],
            )
        db.close()
        before = p.read_bytes()
        await self.plugin.dialogue.generate(
            "bot:GroupMessage:10000", "今天忙坏了", simulation=True
        )
        prompt = self.ctx.generated[0]["system_prompt"]
        self.assertIn("PUBLIC_ROW", prompt)
        self.assertNotIn("PRIVATE_ROW", prompt)
        self.assertNotIn("OWNER_SECRET", prompt)
        self.assertEqual(before, p.read_bytes())
        self.assertFalse(self.ctx.sent)

    async def test_options_report_effective_profiles(self):
        options = await self.plugin.dialogue.options()
        self.assertEqual(options["scenes"][0]["persona"], "小柚")
        self.assertEqual(options["scenes"][1]["persona"], "群搭子")
        self.assertEqual(options["scenes"][1]["provider"], "group-think")
        self.assertNotIn("prompt", options["scenes"][0])

    async def test_test_api_rejects_unknown_destination_before_model_call(self):
        web.request.json.return_value = {
            "umo": "bot:GroupMessage:99999",
            "text": "hello",
        }
        result = await self.plugin.dialogue.api_chat()
        self.assertEqual(result["status"], "error")
        self.ctx.llm_generate.assert_not_called()

    async def test_private_simulation_reads_owner_but_cannot_send(self):
        await self.plugin.dialogue.generate(
            "bot:FriendMessage:123456", "你好", identity="owner", simulation=True
        )
        self.assertIn("OWNER_SECRET", self.ctx.generated[0]["system_prompt"])
        self.assertFalse(self.ctx.sent)

    async def test_test_chat_preserves_provider_metadata_without_returning_it(self):
        self.ctx.llm_generate = AsyncMock(return_value=NS(
            completion_text="public reply", reasoning_content="PRIVATE_PROVIDER_METADATA",
            reasoning_signature="SIGNED_METADATA"))
        dialogue = self.plugin.dialogue
        first = await dialogue.generate("bot:FriendMessage:123456", "first", simulation=True, session="isolated-test")
        self.assertNotIn("PRIVATE_PROVIDER_METADATA", str(first))
        await dialogue.generate("bot:FriendMessage:123456", "second", simulation=True, session="isolated-test")
        contexts = self.ctx.llm_generate.call_args.kwargs["contexts"]
        self.assertEqual(contexts[1].content[0].think, "PRIVATE_PROVIDER_METADATA")
        self.assertEqual(contexts[1].content[0].encrypted, "SIGNED_METADATA")
        await dialogue.generate("bot:GroupMessage:10000", "group", simulation=True, session="isolated-test")
        self.assertEqual(self.ctx.llm_generate.call_args.kwargs["contexts"], [])
        self.assertFalse(self.ctx.sent)

    async def test_history_scanning_is_bounded(self):
        page = 0

        async def action(name, **kw):
            nonlocal page
            if name == "get_group_list":
                return [{"group_id": "10000"}]
            page += 1
            return {
                "messages": [
                    {
                        "message_id": str(page * 100 + i),
                        "time": 10000 - page * 100 + i,
                        "sender": {"user_id": "123456"},
                    }
                    for i in range(80)
                ]
            }

        self.ctx.client.call_action.side_effect = action
        result = await self.plugin.group_history(event(), "10000")
        self.assertEqual(page, 5)
        self.assertIn("扫描400条", result)
        self.assertIn("上限", result)


class StatusTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "app/services.py").read_text(encoding="utf-8"))
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "status_snapshot"
        )
        self.env = {
            "time": NS(time=lambda: 100),
            "load_state": lambda: {"setup_done": True},
            "_st_cache": {"data": None, "at": 0},
            "job": {
                k: None for k in ("name", "running", "msg", "ok", "at", "progress")
            },
        }
        exec(
            compile(ast.Module(body=[node], type_ignores=[]), "services.py", "exec"),
            self.env,
        )

    def test_cold_status_returns_loading_instead_of_probing(self):
        self.assertTrue(self.env["status_snapshot"]()["loading"])

    def test_stale_status_remains_visible_and_marked(self):
        self.env["_st_cache"] = {
            "data": {"setup_done": True, "astrbot": {"running": True}},
            "at": 80,
        }
        result = self.env["status_snapshot"]()
        self.assertTrue(result["stale"])
        self.assertTrue(result["astrbot"]["running"])
        self.assertEqual(result["sampled_at"], 80)


if __name__ == "__main__":
    unittest.main()
