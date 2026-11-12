"""思考程度：界面档位与各服务商参数的换算、「配置 → 档位 → 配置」往返、已有连接未改动时不产生配置变化。
不联网、不调 AstrBot（用假的接口）、不写正在用的 state.json。"""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import modelhub as hub  # noqa: E402

# 各预设每个档位应写入的参数（2026-11-12 核对官方文档）：
#   OpenAI gpt-6-luna：reasoning_effort none/low/medium/high（Responses 适配器转为 reasoning.effort），默认 medium
#   Anthropic claude-haiku-5-5：thinking adaptive + output_config.effort，由 AstrBot 的 anth_thinking_config 生成，默认 medium
#   DeepSeek：thinking.type enabled/disabled，reasoning_effort low/high/max，默认 enabled + high
#   Kimi kimi-k3.1-pro：始终思考，reasoning_effort low/high/max，默认 max
#   Gemini 3.8 Flash：thinking level low/medium/high（不支持 minimal），AstrBot 未设置时按 HIGH 发送
#   Grok 4.7：不能关闭思考，reasoning_effort low/medium/high/xhigh，默认 high
#   Qwen qwen3.7-plus：enable_thinking，默认开启
#   Doubao seed-2-1-pro：thinking.type enabled/disabled，reasoning_effort minimal…max，默认 high
#   MiniMax M3.1-Flash：始终思考，reasoning_effort low/medium/high/xhigh/max，默认 max
#   MiMo v2.6 / LongCat-2.5：thinking.type enabled/disabled
#   GLM glm-5.3-flash：始终思考，不提供档位
EXPECTED = {
    "openai": {"off": {"reasoning_effort": "none"}, "low": {"reasoning_effort": "low"}, "medium": {"reasoning_effort": "medium"}, "high": {"reasoning_effort": "high"}},
    "anthropic": {lv: {"anth_thinking_config": {"type": "adaptive", "effort": lv}} for lv in ("low", "medium", "high")},
    "deepseek": {"off": {"thinking": {"type": "disabled"}}, "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
                 "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
    "kimi": {"low": {"reasoning_effort": "low"}, "medium": {"reasoning_effort": "high"}, "high": {"reasoning_effort": "max"}},
    "gemini": {lv: {"gm_thinking_config": {"level": lv.upper()}} for lv in ("low", "medium", "high")},
    "grok": {lv: {"reasoning_effort": lv} for lv in ("low", "medium", "high")},
    "qwen": {"off": {"enable_thinking": False}, "on": {"enable_thinking": True}},
    "doubao": {"off": {"thinking": {"type": "disabled"}}, **{lv: {"thinking": {"type": "enabled"}, "reasoning_effort": lv} for lv in ("low", "medium", "high")}},
    "minimax": {"low": {"reasoning_effort": "low"}, "medium": {"reasoning_effort": "medium"}, "high": {"reasoning_effort": "max"}},
    "mimo": {"off": {"thinking": {"type": "disabled"}}, "on": {"thinking": {"type": "enabled"}}},
    "longcat": {"off": {"thinking": {"type": "disabled"}}, "on": {"thinking": {"type": "enabled"}}},
}
DEFAULTS = {"openai": "medium", "anthropic": "medium", "deepseek": "high", "kimi": "high", "gemini": "high", "grok": "high",
            "qwen": "on", "doubao": "high", "minimax": "high", "mimo": "on", "longcat": "on"}


def presets():
    return {p["id"]: p for p in hub.catalog()["presets"]}


class ThinkingMapTests(unittest.TestCase):
    def test_every_preset_level_matches_documented_parameters(self):
        for pid, levels in EXPECTED.items():
            spec = hub.thinking_spec(pid, presets()[pid]["protocol"])
            self.assertEqual(set(spec["levels"]), set(levels), pid)
            for level, params in levels.items():
                with self.subTest(preset=pid, level=level):
                    extra, prov = hub.thinking_apply(spec, level, {}, {})
                    got = extra if spec["where"] == "extra" else prov
                    self.assertEqual(got, params)

    def test_round_trip_config_level_config(self):
        cat = hub.catalog()
        specs = [(p["id"], p["thinking"]) for p in cat["presets"] if p.get("thinking") and not p["thinking"].get("always")]
        specs += [("generic-" + k, v) for k, v in cat["thinking_generic"].items()]
        base_extra, base_prov = {"temperature": 0.7, "max_tokens": 900}, {"id": "x/y", "model": "y", "modalities": ["text"]}
        for name, spec in specs:
            for level in spec["levels"]:
                with self.subTest(spec=name, level=level):
                    extra, prov = hub.thinking_apply(spec, level, base_extra, base_prov)
                    self.assertEqual(hub.thinking_parse(spec, extra, prov), level)
                    again = hub.thinking_apply(spec, hub.thinking_parse(spec, extra, prov), extra, prov)
                    self.assertEqual(again, (extra, prov))
                    self.assertEqual(extra.get("temperature"), 0.7)          # 其他参数不受影响
                    self.assertEqual(prov.get("model"), "y")

    def test_no_parameter_shows_vendor_default(self):
        for pid, default in DEFAULTS.items():
            spec = hub.thinking_spec(pid, presets()[pid]["protocol"])
            self.assertEqual(hub.thinking_parse(spec, {}, {}), default, pid)
        spec = hub.thinking_spec("anthropic", "anthropic")
        self.assertEqual(hub.thinking_parse(spec, {}, {"anth_thinking_config": {"type": "", "budget": 0, "effort": ""}}), "medium")

    def test_unrecognized_parameters_show_custom(self):
        ds = hub.thinking_spec("deepseek", "openai")
        self.assertEqual(hub.thinking_parse(ds, {"thinking": {"type": "enabled"}}, {}), "custom")
        self.assertEqual(hub.thinking_parse(ds, {"thinking_budget": 1024}, {}), "custom")
        kimi = hub.thinking_spec("kimi", "openai")
        self.assertEqual(hub.thinking_parse(kimi, {"reasoning_effort": "medium"}, {}), "custom")
        self.assertEqual(hub.thinking_parse(kimi, {"reasoning": {"effort": "low"}}, {}), "custom")
        self.assertIsNone(hub.thinking_parse(hub.thinking_spec("glm", "openai"), {"thinking": {"type": "enabled"}}, {}))

    def test_changed_protocol_uses_generic_spec(self):
        spec = hub.thinking_spec("deepseek", "anthropic")
        self.assertEqual(spec, hub.catalog()["thinking_generic"]["anthropic"])
        self.assertEqual(hub.thinking_parse(spec, {}, {}), "auto")

    def test_unsupported_level_is_rejected(self):
        with self.assertRaises(ValueError):
            hub.thinking_apply(hub.thinking_spec("kimi", "openai"), "off", {}, {})


class ConnectionThinkingTests(unittest.TestCase):
    old_source = {"id": "s", "api_base": "https://api.moonshot.cn/v1", "key": ["SAVED_SECRET"], "custom_headers": {}}

    def test_new_connection_writes_selected_level(self):
        _, cfg = hub.connection({"preset": "deepseek", "key": "fake", "thinking": "off", "extra_body": {"temperature": 1}}, "t")
        self.assertEqual(cfg["custom_extra_body"], {"temperature": 1, "thinking": {"type": "disabled"}})
        _, cfg = hub.connection({"preset": "gemini", "key": "fake", "thinking": "low"}, "t")
        self.assertEqual(cfg["gm_thinking_config"], {"level": "LOW"})
        self.assertNotIn("gm_thinking_config", cfg["custom_extra_body"])

    def test_unchanged_edit_keeps_existing_parameters(self):
        old_model = {"id": "s/kimi-k3.1-pro", "provider_source_id": "s", "model": "kimi-k3.1-pro", "enable": True,
                     "modalities": ["text", "tool_use", "image"], "custom_extra_body": {"reasoning_effort": "low", "top_p": 0.9}}
        body = {"id": old_model["id"], "preset": "kimi", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3.1-pro",
                "protocol": "openai", "vision": True, "extra_body": {"top_p": 0.9}, "thinking": "keep", "name": "Kimi"}
        _, cfg = hub.connection(body, "s", old_model["id"], copy.deepcopy(self.old_source), copy.deepcopy(old_model))
        self.assertEqual(cfg["custom_extra_body"], old_model["custom_extra_body"])
        # 未发送 thinking（自定义或不提供档位）时，其他参数原样保存
        body2 = {**body, "extra_body": {"reasoning": {"effort": "low"}}}
        body2.pop("thinking")
        _, cfg = hub.connection(body2, "s", old_model["id"], copy.deepcopy(self.old_source), copy.deepcopy(old_model))
        self.assertEqual(cfg["custom_extra_body"], {"reasoning": {"effort": "low"}})

    def test_choice_overrides_same_parameter_in_extra(self):
        _, cfg = hub.connection({"preset": "kimi", "key": "fake", "thinking": "high", "extra_body": {"reasoning_effort": "low"}}, "t")
        self.assertEqual(cfg["custom_extra_body"], {"reasoning_effort": "max"})

    def test_provider_level_keep_preserves_old_config(self):
        old_model = {"id": "s/claude-haiku-5-5", "provider_source_id": "s", "model": "claude-haiku-5-5", "enable": True,
                     "modalities": ["text"], "custom_extra_body": {}, "anth_thinking_config": {"type": "adaptive", "effort": "low"}}
        body = {"id": old_model["id"], "preset": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
                "protocol": "anthropic", "extra_body": {}, "thinking": "keep"}
        src = {"id": "s", "api_base": "https://api.anthropic.com", "key": ["K"], "custom_headers": {}}
        _, cfg = hub.connection(body, "s", old_model["id"], src, copy.deepcopy(old_model))
        self.assertEqual(cfg["anth_thinking_config"], {"type": "adaptive", "effort": "low"})


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        view = {"models": [{"id": "a/x", "source_id": "a"}, {"id": "b/y", "source_id": "b"}, {"id": "b/z", "source_id": "b"}],
                "default_chat": "a/x", "default_image": "", "sessions": [{"label": "群聊 1", "provider": "b/z", "inherited": False}]}
        self.state = {"model_checks": {"b/y": {"ok": True}}}
        for p in (patch.object(hub, "snapshot", lambda: copy.deepcopy(view)),
                  patch.object(hub.ab, "call", lambda m, path, *a, **k: self.calls.append((m, path)) or {}),
                  patch.object(hub, "load_state", lambda: copy.deepcopy(self.state)),
                  patch.object(hub, "update_state", lambda **kw: self.state.update(kw))):
            p.start()
            self.addCleanup(p.stop)

    def test_in_use_connection_is_not_deleted(self):
        for pid, word in (("a/x", "默认聊天模型"), ("b/z", "群聊 1")):
            r = hub.delete(pid)
            self.assertFalse(r["ok"])
            self.assertIn(word, r["message"])
        self.assertEqual(self.calls, [])

    def test_delete_keeps_shared_source_and_clears_record(self):
        r = hub.delete("b/y")
        self.assertTrue(r["ok"])
        self.assertEqual([m for m, _ in self.calls], ["DELETE"])         # 连接 b 仍被 b/z 使用，只删除型号
        self.assertNotIn("b/y", self.state["model_checks"])


if __name__ == "__main__":
    unittest.main()
