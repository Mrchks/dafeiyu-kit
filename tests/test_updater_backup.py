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


if __name__ == "__main__":
    unittest.main()
