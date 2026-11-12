"""把整合包自带的插件同步进 AstrBot。

以前自带插件只在安装向导「导入大肥鱼」那一步装一次。之后用压缩包更新控制台，
新加的插件（比如「大肥鱼通用能力」）和改过的插件都进不了 AstrBot，要手动重新导入。
现在控制台每次启动（更新完会自动重启），等 AstrBot 跑起来后对一遍：
自带插件（同名 .json 里写了 always_reinstall 的）没装或者文件跟包里不一样，就重新上传安装。
只在语音选了某一档才装的插件（only_with_voice），没装过就不管，装过的照样更新。

另外做一次性调整（migrate）：配置包里改了默认值，只对新安装生效；已经装好的环境在控制台启动时补上，
每项只做一次（state.pack_migrate_rev 记到第几项）。
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import astrbot_api
import services
from core import PACK_DIR, astrbot_data, load_state, log, log_exc, update_state

PLUGINS = PACK_DIR / "plugins"


def _files(folder: Path) -> dict[str, Path]:
    out = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            p = Path(root) / f
            out[p.relative_to(folder).as_posix()] = p
    return out


def _zip(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, p in sorted(_files(folder).items()):
            zf.write(p, f"{folder.name}/{rel}")
    return buf.getvalue()


def bundled() -> list[dict]:
    out = []
    if not PLUGINS.is_dir():
        return out
    for d in sorted(PLUGINS.iterdir()):
        if not (d.is_dir() and (d / "metadata.yaml").is_file()):
            continue
        meta_file = d.with_suffix(".json")
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8-sig")) if meta_file.exists() else {}
        except ValueError:
            meta = {}
        if not meta.get("always_reinstall"):
            continue
        out.append({"name": meta.get("name") or d.name, "dir": d, "gated": bool(meta.get("only_with_voice"))})
    return out


def outdated() -> list[dict]:
    data = astrbot_data()
    if not data:
        return []
    todo = []
    opt_out = bool(load_state().get("plugins_opt_out"))     # 接入已有安装时没有勾选插件组：只更新已装的，不新装
    for p in bundled():
        inst = data / "plugins" / p["name"]
        if not inst.is_dir():
            if not p["gated"] and not opt_out:
                todo.append({**p, "why": "没装"})
            continue
        for rel, f in _files(p["dir"]).items():
            g = inst / rel
            if not g.is_file() or g.read_bytes() != f.read_bytes():
                todo.append({**p, "why": "有更新"})
                break
    return todo


def sync() -> list[str]:
    done = []
    for p in outdated():
        try:
            astrbot_api.upload_plugin(_zip(p["dir"]), p["dir"].name + ".zip")
            done.append(p["name"])
            log(f"自带插件：{p['name']}（{p['why']}）已装进 AstrBot")
        except Exception as e:  # noqa: BLE001
            log(f"自带插件：{p['name']} 没装上：{e}")
    return done


# ---------------------------------------------------------------- 一次性调整
# 1（v1.4.1）：「记一下」分成两处记。主人本人的信息记进长期记忆（只在那个私聊里召回），
#   资料类内容才记进知识库（群里也会检索）。以前只有知识库一个去处，主人的学校、口味被记进了群里也查得到的知识库。
#   - 打开长期记忆插件的主动记录工具 memorize_long_term_memory；
#   - 人设里配置包写的那句「记一下 → kb_remember」换成新规则（只换这一句，其他内容不动；改过这句的人设不动）。
# 2（v1.5.0）：换成「大肥鱼记忆」插件（日记 + 档案 + 待跟进，常驻上下文）。
#   - 长期记忆插件不再每轮自动塞 5 条记忆（recall_engine.top_k=0），也不再用它的主动记录工具；它的回忆工具留着查旧记忆；
#   - 人设里「记一下」那句的 memorize_long_term_memory 换成 memory_save。
MIGRATE_REV = 4
LM = "astrbot_plugin_livingmemory"
REMEMBER_LINE = ("- 主人让你记东西时，先分清记到哪里：主人自己的事（学校、工作、口味、习惯、日程、约定）用 memorize_long_term_memory "
                 "记进长期记忆，只在私聊里想得起来；规则、术语、版本资料这类群友也可能问到的内容，或者主人在回答你之前替群友问他的问题，"
                 "用 kb_remember 记进知识库，群里所有人都查得到。分不清就先问主人。记完说一声。")
REMEMBER_LINE_V2 = REMEMBER_LINE.replace("用 memorize_long_term_memory 记进长期记忆", "用 memory_save 记进你的记忆")
_OLD_REMEMBER = re.compile(r"^- 主人(?:跟你)?说「记一下」「记住这个」，或者在回答(?:你)?之前替群友问他的问题，就用 kb_remember 记进知识库，记完说一声。(?=\r?$)", re.M)


def _fix_personas(old, new: str) -> list[str]:
    """把人设里配置包写的那一句换掉。old 是正则或原句；人设里没有这句（自己改写过）就不动。"""
    done = []
    for p in astrbot_api.list_personas():
        sp = p.get("system_prompt") or ""
        if isinstance(old, str):
            n = sp.count(old)
            text = sp.replace(old, new)
        else:
            text, n = old.subn(new, sp)
        if n:
            astrbot_api.save_persona(p["persona_id"], text, False)
            done.append(p["persona_id"])
    return done


def _lm_settings(changes: dict) -> str:
    return _plugin_settings(LM, "长期记忆插件", changes)


def _plugin_settings(plugin: str, label: str, changes: dict) -> str:
    names = {p.get("name") for p in (astrbot_api.call("GET", "/api/v1/plugins") or []) if isinstance(p, dict)}
    if plugin not in names:
        return f"未安装{label}，跳过"
    r = astrbot_api.call("GET", f"/api/v1/plugins/config?plugin_id={plugin}")
    conf = r.get("config") if isinstance(r, dict) else None
    if not isinstance(conf, dict):
        raise RuntimeError(f"无法读取{label}的设置")
    diff = False
    for path, v in changes.items():
        cur = conf
        keys = path.split(".")
        for k in keys[:-1]:
            if not isinstance(cur.get(k), dict):
                cur[k] = {}
            cur = cur[k]
        if cur.get(keys[-1]) != v:
            cur[keys[-1]] = v
            diff = True
    if diff:
        astrbot_api.call("PUT", "/api/v1/plugins/config", {"plugin_id": plugin, "config": conf})
    return "已调整" if diff else "无需调整"


def _step1() -> None:
    # 第 1 项原来还会打开长期记忆的主动记录工具；第 2 项又把它关掉了（改用大肥鱼记忆），这里不再打开
    names = _fix_personas(_OLD_REMEMBER, REMEMBER_LINE)
    log("一次性调整：人设中「记一下」的规则" + ("已更新：" + "、".join(names) if names else "无需更新"))


def _step2() -> None:
    m = _lm_settings({"recall_engine.top_k": 0, "agent_tools.enable_memorize_tool": False})
    log(f"一次性调整：长期记忆插件不再每轮自动注入、关闭主动记录工具（{m}），改由大肥鱼记忆负责")
    names = _fix_personas(REMEMBER_LINE, REMEMBER_LINE_V2)
    log("一次性调整：人设中「记一下」改用 memory_save" + ("：" + "、".join(names) if names else "（无需更新）"))


def _caption_provider() -> str:
    """群聊识图用的模型：AstrBot「图片转述模型」，没填就用默认对话模型。"""
    try:
        from core import cmd_config_path
        c = json.loads(cmd_config_path().read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return ""
    pid = (c.get("provider_settings") or {}).get("default_image_caption_provider_id") or ""
    if not pid:
        pid = (((c.get("agent_runner") or {}).get("config") or {}).get("model") or {}).get("provider_id") or ""
    return pid


def _step3() -> None:
    m = _plugin_settings("astrbot_plugin_group_chat_plus", "群聊增强插件", {
        "enable_image_processing": True,
        "image_to_text_scope": "all",
        "image_to_text_provider_id": _caption_provider(),
        "image_to_text_prompt": "请用中文简要描述这张图片；图里有文字（题目、选项、聊天记录等）就把文字原样写出来。",
        "enable_image_description_cache": True,
    })
    log(f"一次性调整：群聊中的图片转为文字后提供给 bot（{m}）")


def _step4() -> None:
    """Batch-trim existing default histories without overriding custom limits."""
    data = astrbot_api.call("GET", "/api/v1/config-profiles/default")
    conf = data.get("config") if isinstance(data, dict) else None
    if not isinstance(conf, dict):
        raise RuntimeError("无法读取默认对话配置，未调整历史截断")
    compression = conf.get("agent_runner", {}).get("config", {}).get("compression", {})
    if compression.get("max_turns") == 30 and compression.get("trim_turns", 1) == 1:
        compression["trim_turns"] = 10
        astrbot_api.call("PUT", "/api/v1/config-profiles/default", conf)
        saved = astrbot_api.call("GET", "/api/v1/config-profiles/default")
        actual = saved.get("config", {}).get("agent_runner", {}).get("config", {}).get("compression", {})
        if actual.get("trim_turns") != 10:
            raise RuntimeError("历史截断设置未保存，将在下次启动时重试")
        log("一次性调整：历史上限保留 30 轮，达到上限后每次截断 10 轮")
    else:
        log("一次性调整：历史截断已调整或使用自定义值，保留当前设置")


STEPS = {1: _step1, 2: _step2, 3: _step3, 4: _step4}


def migrate() -> None:
    rev = int(load_state().get("pack_migrate_rev") or 0)
    for n in sorted(STEPS):
        if n <= rev:
            continue
        try:
            STEPS[n]()
            update_state(pack_migrate_rev=n)
        except Exception as e:  # noqa: BLE001
            log(f"一次性调整第 {n} 项未完成，下次启动控制台时重试：{e}")
            return


def sync_when_ready(max_wait: int = 900) -> None:
    """控制台启动时在后台调用：等 AstrBot 跑起来、插件加载完，再同步一次。"""
    try:
        if not load_state().get("setup_done"):
            return
        t0 = time.time()
        while time.time() - t0 < max_wait:
            if services.astrbot_info()["running"]:
                break
            time.sleep(10)
        else:
            return
        time.sleep(20)   # 等 AstrBot 把插件加载完
        migrate()
        if not outdated():
            return
        names = sync()
        if names:
            log("自带插件已同步：" + "、".join(names))
    except Exception:
        log_exc()
