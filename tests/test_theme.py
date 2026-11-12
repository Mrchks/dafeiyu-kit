"""外观设置：控制台页面在首次绘制前带上所选主题；无效值按「跟随系统」处理。不读写正在用的 state.json。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import core  # noqa: E402
import server  # noqa: E402


class ThemeTests(unittest.TestCase):
    def theme(self, value):
        state = {} if value is None else {"theme": value}
        return patch.object(core, "load_state", lambda: dict(state))

    def test_ui_theme_values(self):
        for value, want in ((None, "system"), ("system", "system"), ("light", "light"), ("dark", "dark"), ("blue", "system"), (1, "system")):
            with self.theme(value):
                self.assertEqual(core.ui_theme(), want, value)

    def test_index_marks_theme_before_first_paint(self):
        for t in ("light", "dark"):
            with patch.object(server, "ui_theme", lambda t=t: t):
                html = server.themed_index()
            self.assertIn(f'<html lang="zh-CN" data-theme="{t}">'.encode(), html[:200])
            self.assertEqual(html.count(b"data-theme=\"" + t.encode() + b"\">"), 1)

    def test_index_follows_system_without_attribute(self):
        with patch.object(server, "ui_theme", lambda: "system"):
            html = server.themed_index()
        self.assertIn(b'<html lang="zh-CN">', html[:200])

    def test_page_css_respects_manual_choice(self):
        css = (core.UI_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn(':root:not([data-theme="light"])', css)       # 跟随系统的深色规则不覆盖手动选择的浅色
        self.assertIn(':root[data-theme="dark"]{', css)
        self.assertIn('<html lang="zh-CN">', css)                   # 注入依赖这一行的写法


if __name__ == "__main__":
    unittest.main()
