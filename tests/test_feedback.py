"""问题反馈（控制台一侧）：内容整理、频率限制、只收自己生成的问题报告、没发出去时存到本地。
状态、导出目录和网络都换成假的，不联网、不写正在用的 state.json。"""

import base64
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import feedback  # noqa: E402


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.state = {}
        self.export = self.tmp / "导出"
        self.export.mkdir()
        ps = [patch.object(feedback, "load_state", lambda: dict(self.state)),
              patch.object(feedback, "update_state", lambda **kw: self.state.update(kw)),
              patch.object(feedback.logtools, "EXPORT", self.export),
              patch.object(feedback, "log", lambda *a, **k: None)]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        self.sent = []

    def fake_post(self, reply):
        def _post(payload, timeout=40):
            self.sent.append(payload)
            return reply
        return patch.object(feedback, "_post", _post)

    def make_report(self, name="问题报告-20261111-1200.zip"):
        p = self.export / name
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("01-概况.txt", "概况")
        return p

    def test_device_id_is_stable_and_anonymous(self):
        d = feedback.device_id()
        self.assertRegex(d, r"^[0-9a-f]{16}$")
        self.assertEqual(feedback.device_id(), d)

    def test_send_ok_with_report(self):
        rep = self.make_report()
        with self.fake_post({"ok": True}):
            r = feedback.send("问题", "她不回消息了", "me@qq.com", str(rep))
        self.assertTrue(r["ok"])
        p = self.sent[0]
        self.assertEqual(p["app"], "dafeiyu-kit")
        self.assertEqual(set(p), {"app", "kind", "desc", "contact", "version", "system", "device", "report"})
        self.assertEqual(base64.b64decode(p["report"]["data"]), rep.read_bytes())
        self.assertEqual(len(self.state["feedback_sent"]), 1)

    def test_validation(self):
        with self.fake_post({"ok": True}):
            self.assertFalse(feedback.send("问题", " ")["ok"])
            outside = self.tmp / "问题报告-x.zip"
            outside.write_bytes(b"PK")
            self.assertIn("未找到", feedback.send("问题", "描述内容", report_path=str(outside))["error"])
            other = self.export / "别的文件.zip"
            other.write_bytes(b"PK")
            self.assertIn("未找到", feedback.send("问题", "描述内容", report_path=str(other))["error"])
            self.assertEqual(feedback.send("乱写", "描述内容")["ok"], True)
        self.assertEqual(self.sent[-1]["kind"], "其他")

    def test_rate_limit_saves_locally(self):
        with self.fake_post({"ok": True}):
            for _ in range(3):
                self.assertTrue(feedback.send("建议", "加个功能")["ok"])
            r = feedback.send("建议", "第四次")
        self.assertFalse(r["ok"])
        self.assertIn("频繁", r["error"])
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(Path(r["saved"]).exists())

    def test_failure_saves_package_with_report(self):
        rep = self.make_report()
        with self.fake_post({"ok": False, "error": "邮件没发出去，请稍后再试"}):
            r = feedback.send("问题", "掉线了\n第二行", "", str(rep))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "邮件没发出去，请稍后再试")
        with zipfile.ZipFile(r["saved"]) as z:
            names = z.namelist()
            text = z.read("反馈.txt").decode("utf-8-sig")
        self.assertIn(rep.name, names)
        self.assertIn("掉线了", text)
        self.assertIn("设备码", text)
        self.assertNotIn("feedback_sent", self.state)

    def test_network_error_is_caught(self):
        def boom(payload, timeout=40):
            raise OSError("no route")
        with patch.object(feedback, "_post", boom):
            r = feedback.send("问题", "连不上")
        self.assertFalse(r["ok"])
        self.assertIn("连不上反馈服务器", r["error"])
        self.assertTrue(Path(r["saved"]).exists())


class RelayTests(unittest.TestCase):
    """中转函数（scripts/feedback-relay/index.py）的校验和邮件内容，SMTP 用假的。"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "relay", Path(__file__).resolve().parents[1] / "scripts" / "feedback-relay" / "index.py")
        self.relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.relay)

    def ev(self, data, ip="1.2.3.4", method="POST"):
        return json.dumps({"body": json.dumps(data, ensure_ascii=False), "headers": {},
                           "requestContext": {"http": {"method": method, "sourceIp": ip}, "requestId": "r1"}})

    def test_mail_contains_fields_and_attachment(self):
        sent = []
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "a@example.com", "SMTP_PASS": "x", "MAIL_TO": "b@example.com"}
        rep = base64.b64encode(b"PK\x03\x04demo").decode()
        data = {"app": "dafeiyu-kit", "kind": "问题", "desc": "她不回消息", "contact": "me@qq.com", "version": "1.5.1",
                "system": "Windows 11", "device": "0123456789abcdef", "report": {"name": "问题报告-1.zip", "data": rep}}
        with patch.dict("os.environ", env), patch.object(self.relay, "send", lambda m: sent.append(m)):
            r = json.loads(self.relay.handler(self.ev(data), None)["body"])
            bad = self.relay.handler(self.ev({**data, "app": "x"}, ip="9.9.9.9"), None)
        self.assertTrue(r["ok"])
        m = sent[0]
        self.assertIn("[大肥鱼反馈][1.5.1][问题]", m["Subject"])
        body = m.get_body(("plain",)).get_content()
        self.assertIn("设备码：0123456789abcdef", body)
        self.assertEqual([a.get_filename() for a in m.iter_attachments()], ["问题报告-1.zip"])
        self.assertEqual(bad["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
