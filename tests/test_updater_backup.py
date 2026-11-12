"""组件回退：备份文件已不存在时，先报错、不停止 AstrBot / NapCat；更新页显示为不可回退。
全部用临时文件夹和替身函数，不读写正在用的 state.json，不启动或停止任何程序。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import updater  # noqa: E402


class BackupMissing(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_kinds(self):
        z = self.d / "astrbot-4.29.3.zip"
        self.assertTrue(updater._backup_missing({"kind": "zip", "backup": str(z)}))
        z.write_bytes(b"PK")
        self.assertFalse(updater._backup_missing({"kind": "zip", "backup": str(z)}))
        self.assertFalse(updater._backup_missing({"kind": "pip", "backup": ""}))
        old = self.d / "NapCat.Shell"
        self.assertTrue(updater._backup_missing({"kind": "dir", "backup": str(old)}))
        old.mkdir(); (old / "NapCatWinBootMain.exe").write_bytes(b"")
        self.assertFalse(updater._backup_missing({"kind": "dir", "backup": str(old)}))

    def test_rollback_checks_before_stopping(self):
        gone = str(self.d / "gone.zip")
        recs = {"astrbot": {"from": "4.29.3", "to": "4.29.4", "kind": "zip", "backup": gone},
                "napcat": {"from": "4.18.41", "to": "4.18.42", "kind": "zip", "backup": gone}}
        with patch.object(updater, "_records", return_value=recs), \
             patch.object(updater.services, "stop_astrbot") as sa, patch.object(updater.services, "stop_napcat") as sn:
            with self.assertRaises(RuntimeError):
                updater.rollback_astrbot()
            with self.assertRaises(RuntimeError):
                updater.rollback_napcat()
            sa.assert_not_called()
            sn.assert_not_called()



class CheckError(unittest.TestCase):
    def test_no_raw_errors(self):
        import urllib.error
        e = urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)")
        self.assertEqual(updater._check_error(e), "无法连接到 GitHub（网络或代理问题），请稍后重试。")
        self.assertIn("限制了查询次数", updater._check_error(OSError("HTTP Error 403: rate limit exceeded")))
        self.assertIn("超时", updater._check_error(TimeoutError("timed out")))
        self.assertEqual(updater._check_error(RuntimeError("更新包签名无效")), "更新包签名无效")
        e404 = RuntimeError("读取更新信息失败：直连和加速站都没连上（github.com：HTTP Error 404: Not Found；gh-proxy.com：HTTP Error 404: Not Found）")
        self.assertIn("还没有支持自动更新", updater._check_error(e404))

    def test_napcat_no_release_is_an_error(self):
        with patch.object(updater, "github_latest_asset", return_value=("", "")):
            with self.assertRaises(RuntimeError) as c:
                updater.nc_latest()
        self.assertIn("没有可下载的发布版本", str(c.exception))



class NapcatFrozen(unittest.TestCase):
    def test_check_skips_napcat_and_info_marks_frozen(self):
        saved = {}
        with patch.object(updater, "load_state", return_value={"update_check": {}}), \
             patch.object(updater, "update_state", side_effect=lambda **k: saved.update(k)), \
             patch.object(updater, "ab_latest", return_value={"version": "4.29.4"}), \
             patch.object(updater.selfupdate, "latest", return_value={}), \
             patch.object(updater, "nc_latest", side_effect=AssertionError("不应再检查 NapCat")):
            c = updater.check(force=True)
        self.assertNotIn("napcat", c)
        with patch.object(updater, "check", return_value={"at": 1, "errors": {"napcat": "旧错误"}, "napcat": {"version": "9.9.9"}}), \
             patch.object(updater, "_records", return_value={}), patch.object(updater, "ab_current", return_value="4.29.4"), \
             patch.object(updater, "nc_current", return_value="4.18.42"), patch.object(updater, "ab_source_root", return_value=None), \
             patch.object(updater, "update_blocked", return_value=""), \
             patch.object(updater.selfupdate, "describe", return_value={}):
            info = updater.info()
        self.assertTrue(info["napcat"]["frozen"])
        self.assertFalse(info["napcat"]["newer"])
        self.assertNotIn("napcat", info["errors"])


if __name__ == "__main__":
    unittest.main()
