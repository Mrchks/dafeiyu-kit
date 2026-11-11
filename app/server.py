"""大肥鱼控制台：本机后台 + 托盘。双击 START.vbs 运行（无黑窗口）。界面 http://127.0.0.1:6190/（6190 被系统保留时自动换端口）"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import astrbot_api  # noqa: E402
import modelhub  # noqa: E402
import dashlogin  # noqa: E402
import usage  # noqa: E402
import memory  # noqa: E402
import report  # noqa: E402
import troubles  # noqa: E402
import installer  # noqa: E402
import logtools  # noqa: E402
import manage  # noqa: E402
import plugin_sync  # noqa: E402
import services  # noqa: E402
import updater  # noqa: E402
import versions  # noqa: E402
from core import (CONSOLE_PORTS, KIT, console_alive, console_port, LOG_DIR, UI_DIR, VERSION, load_state, log, log_exc, open_console_window,  # noqa: E402
                  popen_detached, port_open, set_wanted, tail_lines, update_state)

ACTIONS = {
    "start_all": ("全部启动", services.start_all),
    "stop_all": ("全部停止", services.stop_all),
    "start_astrbot": ("启动 AstrBot", lambda: (set_wanted("astrbot", True), services.start_astrbot())[1]),
    "stop_astrbot": ("停止 AstrBot", lambda: (set_wanted("astrbot", False), services.stop_astrbot())[1]),
    "restart_astrbot": ("重启 AstrBot", lambda: (set_wanted("astrbot", True), services.stop_astrbot(), services.start_astrbot())[2]),
    "start_napcat": ("启动 NapCat", lambda: (set_wanted("napcat", True), services.start_napcat())[1]),
    "stop_napcat": ("停止 NapCat", lambda: (set_wanted("napcat", False), services.stop_napcat())[1]),
    "profile_astrbot": ("启动耗时分析", services.profile_astrbot),
    "restart_napcat": ("重启 NapCat", lambda: (set_wanted("napcat", True), services.stop_napcat(), time.sleep(3), services.start_napcat())[3]),
}


def notice_info() -> dict:
    """每天第一次打开控制台时弹的「更新内容」。勾了「只在版本更新后显示」就同一个版本只弹一次。"""
    s = load_state()
    rel = versions.latest_release()
    n = s.get("notice") or {}
    if not s["setup_done"] or not rel:
        return {"show": False}
    if n.get("only_new"):
        show = n.get("last_version") != rel["version"]
    else:
        show = n.get("last_date") != time.strftime("%Y-%m-%d")
    c = s.get("update_check") or {}
    ups = []
    for comp, name in (("astrbot", "AstrBot"), ("napcat", "NapCat")):
        if comp in updater.has_updates():
            ups.append({"name": name, "version": (c.get(comp) or {}).get("version", "")})
    return {"show": show, "version": rel["version"], "date": rel["date"], "body": rel["body"],
            "only_new": bool(n.get("only_new")), "updates": ups, "todo": after_update_todo(s)}


def after_update_todo(s: dict | None = None) -> list[dict]:
    """更新后还要用户点一下才生效的事（更新内容弹窗里列成清单，做完自动打勾）。"""
    s = s or load_state()
    out = []
    try:
        ab = services.status()["astrbot"]
        need = services.astrbot_needs_restart(s, ab)
        out.append({"key": "restart_astrbot", "done": not need, "title": "重启一次 AstrBot",
                    "detail": "让这次更新里 AstrBot 相关的改动生效。重启时她大约一分钟不回消息。" if need
                    else "已经是用新设置启动的。",
                    "actions": [{"key": "restart_astrbot", "label": "重启 AstrBot"}] if need else []})
    except Exception:
        log_exc()
    return out if any(not t["done"] for t in out) else []      # 都做完了就不显示清单


MEMORY_REPLAY: list[dict] = []     # 最近一次回放测试的结果


def restart_self() -> None:
    exe = sys.executable
    popen_detached([exe, str(Path(__file__).resolve()), "--wait"], KIT, None, LOG_DIR / "console.out.log", console_app=False)
    log("控制台后台重启")
    threading.Timer(0.6, lambda: os._exit(0)).start()


def update_from_zip(path: str) -> str:
    p = Path(path.strip().strip('"'))
    if not p.exists():
        raise RuntimeError("找不到这个文件")
    with zipfile.ZipFile(p) as z:
        infos = {}
        for info in z.infolist():
            n = info.filename
            if not info.flag_bits & 0x800:   # 没标 UTF-8 的压缩包，中文文件名按 UTF-8 还原
                try:
                    n = n.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            infos[n] = info
        names = list(infos)
        prefix = next((n[: -len("app/core.py")] for n in names if n.endswith("app/core.py")), None)
        if prefix is None:
            raise RuntimeError("这不是大肥鱼整合包的压缩包")
        old = KIT / "app.old"
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        shutil.copytree(KIT / "app", old)
        for n in names:
            if not n.startswith(prefix) or n.endswith("/"):
                continue
            rel = n[len(prefix):]
            if not rel.startswith(("app/", "START.vbs", "README.txt", "LICENSES/", "CHANGELOG.md", ".gitignore", "版本切换.bat")):
                continue
            tgt = KIT / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            with z.open(infos[n]) as fs, open(tgt, "wb") as fd:
                shutil.copyfileobj(fs, fd)
    log(f"已从 {p.name} 更新控制台")
    try:
        if versions.is_repo() and versions.status(force=True).get("detached"):
            log("版本管理：现在停在旧版本上，这次更新不记版本")
        else:
            nv = versions.version_in_tree()
            versions.release(nv, f"v{nv}（从 {p.name} 更新）")
    except Exception as e:
        log(f"版本管理：记版本失败 {e}")
    threading.Timer(1.0, restart_self).start()
    return "更新完成，控制台马上重启（她不会掉线）"


def uninstall(full: bool) -> str:
    services.stop_all()
    installer.remove_shortcuts()
    update_state(autostart=False)
    if not full:
        return "已停止，并删掉了桌面图标和开机自启。整合包文件夹你可以自己删。"
    manage.do_backup(False, "before-uninstall")
    s = load_state()
    home = Path(s["home"]) if s["home"] else None
    removed = []
    if home and s["mode"] == "pip":
        for sub in ("astrbot", "astrbot-venv", "napcat", "configpack-work"):
            d = home / sub
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                removed.append(sub)
    update_state(setup_done=False, setup_step="welcome", wizard={})
    return ("已卸载：" + "、".join(removed) + "。备份留在 Backups 文件夹里。最后把整合包文件夹删掉就干净了。") if removed \
        else "已停止并移除快捷方式。"


class Handler(BaseHTTPRequestHandler):
    server_version = f"DafeiyuKit/{VERSION}"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _ok(self, started, msg=""):
        self._json({"ok": started, "msg": msg if started else "上一个操作还没做完，稍等"})

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin") or ""
        port = self.server.server_address[1]
        if host not in ("127.0.0.1", "localhost") or (origin and origin not in (
                f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            p = u.path
            if p in ("/", "/index.html"):
                self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif p == "/icon.png":
                self._send(200, (UI_DIR / "icon.png").read_bytes(), "image/png")
            elif p == "/models.js":
                self._send(200, (UI_DIR / "models.js").read_bytes(), "text/javascript; charset=utf-8")
            elif p == "/api/ping":
                self._json({"ok": True, "version": VERSION})
            elif p == "/api/status":
                self._json(services.status_snapshot())
            elif p == "/api/test-options":
                self._json(astrbot_api.dialogue_options())
            elif p == "/api/models":
                self._json(modelhub.snapshot())
            elif p == "/api/model-catalog":
                self._json(modelhub.catalog())
            elif p == "/api/models/list":
                self._json(modelhub.remote_models(q.get("source", "")))
            elif p == "/api/job":
                self._json(services.job)
            elif p == "/api/balance":
                self._json(manage.deepseek_balance(force=q.get("force") == "1"))
            elif p == "/api/settings":
                self._json(manage.get_settings())
            elif p == "/api/backups":
                self._json(manage.list_backups())
            elif p == "/api/logs/range":
                self._json(logtools.query(q.get("src", "astrbot"), q.get("start", ""), q.get("end", ""),
                                          q.get("problems") == "1"))
            elif p == "/api/logs":
                src = q.get("src", "astrbot")
                n = min(int(q.get("n", "400")), 3000)
                f = {"astrbot": services.astrbot_log_file(), "napcat": LOG_DIR / "napcat.log",
                     "console": LOG_DIR / "console.log"}.get(src, LOG_DIR / "console.log")
                self._json({"file": str(f), "lines": tail_lines(f, n)})
            elif p == "/api/qrcode":
                f = services.napcat_qr_file()
                if f and f.exists():
                    self._send(200, f.read_bytes(), "image/png")
                else:
                    self._json({"error": "no qr"}, 404)
            elif p == "/api/diagnose":
                self._json(manage.diagnose())
            elif p == "/api/groups":
                self._json(manage.group_list())
            elif p == "/api/personas":
                self._json(astrbot_api.list_personas())
            elif p == "/api/kb":
                st = manage.get_settings()
                kbs = astrbot_api.kb_list()
                for k in kbs:
                    k["folder"] = str(manage.kb_folder(k["kb_name"], create=False))
                self._json({"kbs": kbs, "embeddings": manage.embedding_providers(),
                            "used": st.get("kb_names", []), "agentic": st.get("kb_agentic", False),
                            "ask_enable": st.get("ask_enable"), "ask_limit": st.get("ask_limit"),
                            "ask_owner": st.get("ask_owner"), "admins": st.get("admins", [])})
            elif p == "/api/kb/docs":
                self._json(astrbot_api.kb_docs(q.get("kb_id", "")))
            elif p == "/api/kb/task":
                self._json(astrbot_api.kb_task(q.get("task_id", "")))
            elif p == "/api/session-persona":
                svc = astrbot_api.get_session_service(q.get("umo", ""))
                self._json({"persona": svc.get("persona_id", ""), "tts": svc.get("tts_enabled", True)})
            elif p == "/api/versions":
                st = versions.status()
                st["changelog"] = versions.changelog()
                st["sections"] = versions.changelog_sections()
                self._json(st)
            elif p == "/api/notice":
                self._json(notice_info())
            elif p == "/api/stats":
                self._json(astrbot_api.token_stats(int(q.get("days", "7"))))
            elif p == "/api/usage":
                self._json(usage.overview(q.get("range", "7")))
            elif p == "/api/usage/target":
                self._json(usage.target(q.get("key", "")))
            elif p == "/api/memory":
                self._json(memory.overview())
            elif p == "/api/memory/graph":
                self._json(memory.graph(q.get("persona", ""), q.get("archived") == "1"))
            elif p == "/api/memory/facts":
                self._json(memory.facts(q.get("scope", "owner"), q.get("all") == "1"))
            elif p == "/api/memory/diaries":
                self._json(memory.diaries(q.get("scope", "owner"), q.get("persona", "")))
            elif p == "/api/memory/followups":
                self._json(memory.followups(q.get("scope", "owner")))
            elif p == "/api/memory/preview":
                self._json({"text": memory.preview(q.get("scope", "owner"), q.get("persona", ""))})
            elif p == "/api/memory/history":
                self._json(memory.history())
            elif p == "/api/memory/job":
                self._json(memory.job(int(q.get("id", "0") or 0)))
            elif p == "/api/memory/replay":
                self._json({"questions": memory.REPLAY, "last": MEMORY_REPLAY})
            elif p == "/api/wizard/state":
                s = load_state()
                self._json({"setup_done": s["setup_done"], "home": s["home"] or installer.default_home(),
                            "wizard": s["wizard"], "bot_qq": s["bot_qq"], "owners": s["owners"],
                            "dashboard_user": s["dashboard_user"], "kit": str(KIT)})
            elif p == "/api/updates":
                self._json(updater.info(force=q.get("force") == "1"))
            elif p == "/api/group-rules":
                self._json(manage.group_rules())
            elif p == "/api/memes":
                self._json({"packs": astrbot_api.meme_packs(), "map": astrbot_api.meme_persona_map()})
            elif p == "/api/astrbot-login-url":
                self._json({"url": dashlogin.login_url()})
            elif p == "/api/dashboard-cred":
                self._json(dashlogin.credentials())
            elif p == "/api/wizard/check-env":
                self._json(installer.check_env(q.get("home") or installer.default_home()))
            else:
                self._json({"error": "not found"}, 404)
        except astrbot_api.ApiError as e:
            self._json({"error": str(e), "need_login": str(e) == "NEED_LOGIN"}, 200)
        except (usage.UsageError, memory.MemoryError_) as e:
            self._json({"error": str(e)}, 200)
        except Exception as e:
            log_exc()
            self._json({"error": str(e)}, 200)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._guard():
            return
        p = urllib.parse.urlparse(self.path).path
        try:
            b = self._body()
            if p == "/api/action":
                act = ACTIONS.get(b.get("action"))
                if not act:
                    return self._json({"error": "未知操作"}, 400)
                self._ok(services.run_job(act[0], act[1]))
            elif p == "/api/settings":
                self._ok(services.run_job("保存设置", services.with_astrbot_stopped(lambda: manage.apply_settings(b))))
            elif p == "/api/console-settings":
                kw = {k: bool(b[k]) for k in ("autostart", "watchdog", "notify") if k in b}
                update_state(**kw)
                self._json({"ok": True})
            elif p == "/api/backup":
                inc = bool(b.get("include_memes"))
                self._ok(services.run_job("备份", services.with_astrbot_stopped(lambda: manage.do_backup(inc))))
            elif p == "/api/restore":
                name = str(b.get("name", ""))
                self._ok(services.run_job("还原备份", services.with_astrbot_stopped(lambda: manage.do_restore(name))))
            elif p == "/api/logs/export":
                self._json(logtools.export(str(b.get("src", "astrbot")), str(b.get("start", "")), str(b.get("end", "")),
                                           bool(b.get("problems")), bool(b.get("mask"))))
            elif p == "/api/report":
                self._json(report.build(b.get("context") if isinstance(b.get("context"), dict) else None))
            elif p == "/api/explain":
                self._json(troubles.explain(str(b.get("msg", "")), [str(x) for x in (b.get("lines") or [])][-200:],
                                            str(b.get("name", ""))))
            elif p == "/api/memory/edit":
                self._json({"ok": True, "row": memory.edit(str(b.get("tbl", "")), int(b.get("id") or 0), dict(b.get("fields") or {}))})
            elif p == "/api/memory/fact-add":
                self._json({"ok": True, "id": memory.add_fact(str(b.get("scope") or "owner"), str(b.get("text") or ""),
                                                              str(b.get("category") or ""), str(b.get("tier") or "long"))})
            elif p == "/api/memory/followup-add":
                fid = memory.add_followup(str(b.get("scope") or "owner"), str(b.get("due") or ""), str(b.get("text") or ""))
                self._json({"ok": bool(fid), "id": fid, "error": "" if fid else "日期或内容不对"})
            elif p == "/api/memory/run":
                self._json({"ok": True, "job": memory.enqueue(str(b.get("kind") or "consolidate"))})
            elif p == "/api/memory/replay":
                qs = [str(x) for x in (b.get("questions") or [])]

                def _replay():
                    MEMORY_REPLAY.clear()
                    MEMORY_REPLAY.extend(memory.replay(qs or None))
                    return f"回放完成：{len(MEMORY_REPLAY)} 题"
                self._ok(services.run_job("回放测试", _replay))
            elif p == "/api/reveal":
                f = Path(str(b.get("path", "")))
                if f.exists() and LOG_DIR in f.resolve().parents:
                    logtools.reveal(f)
                self._json({"ok": True})
            elif p == "/api/tray-panel":          # 在鼠标位置弹出托盘面板（检查面板能不能正常显示用）
                import tray
                f = tray._flyout
                if f is not None and f.ok:
                    f.show()
                    time.sleep(0.5)
                self._json({"ok": bool(f is not None and f.ok), "visible": bool(f is not None and f.ok and f.visible())})
            elif p == "/api/open-folder":
                target = {"backups": manage.backups_dir(), "kit": KIT, "logs": LOG_DIR}.get(b.get("which"), KIT)
                os.startfile(str(target))  # noqa
                self._json({"ok": True})
            elif p == "/api/open-window":
                self._json({"ok": True, "result": open_console_window()})
            elif p == "/api/restart-console":
                self._json({"ok": True})
                restart_self()
            elif p == "/api/update-from-zip":
                self._ok(services.run_job("更新控制台", lambda: update_from_zip(b.get("path", ""))))
            elif p == "/api/uninstall":
                full = bool(b.get("full"))
                self._ok(services.run_job("卸载", lambda: uninstall(full)))
            elif p == "/api/dashboard-login":
                ok = astrbot_api.save_login(b.get("user", ""), b.get("password", ""))
                self._json({"ok": ok, "msg": "" if ok else "账号或密码不对（或者 AstrBot 没在运行）"})
            elif p == "/api/updates/run":
                fn = {"astrbot": ("更新 AstrBot", updater.update_astrbot), "napcat": ("更新 NapCat", updater.update_napcat),
                      "napcat_full": ("完整更新 NapCat（含 QQ 内核）", updater.update_napcat_full)}.get(b.get("comp"))
                if not fn:
                    return self._json({"error": "未知组件"}, 400)
                self._ok(services.run_job(*fn))
            elif p == "/api/updates/rollback":
                fn = {"astrbot": ("回退 AstrBot 版本", updater.rollback_astrbot),
                      "napcat": ("回退 NapCat 版本", updater.rollback_napcat)}.get(b.get("comp"))
                if not fn:
                    return self._json({"error": "未知组件"}, 400)
                self._ok(services.run_job(*fn))
            elif p == "/api/updates/delete-backup":
                self._json({"ok": True, "msg": updater.delete_backup(str(b.get("comp", "")))})
            elif p == "/api/notice/seen":
                rel = versions.latest_release() or {}
                update_state(notice={"last_date": time.strftime("%Y-%m-%d"), "last_version": rel.get("version", ""),
                                     "only_new": bool(b.get("only_new"))})
                self._json({"ok": True})
            elif p == "/api/group-rules/mute":
                manage.group_mute(str(b["gid"]), float(b.get("minutes", -1)))
                self._json({"ok": True})
            elif p == "/api/group-rules/note-add":
                self._json({"ok": True, "id": manage.group_note_add(str(b["gid"]), b.get("text", ""))})
            elif p == "/api/group-rules/note-del":
                manage.group_note_del(str(b["gid"]), int(b["id"]))
                self._json({"ok": True})
            elif p == "/api/group-rules/note-restore":
                manage.group_note_restore(str(b["gid"]), dict(b["note"]))
                self._json({"ok": True})
            elif p == "/api/session-services":
                self._json(astrbot_api.all_session_services([str(u) for u in b.get("umos", [])]))
            elif p == "/api/memes/persona":
                astrbot_api.meme_set_persona(str(b.get("persona", "")), list(b.get("pack_ids") or []))
                self._json({"ok": True})
            elif p == "/api/memes/create":
                self._json({"ok": True, "pack": astrbot_api.meme_create_pack(str(b.get("name", "")).strip())})
            elif p == "/api/dashboard-reset":
                self._ok(services.run_job("重置 AstrBot 后台密码", services.with_astrbot_stopped(dashlogin.reset)))
            elif p == "/api/personas/save":
                astrbot_api.save_persona(b["persona_id"].strip(), b.get("system_prompt", ""), bool(b.get("is_new")))
                self._json({"ok": True})
            elif p == "/api/personas/delete":
                astrbot_api.delete_persona(b["persona_id"])
                self._json({"ok": True})
            elif p == "/api/session-persona":
                astrbot_api.set_session_persona(b["umo"], b.get("persona", ""))
                self._json({"ok": True})
            elif p == "/api/session-tts":
                astrbot_api.set_session_service(b["umo"], tts_enabled=None if b.get("enabled", True) else False)
                self._json({"ok": True})
            elif p == "/api/kb/create":
                kb = astrbot_api.kb_create(b["name"].strip(), b["embedding_provider_id"],
                                           b.get("description", ""), b.get("emoji") or "📚")
                if not (isinstance(kb, dict) and kb.get("kb_id")):
                    raise RuntimeError(f"知识库没建成：{str(kb)[:200]}")
                self._json({"ok": True, "kb": kb})
            elif p == "/api/kb/delete":
                astrbot_api.kb_delete(b["kb_id"])
                self._json({"ok": True})
            elif p == "/api/kb/upload":
                import base64
                data = base64.b64decode(b["content_b64"])
                tid = astrbot_api.kb_upload(b["kb_id"], b["filename"], data)
                if b.get("kb_name"):
                    manage.kb_save_copy(b["kb_id"], b["kb_name"], b["filename"], data)
                self._json({"ok": True, "task_id": tid})
            elif p == "/api/kb/upload-local":
                from pathlib import Path as _P
                f = _P(b["path"]).resolve()
                root = KIT.parent.resolve()
                if root not in f.parents or not f.is_file():
                    raise ValueError("只能上传整合包所在文件夹里的文件")
                self._json({"ok": True, "task_id": astrbot_api.kb_upload(b["kb_id"], f.name, f.read_bytes())})
            elif p == "/api/versions/switch":
                ref = str(b.get("ref", ""))
                self._ok(services.run_job("切换版本", lambda: (versions.switch(ref), threading.Timer(1.0, restart_self).start(),
                                                          f"已切换到 {ref}，控制台马上重启")[2]))
            elif p == "/api/versions/latest":
                self._ok(services.run_job("切换版本", lambda: (versions.latest(), threading.Timer(1.0, restart_self).start(),
                                                          "已回到最新版，控制台马上重启")[2]))
            elif p == "/api/versions/snapshot":
                msg = str(b.get("message") or "").strip() or f"手动保存（{time.strftime('%Y-%m-%d %H:%M')}）"
                cid = versions.snapshot(msg)
                self._json({"ok": True, "commit": cid, "msg": "已保存" if cid else "没有改动"})
            elif p == "/api/kb/sync":
                kid, kname = b["kb_id"], b["kb_name"]
                self._ok(services.run_job("同步知识库", lambda: manage.kb_sync(kid, kname)))
            elif p == "/api/kb/open-folder":
                os.startfile(str(manage.kb_folder(b["kb_name"])))  # noqa
                self._json({"ok": True})
            elif p == "/api/kb/doc-delete":
                astrbot_api.kb_doc_delete(b["kb_id"], b["doc_id"])
                if b.get("kb_name") and b.get("doc_name"):
                    manage.kb_remove_copy(b["kb_id"], b["kb_name"], b["doc_name"])
                self._json({"ok": True})
            elif p == "/api/kb/search":
                self._json({"ok": True, "results": astrbot_api.kb_search(b["kb_id"], b["kb_name"], b["query"])})
            elif p == "/api/kb/use":
                kv = {"kb_names": [n for n in b.get("names", []) if isinstance(n, str)], "kb_agentic": bool(b.get("agentic", True))}
                for k in ("ask_enable", "ask_limit", "ask_owner"):
                    if b.get(k) is not None:
                        kv[k] = b[k]
                self._ok(services.run_job("保存设置", services.with_astrbot_stopped(lambda: manage.apply_settings(kv))))
            elif p == "/api/test-chat":
                if b.get("umo"):
                    self._json({"ok": True, **astrbot_api.dialogue_test(b)})
                else:
                    self._json({"ok": True, "reply": astrbot_api.test_chat(b.get("session", "dafeiyu-console-test"), b.get("text", ""))})
            elif p == "/api/models/save":
                self._json(modelhub.save(b))
            elif p == "/api/models/assign":
                self._json(modelhub.assign(b))
            elif p == "/api/models/test":
                self._json(modelhub.test(str(b.get("id", ""))))
            elif p == "/api/models/image-rules":
                self._json(modelhub.apply_image_rules())
            elif p == "/api/ensure-onebot-http":
                added = manage.ensure_onebot_http()
                if added:
                    services.run_job("重启 NapCat", lambda: (services.stop_napcat(), time.sleep(3), services.start_napcat())[2])
                self._json({"ok": True, "restarting": added})
            # ---------------- 向导
            elif p == "/api/wizard/save":
                s = load_state()
                w = s["wizard"]
                w.update({k: v for k, v in b.items() if k not in ("password", "deepseek_key", "keys")})
                update_state(wizard=w, setup_step=b.get("step", s["setup_step"]))
                self._json({"ok": True})
            elif p == "/api/wizard/install-astrbot":
                self._ok(services.run_job("安装 AstrBot", lambda: installer.install_astrbot(b["home"], b.get("user") or "", b.get("password") or "")))
            elif p == "/api/wizard/install-napcat":
                self._ok(services.run_job("安装 NapCat", installer.install_napcat))
            elif p == "/api/wizard/check-deepseek":
                self._json(manage.check_deepseek(b.get("key", "").strip()))
            elif p == "/api/wizard/configure":
                self._ok(services.run_job("写入配置", lambda: installer.configure(
                    str(b.get("bot_qq", "")).strip(), [str(x).strip() for x in b.get("owners", [])],
                    [str(x).strip() for x in b.get("groups", [])], b.get("deepseek_key", "").strip(), b.get("model_connection"))))
            elif p == "/api/wizard/start-login":
                self._ok(services.run_job("启动并等待扫码", installer.start_for_login))
            elif p == "/api/wizard/import":
                self._ok(services.run_job("导入配置", lambda: installer.import_pack(b.get("voice", "edge"), b.get("memory", "off"), b.get("keys", {}))))
            elif p == "/api/wizard/wait-test":
                self._ok(services.run_job("等待测试消息", installer.wait_test))
            elif p == "/api/wizard/finish":
                self._ok(services.run_job("完成安装", installer.finish))
            else:
                self._json({"error": "not found"}, 404)
        except astrbot_api.ApiError as e:
            self._json({"ok": False, "error": str(e), "need_login": str(e) == "NEED_LOGIN"})
        except (memory.MemoryError_, KeyError) as e:
            self._json({"ok": False, "error": str(e).strip("'")})
        except Exception as e:
            log_exc()
            self._json({"ok": False, "error": str(e)})


def _bind() -> ThreadingHTTPServer:
    """先用上次的端口，绑不上（被系统保留或被别的程序占了）就依次换，成功的记下来。"""
    last = console_port()
    errs = []
    for port in [last] + [p for p in CONSOLE_PORTS if p != last]:
        if port_open(port):                  # 有别的程序在听这个端口（是控制台的话前面已经认出来了）
            errs.append(f"{port}（被别的程序占用）")
            continue
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            errs.append(f"{port}（{'被系统保留' if getattr(e, 'winerror', None) == 10013 else e}）")
            continue
        if errs:
            log("控制台端口 " + "、".join(errs) + f"，改用 {port}")
        if port != last or "console_port" not in load_state():
            update_state(console_port=port)
        return srv
    raise RuntimeError("控制台找不到能用的端口：" + "、".join(errs))


def _fatal(msg: str) -> None:
    """启动失败时弹一个窗口说明，不让人对着「双击没反应」。"""
    log(msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "大肥鱼控制台", 0x10)
    except Exception:
        pass


def main():
    if "--wait" in sys.argv:                 # 后台重启：等旧进程让出端口
        for _ in range(40):
            if not console_alive(console_port()):
                break
            time.sleep(0.5)
    if console_alive(console_port()):        # 已经在运行：只把窗口打开
        if "--open" in sys.argv:
            open_console_window()
        return
    try:
        srv = _bind()
    except RuntimeError as e:
        return _fatal(f"{e}\n\n可以重启电脑后再打开；还不行就把整合包 logs 文件夹里的 console.log 发给作者。")
    log(f"控制台 {VERSION} 启动（端口 {srv.server_address[1]}）")
    try:                # 控制台进程意外崩溃时（不是普通报错），把现场写进 logs/console.crash.log
        import faulthandler
        faulthandler.enable(open(LOG_DIR / "console.crash.log", "a", encoding="utf-8"))
    except Exception:
        log_exc()
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=services.status_refresher, daemon=True).start()
    threading.Thread(target=services.watchdog, daemon=True).start()
    threading.Thread(target=updater.auto_check_loop, daemon=True).start()
    threading.Thread(target=lambda: log("版本管理：" + versions.ensure_repo()), daemon=True).start()
    threading.Thread(target=plugin_sync.sync_when_ready, daemon=True).start()
    s = load_state()
    if "--boot" in sys.argv and s["setup_done"] and s.get("autostart"):
        services.run_job("开机自动启动", services.start_all)
    if "--open" in sys.argv:
        threading.Timer(0.5, open_console_window).start()
    try:
        import tray
        tray.run()  # 阻塞在这里，直到退出
    except Exception:
        log_exc()
    while True:  # 没有托盘时保持运行
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        _fatal("控制台没能启动，原因已经记在整合包 logs 文件夹的 console.log 里。\n\n"
               "可以先重启电脑再打开；还不行就把 console.log 发给作者。")
        raise
