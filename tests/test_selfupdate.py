"""控制台自动更新：签名校验、版本判断、读取更新信息、下载核对、备份与安装往返。
只用临时目录和 file:// 地址，不联网，不碰正在用的整合包。"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ed25519  # noqa: E402
import selfupdate  # noqa: E402

SECRET = bytes(range(32))
PUB = ed25519.public_key(SECRET).hex()


def signed(payload: dict, secret: bytes = SECRET) -> dict:
    return {"payload": payload, "sig": ed25519.sign(secret, selfupdate.canonical(payload)).hex()}


def payload(version="1.5.2", sha="00" * 32, size=10, url="https://github.com/x/y/releases/download/v1.5.2/u.zip"):
    return {"schema": 1, "version": version, "date": "2026-11-20", "notes": "### 新增\n- 自动更新", "todo": [],
            "update_min_from": "1.5.0", "update": {"name": "u.zip", "size": size, "sha256": sha, "url": url},
            "full": {"name": "f.zip", "size": 99, "sha256": "11" * 32, "url": url}}


def make_zip(path: Path, files: dict, root="Dafeiyu"):
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr(f"{root}/{rel}" if root else rel, text)
    return path


class VerifyTests(unittest.TestCase):
    def test_good_signature(self):
        self.assertEqual(selfupdate.verify_doc(signed(payload()), PUB)["version"], "1.5.2")

    def test_tampered_or_wrong_key(self):
        doc = signed(payload())
        doc["payload"]["update"]["url"] = "https://evil.example/u.zip"
        with self.assertRaises(ValueError):
            selfupdate.verify_doc(doc, PUB)
        with self.assertRaises(ValueError):
            selfupdate.verify_doc(signed(payload(), bytes(32)), PUB)
        with self.assertRaises(ValueError):
            selfupdate.verify_doc({"payload": payload()}, PUB)

    def test_incomplete_payload_rejected_even_if_signed(self):
        with self.assertRaises(ValueError):
            selfupdate.verify_doc(signed(payload(sha="xyz")), PUB)
        with self.assertRaises(ValueError):
            selfupdate.verify_doc(signed(payload(url="ftp://a/b.zip")), PUB)


class VersionTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(selfupdate.newer("1.5.10", "1.5.9"))
        self.assertFalse(selfupdate.newer("1.5.1", "1.5.1"))
        self.assertFalse(selfupdate.newer("", "1.5.1"))

    def test_describe_needs_full(self):
        cached = {"version": "1.6.0", "update_min_from": "1.5.5", "notes": "x"}
        with patch.object(selfupdate, "VERSION", "1.5.1"):
            d = selfupdate.describe(cached)
        self.assertTrue(d["newer"])
        self.assertTrue(d["needs_full"])
        with patch.object(selfupdate, "VERSION", "1.5.5"):
            self.assertFalse(selfupdate.describe(cached)["needs_full"])
        with patch.object(selfupdate, "VERSION", "1.6.0"):
            self.assertFalse(selfupdate.describe(cached)["newer"])


class FetchAndDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        patcher = patch.object(selfupdate, "LOG_DIR", self.tmp / "logs")
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, manifest: Path, key=PUB):
        return patch.dict(os.environ, {selfupdate.ENV_MANIFEST: manifest.as_uri(), selfupdate.ENV_PUBKEY: key})

    def test_fetch_from_local_manifest(self):
        m = self.tmp / "latest.json"
        m.write_text(json.dumps(signed(payload()), ensure_ascii=False), encoding="utf-8")
        with self.env(m):
            self.assertEqual(selfupdate.fetch_manifest()["version"], "1.5.2")
            self.assertEqual(selfupdate.latest()["version"], "1.5.2")

    def test_fetch_rejects_bad_signature(self):
        m = self.tmp / "latest.json"
        doc = signed(payload())
        doc["payload"]["version"] = "9.9.9"
        m.write_text(json.dumps(doc), encoding="utf-8")
        with self.env(m), self.assertRaises(RuntimeError) as cm:
            selfupdate.fetch_manifest()
        self.assertIn("校验失败", str(cm.exception))

    def test_not_enabled_without_key(self):
        with patch.object(selfupdate, "UPDATE_PUBKEY", ""), patch.dict(os.environ, {selfupdate.ENV_PUBKEY: ""}):
            self.assertFalse(selfupdate.enabled())
            self.assertEqual(selfupdate.latest(), {})

    def test_download_checks_sha256(self):
        z = make_zip(self.tmp / "u.zip", {"app/core.py": 'VERSION = "1.5.2"\n', "pad.txt": "x" * 5000})
        good = payload(sha=selfupdate.sha256_file(z), size=z.stat().st_size, url=z.as_uri())
        got = selfupdate.download_update(good)
        self.assertEqual(selfupdate.sha256_file(got), good["update"]["sha256"])
        bad = payload(sha="ab" * 32, size=z.stat().st_size, url=z.as_uri())
        with self.assertRaises(RuntimeError) as cm:
            selfupdate.download_update(bad)
        self.assertIn("更新包校验失败", str(cm.exception))
        self.assertFalse(any((self.tmp / "logs" / "更新下载").glob("*.zip")))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.kit = self.tmp / "kit"
        for rel, text in {"app/core.py": 'VERSION = "1.5.1"\n', "app/old.py": "old", "README.txt": "v1.5.1",
                          "runtime/python/keep.txt": "runtime", "state.json": "{}", "知识库/a.md": "资料",
                          "版本切换.bat": "bat"}.items():
            (self.kit / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.kit / rel).write_text(text, encoding="utf-8")
        self.upd = make_zip(self.tmp / "u.zip", {
            "app/core.py": 'VERSION = "1.5.2"\n', "app/new.py": "new", "README.txt": "v1.5.2",
            "docs/开发记录.md": "dev", "runtime/python/keep.txt": "evil", "state.json": "evil",
            "app/../../escape.txt": "evil"})

    def read(self, rel):
        return (self.kit / rel).read_text(encoding="utf-8")

    def test_package_version(self):
        self.assertEqual(selfupdate.package_version(self.upd), "1.5.2")
        with self.assertRaises(RuntimeError):
            selfupdate.package_version(make_zip(self.tmp / "x.zip", {"hello.txt": "x"}))

    def test_install_backup_and_rollback_roundtrip(self):
        bk, n = selfupdate.install_with_backup(self.upd, self.tmp / "bk", self.kit)
        self.assertIn('"1.5.2"', self.read("app/core.py"))
        self.assertEqual(self.read("app/new.py"), "new")
        self.assertEqual(self.read("runtime/python/keep.txt"), "runtime")   # 自带 Python 不动
        self.assertEqual(self.read("state.json"), "{}")                      # 设置不动
        self.assertEqual(self.read("知识库/a.md"), "资料")
        self.assertFalse((self.kit / "docs").exists())
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertEqual(n, 3)
        self.assertTrue(bk.name.startswith("控制台-v1.5.1-"))
        with zipfile.ZipFile(bk) as z:
            names = set(z.namelist())
        self.assertIn("Dafeiyu/app/core.py", names)
        self.assertIn("Dafeiyu/版本切换.bat", names)
        self.assertFalse(any("runtime" in x or "state.json" in x or "知识库" in x for x in names))
        selfupdate.install_zip(bk, self.kit)                                  # 回退
        self.assertIn('"1.5.1"', self.read("app/core.py"))
        self.assertEqual(self.read("README.txt"), "v1.5.1")

    def test_failed_install_restores_backup(self):
        real = selfupdate.install_zip
        calls = []

        def flaky(path, kit=selfupdate.KIT):
            calls.append(Path(path).name)
            if len(calls) == 1:
                real(path, kit)                 # 已经覆盖了一部分
                raise OSError("模拟写到一半出错")
            return real(path, kit)

        with patch.object(selfupdate, "install_zip", flaky), self.assertRaises(OSError):
            selfupdate.install_with_backup(self.upd, self.tmp / "bk", self.kit)
        self.assertEqual(len(calls), 2)
        self.assertIn('"1.5.1"', self.read("app/core.py"))
        self.assertEqual(self.read("README.txt"), "v1.5.1")


if __name__ == "__main__":
    unittest.main()
