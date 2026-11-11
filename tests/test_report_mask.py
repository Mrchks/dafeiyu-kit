"""Offline report privacy checks: synthetic data only, no network or production writes."""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import report


class MaskTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"USERNAME": "FixtureTester", "USERPROFILE": r"C:\Users\ProfileTester"})
        env.start()
        self.addCleanup(env.stop)

    def test_windows_user_paths_preserve_the_rest(self):
        cases = [
            ("打开 C:\\Users\\OtherUser\\logs\\run.log", "打开 C:\\Users\\<用户>\\logs\\run.log"),
            ("打开 C:/Users/名字/logs/run.log", "打开 C:/Users/<用户>/logs/run.log"),
            (r'{"path":"C:\\Users\\OtherUser\\logs\\run.log"}',
             r'{"path":"C:\\Users\\<用户>\\logs\\run.log"}'),
            (r"C:\Users\11145\logs", r"C:\Users\<用户>\logs"),
            (r"d:\uSeRs\Jane Doe\AppData", r"d:\uSeRs\<用户>\AppData"),
            (r"C:\Users\O'Brien\AppData", r"C:\Users\<用户>\AppData"),
            ('"C:\\Users\\OtherUser"', '"C:\\Users\\<用户>"'),
            (r"C:\Users\OtherUser", r"C:\Users\<用户>"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(report.mask(source), expected)

    def test_current_usernames_outside_paths_are_masked_case_insensitively(self):
        self.assertEqual(report.mask("当前用户：FixtureTester；profiletester 登录失败。"),
                         "当前用户：<用户>；<用户> 登录失败。")

    def test_profile_name_is_used_without_username(self):
        with patch.dict(os.environ, {"USERNAME": "", "USERPROFILE": "C:/Users/ProfileTester/"}):
            self.assertEqual(report.mask("用户 ProfileTester"), "用户 <用户>")

    def test_numeric_current_username_is_masked_before_qq_numbers(self):
        with patch.dict(os.environ, {"USERNAME": "11145", "USERPROFILE": ""}):
            self.assertEqual(report.mask("用户：11145"), "用户：<用户>")

    def test_username_with_regex_characters_is_literal(self):
        with patch.dict(os.environ, {"USERNAME": "Test.Name+", "USERPROFILE": ""}):
            self.assertEqual(report.mask("Test.Name+ / TestXName+"), "<用户> / TestXName+")

    def test_ordinary_text_and_longer_words_are_unchanged(self):
        source = "用户目录 Users、OtherUser、名字；FixtureTesterBackup、preFixtureTester；C:/Tools/OtherUser/logs"
        self.assertEqual(report.mask(source), source)

    def test_generic_names_are_kept_outside_user_paths_case_insensitively(self):
        for name in ("admin", "administrator", "user", "users", "root", "test", "default", "public",
                     "pc", "owner", "astrbot", "napcat", "dafeiyu"):
            with self.subTest(name=name), patch.dict(os.environ, {
                    "USERNAME": name.upper(), "USERPROFILE": f"C:/Users/{name}"}):
                source = f"登录：{name} / {name.upper()}；C:/Users/{name}/AppData"
                self.assertEqual(report.mask(source, (name.upper(),)),
                                 f"登录：{name} / {name.upper()}；C:/Users/<用户>/AppData")

    def test_admin_backend_keeps_permission_field_and_default_login_name(self):
        config = {"admin": {"enabled": True}, "dashboard": {"username": "admin"}}
        source = json.dumps(config)
        self.assertEqual(report.mask(source, ("AdMiN",)), source)

    def test_astrbot_backend_keeps_component_name_and_program_path(self):
        source = r"AstrBot 启动：D:\Dafeiyu\astrbot\data；NapCat 已连接。"
        self.assertEqual(report.mask(source, ("astrbot",)), source)

    def test_user_windows_name_keeps_user_id_but_masks_user_directory(self):
        with patch.dict(os.environ, {"USERNAME": "user", "USERPROFILE": r"C:\Users\user"}):
            self.assertEqual(report.mask(r"User ID: abc；C:\Users\user\AppData"),
                             r"User ID: abc；C:\Users\<用户>\AppData")

    def test_short_names_are_kept_outside_paths(self):
        for name in ("Q", "ab", "张三"):
            with self.subTest(name=name), patch.dict(os.environ, {
                    "USERNAME": name, "USERPROFILE": f"C:/Users/{name}"}):
                self.assertEqual(report.mask(f"登录：{name}；C:/Users/{name}/logs", (name,)),
                                 f"登录：{name}；C:/Users/<用户>/logs")

    def test_three_character_personal_names_are_still_masked(self):
        with patch.dict(os.environ, {"USERNAME": "Abc", "USERPROFILE": ""}):
            self.assertEqual(report.mask("用户 Abc；后台 Xyz", ("Xyz",)), "用户 <用户>；后台 <后台账号>")

    def test_missing_environment_names_do_not_modify_ordinary_text(self):
        with patch.dict(os.environ, {"USERNAME": "", "USERPROFILE": ""}):
            self.assertEqual(report.mask("普通日志：OtherUser 运行正常。"), "普通日志：OtherUser 运行正常。")

    def test_masking_is_idempotent_even_when_username_matches_placeholder(self):
        with patch.dict(os.environ, {"USERNAME": "用户", "USERPROFILE": ""}):
            result = report.mask(r"C:\Users\OtherUser\logs 用户 <用户>")
            self.assertEqual(result, r"C:\Users\<用户>\logs 用户 <用户>")
            self.assertEqual(report.mask(result), result)

    def test_existing_secret_and_qq_masking_is_preserved(self):
        self.assertEqual(report.mask("key=sk-abcdefgh123456789 QQ=123456789"), "key=sk-**** QQ=12****89")

    def test_json_path_is_still_valid_json(self):
        source = json.dumps({"path": r"C:\Users\OtherUser\AppData\logs"})
        self.assertEqual(json.loads(report.mask(source)), {"path": r"C:\Users\<用户>\AppData\logs"})

    def test_backend_accounts_use_their_own_placeholder_before_number_masking(self):
        source = "FixtureTesterLogin fixturetesterlogin 654321 Config.Login+ FixtureTesterLoginBackup FixtureTester"
        self.assertEqual(report.mask(source, ("FixtureTesterLogin", "654321", "Config.Login+", "")),
                         "<后台账号> <后台账号> <后台账号> <后台账号> FixtureTesterLoginBackup <用户>")

    def test_same_backend_and_windows_name_keeps_user_path_placeholder(self):
        self.assertEqual(report.mask(r"C:\Users\FixtureTester\logs 登录：FixtureTester", ("FixtureTester",)),
                         r"C:\Users\<用户>\logs 登录：<后台账号>")

    def test_both_placeholders_survive_repeated_masking(self):
        with patch.dict(os.environ, {"USERNAME": "用户", "USERPROFILE": ""}):
            result = report.mask("用户 后台账号 <用户> <后台账号>", ("后台账号",))
            self.assertEqual(result, "用户 <后台账号> <用户> <后台账号>")
            self.assertEqual(report.mask(result, ("后台账号",)), result)

    def test_build_masks_every_archive_entry_including_bypassed_fields(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            config = {"FixtureTester": r"C:\Users\OtherUser\AppData", "api_key": "TEST_SECRET",
                      "dashboard": {"username": "654321"}}
            (root / "cmd_config.json").write_text(json.dumps(config), encoding="utf-8")
            state = {"setup_done": True, "home": r"C:\Users\FixtureTester\Kit",
                     "FixtureTester": "profile=ProfileTester", "dashboard_user": "FixtureTesterLogin",
                     "offline_events": [{"at": 1, "reason": "FixtureTester FixtureTesterLogin 654321 连接断开"}]}
            stack.enter_context(patch.object(report, "load_state", return_value=state))
            stack.enter_context(patch.object(report, "_checks", return_value=("故障诊断", [])))
            stack.enter_context(patch.object(report, "_versions", return_value=["FixtureTester FixtureTesterLogin 654321 版本 12345"]))
            stack.enter_context(patch.object(report.services, "status", return_value={}))
            stack.enter_context(patch.object(report.services, "job", {"lines": ["FixtureTester FixtureTesterLogin 654321"]}))
            stack.enter_context(patch.object(report.services, "astrbot_log_file", return_value=root / "astrbot.log"))
            stack.enter_context(patch.object(report, "astrbot_data", return_value=root))
            stack.enter_context(patch.object(report, "LOG_DIR", root))
            stack.enter_context(patch.object(report, "tail_lines", return_value=[r"C:\Users\OtherUser\logs FixtureTester FixtureTesterLogin 654321"]))
            stack.enter_context(patch.object(report.logtools, "meaningful_tail", return_value=["ProfileTester FixtureTesterLogin 654321"]))
            stack.enter_context(patch.object(report.logtools, "EXPORT", root / "export"))
            stack.enter_context(patch.object(report.logtools, "reveal"))
            stack.enter_context(patch.object(report, "log"))
            result = report.build()
            with zipfile.ZipFile(result["path"]) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "01-概况.txt", "02-最近一次操作的输出.txt", "03-设置（已脱敏）.json",
                    "04-控制台日志.txt", "05-AstrBot日志.txt", "06-NapCat日志.txt",
                    "07-AstrBot配置（已脱敏）.json", "08-掉线记录.txt",
                })
                for name in archive.namelist():
                    with self.subTest(name=name):
                        text = archive.read(name).decode("utf-8-sig")
                        self.assertNotRegex(text, r"FixtureTester|ProfileTester|OtherUser|654321|TEST_SECRET")
                        self.assertIn("<后台账号>", text)
                        self.assertIn("<用户>", text)
                        if name.endswith(".json"):
                            json.loads(text)
                self.assertIn("版本 12345", archive.read("01-概况.txt").decode("utf-8-sig"))
                self.assertIn("通用名称和少于 3 个字符的名字保留", archive.read("01-概况.txt").decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
