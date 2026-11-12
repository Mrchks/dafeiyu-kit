"""查询 GitHub 最新版本：加速站没有跳转时不把 releases 当成版本号；API 返回的发布缺少对应文件时仍给出版本号。
全部用替身函数，不联网。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import core  # noqa: E402


class FakeResp:
    def __init__(self, url):
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def api_down(*a, **k):
    raise OSError("offline")


class GithubLatest(unittest.TestCase):
    def setUp(self):
        p = patch.object(core, "log", lambda *a, **k: None)      # 不写正在用的控制台日志
        p.start()
        self.addCleanup(p.stop)

    def test_proxy_without_redirect_is_ignored(self):
        with patch.object(core, "http_json", api_down), patch.object(core, "GH_PROXIES", ["https://p/"]), \
             patch.object(core.urllib.request, "urlopen", lambda req, timeout=10: FakeResp("https://p/https://github.com/x/y/releases")):
            self.assertEqual(core.github_latest_asset("x/y", lambda n: True), ("", ""))

    def test_proxy_redirect_gives_tag(self):
        with patch.object(core, "http_json", api_down), patch.object(core, "GH_PROXIES", ["https://p/"]), \
             patch.object(core.urllib.request, "urlopen", lambda req, timeout=10: FakeResp("https://github.com/x/y/releases/tag/v4.18.43")):
            self.assertEqual(core.github_latest_asset("x/y", lambda n: True), ("v4.18.43", ""))

    def test_api_without_matching_asset_keeps_tag(self):
        d = {"tag_name": "v4.18.43", "assets": [{"name": "Other.zip", "browser_download_url": "u"}]}
        with patch.object(core, "http_json", lambda *a, **k: d):
            self.assertEqual(core.github_latest_asset("x/y", lambda n: n == "NapCat.Shell.zip"), ("v4.18.43", ""))

    def test_api_with_asset(self):
        d = {"tag_name": "v4.18.43", "assets": [{"name": "NapCat.Shell.zip", "browser_download_url": "u"}]}
        with patch.object(core, "http_json", lambda *a, **k: d):
            self.assertEqual(core.github_latest_asset("x/y", lambda n: n == "NapCat.Shell.zip"), ("v4.18.43", "u"))


if __name__ == "__main__":
    unittest.main()
