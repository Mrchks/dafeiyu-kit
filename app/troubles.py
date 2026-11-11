"""把报错翻译成「问题卡」：发生了什么、可能的原因、怎么办（编号步骤 + 可以直接点的按钮）。

安装、更新、运行中的任务失败时，services.run_job 会调用 explain()，结果放在 job["trouble"] 里，
界面据此显示问题卡；原始报错只在「查看详细记录」里给需要的人看。

按钮的 key（界面负责执行）：
  retry 重试刚才那一步      report 生成问题报告      logs 打开日志页      env 回到环境检查
  qr 扫码登录              start_astrbot / restart_napcat / restart_astrbot 启动或重启
  topup 打开 DeepSeek 充值页  deepseek_key 打开 DeepSeek 密钥页
  settings 打开常用设置      diagnose 运行故障诊断    folder 打开整合包文件夹
新增一类问题：在 RULES 里加一条，patterns 用正则，从上往下第一个匹配的生效（越具体的放越前面）。
"""

from __future__ import annotations

import re

RULES: list[dict] = [
    {
        "code": "update_verify",
        "patterns": [r"更新包校验失败", r"签名校验失败"],
        "title": "更新包未通过校验",
        "cause": "下载到的更新包与作者发布的内容不一致，可能是下载加速站缓存了旧文件、传输过程出错，或文件被篡改。出于安全考虑未进行安装，控制台没有任何改动。",
        "steps": ["点击「重试」，控制台会更换下载地址重新下载。",
                  "多次失败时，可在「版本与更新」中点击「下载完整包」，从发布页手动下载新版，再使用「从压缩包更新」安装。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "update_download",
        "patterns": [r"下载更新失败", r"读取更新信息失败"],
        "title": "无法连接更新服务器",
        "cause": "控制台的更新发布在 GitHub，国内直连不稳定；本次直连和各加速站均未连接成功。控制台没有任何改动。",
        "steps": ["几分钟后点击「重试」，加速站的可用性会有波动。",
                  "更换网络后重试（例如使用手机热点），或开启代理、加速器后重试。"],
        "actions": [("retry", "重试")],
    },
    {
        "code": "download_incomplete",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"Unexpected end of archive", r"BadZipFile", r"File is not a zip file", r"Data Error", r"CRC Failed",
                     r"解压\S*失败", r"文件不完整", r"大小对不上"],
        "title": "下载的文件不完整",
        "cause": "下载中途中断，文件只下载了一部分。通常是网络不稳定，或当时使用的下载加速站出现故障，与本机无关。",
        "steps": ["点击「重试」，控制台会删除不完整的文件，并更换下载地址重新下载。",
                  "连续失败两三次后，请更换网络重试（例如使用手机热点）。",
                  "平时使用代理或加速器的，请开启后再重试。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "virus_blocked",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"WinError 225", r"contains a virus", r"病毒", r"潜在的不需要", r"Windows Defender", r"被安全软件"],
        "title": "被杀毒软件拦截",
        "cause": "杀毒软件（Windows 安全中心、360、火绒等）将整合包中的程序识别为可疑文件，并予以删除或隔离。整合包中的程序均来自 AstrBot、NapCat 官方。",
        "steps": ["打开杀毒软件的「隔离区」或「保护历史记录」，恢复被拦截的文件，并将整合包所在的文件夹加入信任（排除项）。",
                  "返回此处点击「重试」。"],
        "actions": [("retry", "重试"), ("folder", "打开整合包文件夹")],
    },
    {
        "code": "disk_full",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"No space left", r"Errno 28", r"WinError 112", r"磁盘空间不足", r"空间不够"],
        "title": "磁盘空间不足",
        "cause": "安装位置所在磁盘的剩余空间不足。QQ 内核、AstrBot 和 Python 组件共需约 3 GB。",
        "steps": ["清理该磁盘（回收站、下载文件夹），或返回「安装位置与环境检查」，更换剩余空间较大的磁盘。",
                  "释放空间后点击「重试」。"],
        "actions": [("retry", "重试"), ("env", "更换安装位置")],
    },
    {
        "code": "port_busy",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"WinError 10048", r"Address already in use", r"端口\S*(被占用|占用)"],
        "title": "端口被其他程序占用",
        "cause": "AstrBot 或 NapCat 需要使用的端口已被其他程序占用，常见情况是此前运行的其他机器人程序，或旧版本的控制台未完全退出。",
        "steps": ["关闭其他 QQ 机器人程序；托盘中如有旧的控制台图标，请右键退出。",
                  "仍未解决时，重启电脑后再打开控制台。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "file_locked",
        "patterns": [r"WinError 5\b", r"WinError 32", r"PermissionError", r"Access is denied", r"拒绝访问", r"另一个程序正在使用"],
        "title": "文件被占用或没有权限",
        "cause": "需要修改的文件正被其他程序占用（例如 QQ、AstrBot 仍在运行，或该文件夹正在资源管理器中打开），也可能被杀毒软件锁定。",
        "steps": ["点击总览页右上角的「全部停止」，并关闭已打开整合包文件夹的资源管理器窗口。",
                  "再点击「重试」。仍未解决时，重启电脑后重试。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "pip_failed",
        "patterns": [r"pip 退出码", r"No matching distribution", r"Could not find a version", r"pip install", r"装 AstrBot 失败", r"AstrBot 安装失败"],
        "title": "AstrBot 所需组件未安装成功",
        "cause": "AstrBot 运行需要一批 Python 组件，这些组件从国内镜像源下载。本次下载未完成，通常是网络不稳定所致。",
        "steps": ["点击「重试」，已安装的部分会自动跳过，不会重新开始。",
                  "连续失败时，请更换网络重试，或关闭代理后重试（部分代理会阻断国内镜像源）。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "network",
        "patterns": [r"urlopen error", r"timed out", r"TimeoutError", r"ConnectionReset", r"ConnectionRefused(?!.*6185)",
                     r"getaddrinfo", r"Name or service not known", r"远程主机强迫关闭", r"WinError 1005\d", r"WinError 10060",
                     r"SSL", r"CERTIFICATE", r"所有加速站", r"下载失败", r"连不上 GitHub", r"无法连接 GitHub", r"Max retries exceeded"],
        "title": "无法连接下载地址",
        "cause": "所需文件存放在 GitHub 等境外网站，国内直连不稳定。整合包已自动尝试多个加速站，本次均未连接成功。",
        "steps": ["几分钟后点击「重试」，加速站的可用性会有波动。",
                  "更换网络后重试（例如使用手机热点），或开启代理、加速器后重试。",
                  "返回「安装位置与环境检查」并点击「检查」，可以查看具体是哪个地址无法连接。"],
        "actions": [("retry", "重试"), ("env", "检查网络")],
    },
    {
        "code": "deepseek_key",
        "patterns": [r"\b401\b", r"Authentication", r"invalid.{0,10}api.?key", r"API key", r"key 不对", r"密钥不对", r"Unauthorized"],
        "title": "DeepSeek 密钥无效",
        "cause": "填写的 DeepSeek 密钥（以 sk- 开头的字符串）不正确，或已在 DeepSeek 网站上被删除。",
        "steps": ["打开 DeepSeek 开放平台的「API keys」页面，新建一个密钥并复制。",
                  "返回此步骤粘贴新密钥，再点击「检查」或「重试」。"],
        "actions": [("deepseek_key", "打开 DeepSeek 密钥页"), ("retry", "重试")],
    },
    {
        "code": "deepseek_balance",
        "patterns": [r"Insufficient Balance", r"\b402\b", r"余额不足", r"余额为 ?0"],
        "title": "DeepSeek 余额不足",
        "cause": "bot 每次回复都需要调用一次 DeepSeek，余额用尽后无法回复消息。",
        "steps": ["打开 DeepSeek 开放平台充值，小额充值即可使用较长时间。", "充值后几分钟内生效，无需重启。"],
        "actions": [("topup", "前往充值")],
    },
    {
        "code": "qq_risk",
        "patterns": [r"风控", r"冻结", r"安全验证", r"登录失败.{0,20}(异常|限制)", r"账号.{0,6}(异常|受限)"],
        "title": "QQ 小号登录受限",
        "cause": "腾讯检测到该账号通过第三方程序登录，要求进行验证或限制了登录。新注册的账号更容易出现此情况。",
        "steps": ["使用手机 QQ 登录该小号，按提示完成安全验证；账号被冻结时，按提示解冻。",
                  "验证完成后，返回此处点击「重试」或重新扫码。",
                  "频繁被限制时，建议更换注册时间较长的小号。"],
        "actions": [("qr", "扫码登录"), ("retry", "重试")],
    },
    {
        "code": "qq_login",
        "patterns": [r"二维码", r"扫码", r"need_qr", r"登录(超时|过期)", r"没登录", r"未登录"],
        "title": "QQ 小号尚未登录",
        "cause": "需要使用已登录小号的手机 QQ 扫码，确认登录后 bot 才能收发消息。",
        "steps": ["点击「扫码登录」，使用已登录小号的手机 QQ，通过右上角的「扫一扫」扫描电脑上的二维码，并在手机上确认。",
                  "截图无法用于扫码，需要直接对着屏幕扫描。二维码过期后会自动刷新。"],
        "actions": [("qr", "扫码登录")],
    },
    {
        "code": "test_no_message",
        "patterns": [r"未收到测试消息"],
        "title": "未收到测试消息",
        "cause": "测试消息需要由主人的 QQ 号私聊小号发送。使用其他账号发送、发送到群内，或小号尚未登录时，都无法收到。",
        "steps": ["确认上一步扫码登录成功（总览页中 QQ 显示「在线」）。",
                  "使用主人的 QQ 号打开与小号的私聊，发送一句「在吗」。",
                  "再点击「重试」，等待 1–2 分钟。"],
        "actions": [("retry", "重试"), ("diagnose", "故障诊断")],
    },
    {
        "code": "test_no_reply",
        "patterns": [r"已收到测试消息，但 bot 没有回复"],
        "title": "已收到消息，但 bot 没有回复",
        "cause": "常见原因包括：DeepSeek 密钥无效或余额不足、AstrBot 仍在启动，或主人的 QQ 号未加入白名单。",
        "steps": ["点击「故障诊断」，按诊断结果中的提示处理。", "处理完成后点击「重试」。"],
        "actions": [("diagnose", "故障诊断"), ("retry", "重试")],
    },
    {
        "code": "astrbot_down",
        "patterns": [r"NEED_LOGIN", r"AstrBot 没在(运行|跑)", r"AstrBot 未运行", r"6185.{0,40}(refused|拒绝)", r"连不上 AstrBot", r"无法连接 AstrBot"],
        "title": "AstrBot 未运行",
        "cause": "AstrBot 负责生成回复；它未运行时，bot 不会回复任何消息，管理后台也无法打开。",
        "steps": ["点击「启动 AstrBot」，等待约半分钟至一分钟。", "启动后仍未解决时，点击「故障诊断」。"],
        "actions": [("start_astrbot", "启动 AstrBot"), ("diagnose", "故障诊断")],
    },
    {
        "code": "napcat_install",
        "patterns": [r"NapCat 没装好", r"NapCat 安装未完成", r"NapCatInstaller", r"NapCat 目录不完整"],
        "title": "NapCat 安装未完成",
        "cause": "NapCat 安装器需要下载 QQ 内核（约 300MB）和 NapCat 本体。最常见的原因是下载中途中断，其次是被杀毒软件拦截。",
        "steps": ["点击「重试」，控制台会检查并删除未下载完成的文件，然后重新下载。",
                  "连续失败时，请更换网络重试，或检查杀毒软件是否进行了拦截（隔离区中是否有 QQ、NapCat 相关文件）。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "path_bad",
        "patterns": [r"路径.{0,10}(中文|空格|英文)", r"UnicodeEncodeError"],
        "title": "安装位置的路径不符合要求",
        "cause": "AstrBot 和 NapCat 不支持包含中文或空格的路径，例如「C:\\用户\\新建 文件夹」。",
        "steps": ["返回「安装位置与环境检查」，更换为全英文且不含空格的文件夹，例如 D:\\Dafeiyu。"],
        "actions": [("env", "更换安装位置")],
    },
]

DEFAULT = {
    "code": "unknown",
    "title": "此步骤未完成",
    "cause": "遇到了整合包尚未收录的问题。",
    "steps": ["先点击「重试」，多数问题重试一次即可解决。",
              "仍未解决时，点击「生成问题报告」，并通过「反馈给作者」发送，无需自行截图描述。"],
    "actions": [("retry", "重试"), ("report", "生成问题报告")],
}


def explain(msg: str, lines: list[str] | None = None, name: str = "") -> dict:
    """msg 是任务的结果（出错信息），lines 是任务过程中的输出。优先按出错信息匹配，再看过程输出的最后一段。"""
    msg = str(msg or "")
    tail = "\n".join((lines or [])[-60:])
    rule = next((r for r in RULES if r.get("strong") and any(re.search(p, msg + "\n" + tail, re.I) for p in r["patterns"])), None)
    for text in (() if rule else (msg, tail)):
        for r in RULES:
            if any(re.search(p, text, re.I) for p in r["patterns"]):
                rule = r
                break
        if rule:
            break
    r = rule or DEFAULT
    raw = msg + ("\n\n" + tail if tail else "")
    return {"code": r["code"], "title": r["title"], "cause": r["cause"], "steps": list(r["steps"]),
            "actions": [{"key": k, "label": lb} for k, lb in r["actions"]], "job": name, "raw": raw[-6000:]}
