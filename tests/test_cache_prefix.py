"""Offline cache regressions: no network, model calls or production writes."""

import ast
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS


ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "app/configpack/plugins"


class TempPart:
    def __init__(self, text):
        self.text = text
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


def load_function(path, name, **env):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    namespace = {"AstrMessageEvent": object, "TextPart": TempPart, **env}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class PrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_mood_moves_after_history_without_duplicate_or_shared_tool_mutation(self):
        handler = load_function(PLUGINS / "astrbot_plugin_dafeiyu_fixes/main.py", "stable_request",
                                copy=copy, GROUP_UNUSED_TOOLS={"llm_view_feed", "llm_publish_feed"})
        shared = NS(tools=[NS(name=n) for n in ("qqprofile_set_status", "memory_search", "llm_view_feed", "kb_search")])
        data = {"_group_chat_plus_request": True, "_group_chat_plus_mood_hint": "MOOD"}
        event = NS(get_group_id=lambda: "group", get_extra=lambda k, d=None: data.get(k, d))
        req = NS(system_prompt="PERSONA\n\n[系统提示-工具提醒开始]\nDUPLICATED_SCHEMA\n[系统提示-工具提醒结束]"
                 "\n(以上是当前会话中你可以调用的所有工具，根据需要选择合适的工具使用)\n\nMOOD",
                 extra_user_content_parts=[TempPart("EXISTING")], func_tool=shared)
        await handler(None, event, req)
        self.assertEqual(req.system_prompt, "PERSONA")
        self.assertEqual([t.name for t in req.func_tool.tools], ["kb_search", "memory_search"])
        self.assertEqual(len(shared.tools), 4)
        self.assertIsNot(req.func_tool, shared)
        self.assertTrue(req.extra_user_content_parts[-1]._no_save)
        await handler(None, event, req)
        self.assertEqual([p.text for p in req.extra_user_content_parts], ["EXISTING", "MOOD"])

    async def test_private_tools_preserved_and_unrelated_mood_text_not_removed(self):
        handler = load_function(PLUGINS / "astrbot_plugin_dafeiyu_fixes/main.py", "stable_request",
                                copy=copy, GROUP_UNUSED_TOOLS={"llm_view_feed", "llm_publish_feed"})
        event = NS(get_group_id=lambda: "", get_extra=lambda *a: "MOOD")
        req = NS(system_prompt="PERSONA MOOD", extra_user_content_parts=[],
                 func_tool=NS(tools=[NS(name="qqprofile_set_status"), NS(name="llm_view_feed")]))
        await handler(None, event, req)
        self.assertEqual(req.system_prompt, "PERSONA MOOD")
        self.assertEqual(len(req.func_tool.tools), 2)

    async def test_group_identity_and_rule_changes_keep_identical_system_prefix(self):
        notes = []
        handler = load_function(PLUGINS / "astrbot_plugin_dafeiyu_common/main.py", "common_hint",
                                RULES=NS(notes=lambda _: notes))
        plugin = NS(_owners=lambda _: ["owner"], _is_owner=lambda e: e.owner)
        requests = []
        for owner in (False, True):
            if owner:
                notes.append({"text": "NEW_GROUP_RULE"})
            event = NS(get_group_id=lambda: "group", owner=owner)
            req = NS(system_prompt="PERSONA", extra_user_content_parts=[])
            await handler(plugin, event, req)
            requests.append(req)
        self.assertEqual(requests[0].system_prompt, requests[1].system_prompt)
        self.assertNotIn("NEW_GROUP_RULE", requests[1].system_prompt)
        self.assertIn("NEW_GROUP_RULE", requests[1].extra_user_content_parts[0].text)
        self.assertNotEqual(requests[0].extra_user_content_parts[0].text, requests[1].extra_user_content_parts[0].text)
        self.assertTrue(all(r.extra_user_content_parts[0]._no_save for r in requests))

    async def test_signature_hint_is_temporary_and_keeps_limits(self):
        handler = load_function(PLUGINS / "astrbot_plugin_dafeiyu_signature/main.py", "mood_hint",
                                MOODS={"在夸你": ["真棒"]}, logger=NS(info=lambda _: None))
        plugin = NS(conf={}, _is_owner=lambda _: True, _self_count_today=lambda: 0)
        req = NS(system_prompt="PERSONA", extra_user_content_parts=[])
        event = NS(message_str="真棒")
        await handler(plugin, event, req)
        await handler(plugin, event, req)
        self.assertEqual(req.system_prompt, "PERSONA")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0]._no_save)
        plugin._self_count_today = lambda: 2
        limited = NS(system_prompt="PERSONA", extra_user_content_parts=[])
        await handler(plugin, event, limited)
        self.assertFalse(limited.extra_user_content_parts)


class MigrationTests(unittest.TestCase):
    def run_migration(self, max_turns=30, trim_turns=1, bad_readback=False):
        conf = {"agent_runner": {"config": {"compression": {
            "max_turns": max_turns, "trim_turns": trim_turns, "overflow_strategy": "llm_compress"}}},
            "unrelated": "KEEP"}
        writes = []

        def call(method, path, body=None):
            if method == "PUT":
                writes.append(copy.deepcopy(body))
                if not bad_readback:
                    conf.update(copy.deepcopy(body))
            return {"config": copy.deepcopy(conf)}

        migrate = load_function(ROOT / "app/plugin_sync.py", "_step4", astrbot_api=NS(call=call), log=lambda _: None)
        migrate()
        return conf, writes, migrate

    def test_old_default_is_batched_and_migration_is_idempotent(self):
        conf, writes, migrate = self.run_migration()
        self.assertEqual(conf["agent_runner"]["config"]["compression"]["trim_turns"], 10)
        self.assertEqual(writes[0]["unrelated"], "KEEP")
        self.assertEqual(writes[0]["agent_runner"]["config"]["compression"]["overflow_strategy"], "llm_compress")
        migrate()
        self.assertEqual(len(writes), 1)

    def test_custom_limits_are_kept(self):
        for max_turns, trim_turns in ((20, 1), (30, 15), (-1, 1)):
            with self.subTest(max_turns=max_turns, trim_turns=trim_turns):
                _, writes, _ = self.run_migration(max_turns, trim_turns)
                self.assertFalse(writes)

    def test_failed_readback_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "未保存"):
            self.run_migration(bad_readback=True)

    def test_pack_and_hook_order(self):
        pack = json.loads((ROOT / "app/configpack/settings/pack.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(pack["base"]["agent_runner.config.compression.trim_turns"], 10)
        tree = ast.parse((PLUGINS / "astrbot_plugin_dafeiyu_fixes/main.py").read_text(encoding="utf-8"))
        hook = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "stable_request")
        priority = ast.literal_eval(hook.decorator_list[0].keywords[0].value)
        self.assertLess(priority, -1)
        self.assertGreater(priority, -10)


if __name__ == "__main__":
    unittest.main()
