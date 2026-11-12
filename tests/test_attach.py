"""接入已有的 AstrBot 和 NapCat：路径检测、版本判断、推荐设置对比、增量导入、解除接入不删除外部目录。
全部在临时文件夹中进行，不读写正在用的 state.json，不启动任何程序。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import attach  # noqa: E402

BIN = "Scripts" if attach.WIN else "bin"
PY = "pythonw.exe" if attach.WIN else "python3"


def write(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_astrbot(root: Path, version="4.29.3", source=True, ws=6199):
    cfg = {"dashboard": {"port": 6185, "username": "owner"}, "admins_id": ["12345678", "abc"],
           "platform": [{"type": "aiocqhttp", "enable": True, "ws_reverse_port": ws, "ws_reverse_token": "TOK"}],
           "platform_settings": {"segmented_reply": {"enable": False}}, "provider_settings": {"identifier": True}}
    write(root / "data" / "cmd_config.json", json.dumps(cfg))
    if source:
        write(root / "main.py")
        (root / "astrbot" / "core").mkdir(parents=True, exist_ok=True)
        write(root / "pyproject.toml", f'[project]\nname = "AstrBot"\nversion = "{version}"\n')
    return root


def make_napcat(base: Path, qq="3250000001", ws=6199, token="TOK") -> Path:
    shell = base / "NapCat.52230.Shell"
    write(shell / "NapCatWinBootMain.exe")
    app = shell / "versions" / "9.9.20" / "resources" / "app" / "napcat"
    write(app / "napcat.mjs", 'const v = "4.18.42" || "1.0.0-dev";')
    write(app / "config" / f"onebot11_{qq}.json", json.dumps({"network": {"websocketClients": [
        {"enable": True, "url": f"ws://127.0.0.1:{ws}/ws", "token": token}]}}))
    write(app / "config" / "webui.json", json.dumps({"port": 6099, "autoLoginAccount": qq}))
    return shell


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_launcher_instance(self):
        inst = self.tmp / ".astrbot_launcher" / "instances" / "abc"
        make_astrbot(inst / "core")
        write(inst / "venv" / BIN / PY)
        with patch.object(attach.Path, "home", return_value=self.tmp):
            roots = attach._launcher_roots()
        self.assertEqual(roots, [inst / "core"])
        it = attach.inspect_astrbot(inst)                 # 选到实例目录也能识别到 core
        self.assertTrue(it["ok"], it["problems"])
        self.assertEqual((it["mode"], it["kind"], it["cmd"], it["version"], it["port"]), ("launcher", "启动器实例", ["main.py"], "4.29.3", 6185))
        self.assertEqual(it["admins"], ["12345678"])
        self.assertEqual((it["ws_port"], it["ws_token"], it["dashboard_user"]), (6199, "TOK", "owner"))

    def test_pip_install_with_venv(self):
        root = make_astrbot(self.tmp / "astrbot", source=False)
        write(self.tmp / "astrbot-venv" / BIN / PY)
        (self.tmp / "astrbot-venv" / "pyvenv.cfg").write_text("home = x")
        (self.tmp / "astrbot-venv" / "Lib" / "site-packages" / "astrbot-4.27.1.dist-info").mkdir(parents=True)
        it = attach.inspect_astrbot(root / "data")       # 选到 data 文件夹也能识别
        self.assertTrue(it["ok"], it["problems"])
        self.assertEqual((it["mode"], it["cmd"], it["version"], it["system_python"]), ("pip", ["-m", "astrbot.cli", "run"], "4.27.1", False))

    def test_invalid_folders(self):
        it = attach.inspect_astrbot(self.tmp)
        self.assertFalse(it["ok"])
        self.assertIn("cmd_config.json", it["problems"][0])
        root = make_astrbot(self.tmp / "nopy")
        self.assertIn("Python", attach.inspect_astrbot(root)["problems"][0])
        self.assertFalse(attach.inspect_napcat(self.tmp)["ok"])

    def test_version_rules(self):
        self.assertEqual(attach.version_status("4.25.6")["level"], "bad")
        self.assertEqual(attach.version_status(attach.MIN_ASTRBOT)["level"], "ok")
        self.assertEqual(attach.version_status(attach.ASTRBOT_VERSION)["level"], "ok")
        self.assertEqual(attach.version_status("9.0.0")["level"], "warn")
        self.assertEqual(attach.version_status("")["level"], "warn")
        root = make_astrbot(self.tmp / "old", version="4.20.0")
        write(root / "venv" / BIN / PY)
        it = attach.inspect_astrbot(root)
        self.assertFalse(it["ok"])

    def test_napcat_and_link_detection(self):
        shell = make_napcat(self.tmp)
        it = attach.inspect_napcat(self.tmp)               # 选到上一层也能找到 Shell 文件夹
        self.assertTrue(it["ok"], it["problems"])
        self.assertEqual((it["dir"], it["version"], it["qq"], it["ws_ports"]), (str(shell), "4.18.42", "3250000001", [6199]))

    def test_only_astrbot_state(self):
        root = make_astrbot(self.tmp / "core")
        write(root / "venv" / BIN / PY)
        ab = attach.inspect_astrbot(root)
        with patch.object(attach, "load_state", return_value={"home": ""}):
            st = attach.state_for(ab, None, ["12345678"], "")
        self.assertEqual(st["napcat_dir"], "")
        self.assertTrue(st["attached"])
        self.assertEqual(st["astrbot_root"], str(root))

    def test_recommended_diff_and_apply(self):
        cfg = {"platform_settings": {"segmented_reply": {"enable": False}}, "provider_settings": {"identifier": True},
               "agent_runner": {"config": {"persona": {"persona_id": "my"}}}}
        diff = attach.recommended_diff(cfg)
        keys = [d["key"] for d in diff]
        self.assertIn("platform_settings.segmented_reply.enable", keys)
        self.assertNotIn("provider_settings.identifier", keys)                      # 已经一致的不列出
        self.assertNotIn("agent_runner.config.persona.persona_id", keys)             # 不替换当前人设
        new = attach.apply_recommended(cfg, ["platform_settings.segmented_reply.enable", "agent_runner.config.persona.persona_id"])
        self.assertTrue(new["platform_settings"]["segmented_reply"]["enable"])
        self.assertEqual(new["agent_runner"]["config"]["persona"]["persona_id"], "my")
        self.assertFalse(cfg["platform_settings"]["segmented_reply"]["enable"])       # 原配置对象不被改动

    def test_backup_copies_without_touching_originals(self):
        root = make_astrbot(self.tmp / "core")
        shell = make_napcat(self.tmp / "nc")
        ab, nc = attach.inspect_astrbot(root), attach.inspect_napcat(shell)
        files = attach.files_to_backup(ab, nc)
        before = {p: p.read_bytes() for p in files}
        dest = attach.backup_files(files, self.tmp / "Backups")
        self.assertEqual(len(list(dest.rglob("*.json"))), 3)
        self.assertEqual({p: p.read_bytes() for p in files}, before)

    def test_detach_never_deletes_external_dirs(self):
        root = make_astrbot(self.tmp / "core")
        shell = make_napcat(self.tmp / "nc")
        state = {"astrbot_root": str(root), "napcat_dir": str(shell), "attached": True, "setup_done": True}
        removed = []
        fake_installer = type(sys)("installer")
        fake_installer.remove_shortcuts = lambda: removed.append("shortcuts")
        with patch.dict(sys.modules, {"installer": fake_installer}), \
             patch.object(attach, "load_state", lambda: dict(state)), \
             patch.object(attach, "update_state", lambda **kw: state.update(kw)), \
             patch.object(attach, "log", lambda *a: None), \
             patch.object(shutil, "rmtree", side_effect=AssertionError("不应删除任何目录")):
            msg = attach.detach()
        self.assertTrue((root / "data" / "cmd_config.json").is_file())
        self.assertTrue((shell / "NapCatWinBootMain.exe").is_file())
        self.assertEqual(removed, ["shortcuts"])
        self.assertFalse(state["setup_done"])
        self.assertFalse(state["attached"])
        self.assertIn("全部保留", msg)

    def test_launcher_mode_counts_as_attached(self):
        self.assertTrue(attach.is_attached({"mode": "launcher"}))       # 作者现有环境升级后无需重新接入
        self.assertTrue(attach.is_attached({"mode": "pip", "attached": True}))
        self.assertFalse(attach.is_attached({"mode": "pip"}))


class UninstallRouteTests(unittest.TestCase):
    def test_full_uninstall_in_attached_mode_only_detaches(self):
        import server
        with patch.object(server.attach, "is_attached", return_value=True), \
             patch.object(server.attach, "detach", return_value="detached") as d, \
             patch.object(server.services, "stop_all", side_effect=AssertionError("接入形态下不停止原有程序")), \
             patch.object(server.shutil, "rmtree", side_effect=AssertionError("不应删除任何目录")):
            self.assertEqual(server.uninstall(True), "detached")
        d.assert_called_once()


class IncrementalImportTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "configpack" / "tools"))
        import import_dafeiyu
        self.mod = import_dafeiyu

    def test_steps_gate(self):
        with patch.object(self.mod, "ANSWERS", {"steps": ["plugins", "reload"]}):
            self.assertTrue(self.mod.want("plugins"))
            self.assertFalse(self.mod.want("base"))
            self.assertFalse(self.mod.want("memory"))
            self.assertFalse(self.mod.want("chat_model"))
        with patch.object(self.mod, "ANSWERS", {}):
            self.assertTrue(self.mod.want("base"))                      # 未指定时与原来一样全部执行

    def test_attach_answers_skip_overwriting_steps(self):
        import inspect
        self.assertIn('"persona_keep": True', inspect.getsource(attach.run_import))
        src = inspect.getsource(attach.apply) + inspect.getsource(attach.install_plugins)
        for step in ("base", "memory", "voice", "chat_model"):           # 会覆盖原有设置的几步，接入时从不执行
            self.assertNotIn(f'"{step}"', src)


class PluginSyncOptOutTests(unittest.TestCase):
    def test_opt_out_only_updates_installed(self):
        import plugin_sync
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "plugins").mkdir()
        with patch.object(plugin_sync, "astrbot_data", return_value=tmp), \
             patch.object(plugin_sync, "load_state", return_value={"plugins_opt_out": True}):
            self.assertEqual(plugin_sync.outdated(), [])
        with patch.object(plugin_sync, "astrbot_data", return_value=tmp), \
             patch.object(plugin_sync, "load_state", return_value={}):
            self.assertTrue(plugin_sync.outdated())


if __name__ == "__main__":
    unittest.main()
