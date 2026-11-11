"""发版脚本的纯函数：版本号改写、更新日志、文件挑选、签名清单。离线，不碰 Git 和网络。"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release", KIT / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CORE = 'X = 1\nVERSION = "1.5.0"\nUPDATE_REPO = "Mrchks/dafeiyu-kit"\nUPDATE_PUBKEY = ""\n'
LOG = """# 更新日志

规则说明。

## 未发布

### 新增
- 「版本与更新」可以自动更新。

### 升级须知
- 这次要手动装一次。
- 装完重启 AstrBot。

## v1.5.0 · 2026-11-03

### 新增
- 旧内容。
"""


class TextTests(unittest.TestCase):
    def test_core_version_and_pubkey(self):
        out = release.set_core(CORE, "1.5.1", "ab" * 32)
        self.assertIn('VERSION = "1.5.1"', out)
        self.assertIn(f'UPDATE_PUBKEY = "{"ab" * 32}"', out)
        self.assertEqual(release.current_version(out), "1.5.1")
        self.assertIn("X = 1", out)

    def test_readme_first_line_only(self):
        txt = "大肥鱼整合包  v1.5.0\n====\n见 v1.4.0 的说明\n"
        out = release.set_readme(txt, "1.5.1")
        self.assertTrue(out.startswith("大肥鱼整合包  v1.5.1\n"))
        self.assertIn("见 v1.4.0 的说明", out)

    def test_changelog_release_and_new_unreleased(self):
        out = release.set_changelog(LOG, "1.5.1", "2026-11-20")
        self.assertIn("## 未发布\n\n（暂无）\n\n## v1.5.1 · 2026-11-20\n", out)
        self.assertEqual(release.unreleased_body(out), "")
        self.assertIn("自动更新", release.changelog_section(out, "1.5.1"))
        self.assertIn("旧内容", release.changelog_section(out, "1.5.0"))

    def test_empty_unreleased_is_rejected(self):
        empty = LOG.replace(LOG[LOG.index("### 新增"):LOG.index("## v1.5.0")], "（暂无）\n\n")
        with self.assertRaises(release.ReleaseError):
            release.set_changelog(empty, "1.5.1", "2026-11-20")

    def test_todo_items(self):
        self.assertEqual(release.todo_items(release.unreleased_body(LOG)), ["这次要手动装一次。", "装完重启 AstrBot。"])
        self.assertEqual(release.todo_items("### 新增\n- 只有新增\n"), [])


class PickTests(unittest.TestCase):
    def test_full_and_update_selection(self):
        tracked = ["AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "LICENSE", "README.md", "README.txt", "START.vbs",
                   ".gitignore", "app/core.py", "app/ui/index.html", "docs/开发记录.md", "tests/test_x.py",
                   "scripts/release.py", "LICENSES/a.txt"]
        runtime = ["runtime/python/python.exe", "runtime/python/Lib/__pycache__/x.pyc", "runtime/python/x.pyc"]
        full, upd = release.pick_files(tracked, ["版本切换.bat"], runtime)
        for dev in ("AGENTS.md", "CLAUDE.md", "docs/开发记录.md", "tests/test_x.py", "scripts/release.py"):
            self.assertNotIn(dev, full)
        self.assertIn("runtime/python/python.exe", full)
        self.assertFalse(any(r.endswith(".pyc") for r in full))
        self.assertIn("版本切换.bat", full)
        self.assertIn("README.md", full)
        self.assertNotIn("README.md", upd)
        self.assertFalse(any(r.startswith("runtime/") for r in upd))
        self.assertTrue({"app/core.py", "app/ui/index.html", "START.vbs", "CHANGELOG.md", "版本切换.bat",
                         "LICENSES/a.txt"} <= set(upd))

    def test_update_prefixes_match_update_from_zip(self):
        src = (KIT / "app" / "server.py").read_text(encoding="utf-8")
        start = src.index("def update_from_zip")
        body = src[start:src.index("\ndef ", start + 10)]
        for p in release.UPDATE_PREFIXES:
            self.assertIn(f'"{p}"', body, f"update_from_zip 不会覆盖 {p}，更新包里放了也没用")


class ManifestTests(unittest.TestCase):
    def test_sign_and_verify(self):
        secret = os.urandom(32)
        pub = release.ed25519.public_key(secret).hex()
        payload = {"version": "1.5.1", "notes": "中文说明", "update": {"sha256": "00" * 32, "size": 1}}
        doc = release.sign_manifest(payload, secret)
        self.assertTrue(release.verify_manifest(doc, pub))
        doc["payload"]["update"]["sha256"] = "11" * 32
        self.assertFalse(release.verify_manifest(doc, pub))
        self.assertFalse(release.verify_manifest({"payload": payload}, pub))
        self.assertFalse(release.verify_manifest(release.sign_manifest(payload, os.urandom(32)), pub))

    def test_canonical_is_order_independent(self):
        self.assertEqual(release.canonical({"b": 1, "a": "鱼"}), release.canonical({"a": "鱼", "b": 1}))


if __name__ == "__main__":
    unittest.main()
