"""Model configuration regressions; all provider mutations use a fake API."""

import copy
import json
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import modelhub as hub
import usage


class FakeAPI:
    def __init__(self):
        self.sources = {"old": {"id": "old", "type": "openai_chat_completion", "key": ["TEST_SECRET"],
                                "api_base": "https://example.test/v1", "custom_headers": {"x-token": "HEADER_SECRET"}}}
        self.providers = {"old/chat": {"id": "old/chat", "provider_source_id": "old", "model": "chat",
                                       "modalities": ["text", "image"], "enable": True}}
        self.config = {"agent_runner": {"config": {"model": {"provider_id": "old/chat"}}},
                       "provider_settings": {"default_image_caption_provider_id": "old/chat"},
                       "platform_settings": {"id_whitelist": []}}
        self.group = {"image_to_text_provider_id": "old/chat", "image_to_text_prompt": "previous", "keep": 123}
        self.mutations = []
        self.fail_create = False
        self.fail_group_once = False

    def __call__(self, method, path, body=None, **kw):
        if method != "GET":
            self.mutations.append((method, path, copy.deepcopy(body)))
        if path == "/api/v1/provider-sources":
            return {"provider_sources": copy.deepcopy(list(self.sources.values()))}
        if path.startswith("/api/v1/providers?"):
            return {"providers": copy.deepcopy(list(self.providers.values()))}
        if path == "/api/v1/config-profiles/default":
            if method == "PUT":
                self.config = copy.deepcopy(body)
            return {"config": copy.deepcopy(self.config)}
        if path.startswith("/api/v1/providers/by-id?"):
            return {"provider": copy.deepcopy(self.providers["old/chat"])}
        if path.startswith("/api/v1/provider-sources/by-id?"):
            if method == "DELETE":
                self.sources.pop(path.split("source_id=")[1], None)
                return {}
            return {"provider_source": copy.deepcopy(self.sources["old"])}
        if path == "/api/v1/provider-sources/by-id":
            self.sources[body["source_id"]] = copy.deepcopy(body["config"])
            return {}
        if path in ("/api/v1/providers/by-id", "/api/v1/provider-sources/providers"):
            if self.fail_create and method == "POST":
                raise RuntimeError("mock failure")
            self.providers[body["config"]["id"]] = copy.deepcopy(body["config"])
            return {}
        if path.startswith("/api/v1/plugins/config"):
            if method == "PUT":
                self.group = copy.deepcopy(body["config"])
                if self.fail_group_once:
                    self.fail_group_once = False
                    raise RuntimeError("mock partial failure")
            return {"config": copy.deepcopy(self.group)}
        raise AssertionError((method, path))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAPI()
        self.api = patch.object(hub.ab, "call", self.fake)
        self.api.start()
        self.data = patch.object(hub, "astrbot_data", return_value=None)
        self.data.start()
        self.addCleanup(self.api.stop)
        self.addCleanup(self.data.stop)

    def test_all_twelve_presets_build_with_key_only(self):
        presets = hub.catalog()["presets"]
        self.assertEqual(len(presets), 13)
        for preset in presets:
            if preset["id"] == "custom":
                continue
            with self.subTest(preset=preset["id"]):
                source, model = hub.connection({"preset": preset["id"], "key": "fake"}, "test")
                self.assertEqual(source["type"], hub.PROTOCOLS[preset["protocol"]][1])
                self.assertEqual(model["model"], preset["model"])
                self.assertIn("image", model["modalities"])

    def test_snapshot_never_returns_keys_or_header_values(self):
        self.fake.sources["old"]["api_base"] = "https://user:URL_SECRET@example.test/v1?key=QUERY_SECRET"
        output = json.dumps(hub.snapshot())
        for secret in ("TEST_SECRET", "HEADER_SECRET", "URL_SECRET", "QUERY_SECRET"):
            self.assertNotIn(secret, output)
        self.assertEqual(hub._safe_json({"max_tokens": 200, "api_key": "secret"}),
                         {"max_tokens": 200, "api_key": "[已隐藏]"})

    def test_add_does_not_change_live_assignments(self):
        before = copy.deepcopy(self.fake.config)
        result = hub.save({"preset": "anthropic", "key": "fake"})
        self.assertTrue(result["ok"])
        self.assertEqual(before, self.fake.config)

    def test_blank_key_edit_preserves_key_and_headers(self):
        hub.save({"id": "old/chat", "preset": "custom", "base_url": "https://example.test/v1", "model": "new-chat"})
        self.assertEqual(self.fake.sources["old"]["key"], ["TEST_SECRET"])
        self.assertEqual(self.fake.sources["old"]["custom_headers"], {"x-token": "HEADER_SECRET"})

    def test_address_change_requires_new_key(self):
        with self.assertRaises(ValueError):
            hub.save({"id": "old/chat", "preset": "custom", "base_url": "https://another.test/v1", "model": "chat"})
        self.assertEqual(self.fake.mutations, [])

    def test_failed_create_removes_only_new_source(self):
        self.fake.fail_create = True
        with self.assertRaises(ValueError):
            hub.save({"preset": "gemini", "key": "fake"})
        self.assertEqual(set(self.fake.sources), {"old"})
        self.assertEqual(set(self.fake.providers), {"old/chat"})

    def test_vision_assignment_restores_both_configs_on_partial_failure(self):
        before, group = copy.deepcopy(self.fake.config), copy.deepcopy(self.fake.group)
        self.fake.fail_group_once = True
        with self.assertRaises(ValueError):
            hub.assign({"role": "image", "provider": "old/chat"})
        self.assertEqual(self.fake.config, before)
        self.assertEqual(self.fake.group, group)

    def test_text_model_cannot_be_assigned_to_images(self):
        self.fake.providers["old/chat"]["modalities"] = ["text"]
        with self.assertRaises(ValueError):
            hub.assign({"role": "image", "provider": "old/chat"})
        self.assertEqual(self.fake.mutations, [])

    def test_known_text_only_models_do_not_acquire_image_capability(self):
        _, model = hub.connection({"preset": "deepseek", "key": "fake", "model": "deepseek-v4-pro", "vision": True}, "test")
        self.assertNotIn("image", model["modalities"])

    def test_invalid_addresses_are_rejected_before_mutation(self):
        for address in ("file:///C:/temp", "https://host/?api_key=secret", "https://user:pass@host/v1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                hub.save({"preset": "custom", "base_url": address, "model": "test", "key": "fake"})
        self.assertEqual(self.fake.mutations, [])


class UsageTests(unittest.TestCase):
    def test_cache_ratio_uses_input_tokens_and_excludes_external_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE provider_stats(umo TEXT,agent_type TEXT,provider_model TEXT,provider_id TEXT,"
                       "token_input_other INTEGER,token_input_cached INTEGER,token_output INTEGER,created_at TEXT)")
            db.executemany("INSERT INTO provider_stats VALUES(?,?,?,?,?,?,?,datetime('now'))", [
                ("bot:GroupMessage:10000", "internal", "first", "a", 100, 300, 800),
                ("bot:FriendMessage:123456", "internal", "second", "b", 200, 0, 100),
                ("external", "external", "first", "a", 10000, 0, 0)])
            db.commit()
            db.close()
            with patch.object(usage, "DB_OVERRIDE", path), patch.object(usage, "load_state", return_value={}):
                result = usage.overview("7")
            self.assertEqual(result["cache_rate"], 50)
            self.assertEqual(result["calls"], 2)
            self.assertEqual({m["model"]: m["rate"] for m in result["cache_models"]}, {"first": 75, "second": 0})
            self.assertIsNone(result["cost"])


if __name__ == "__main__":
    unittest.main()
