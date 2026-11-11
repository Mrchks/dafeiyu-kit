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
        "code": "download_incomplete",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"Unexpected end of archive", r"BadZipFile", r"File is not a zip file", r"Data Error", r"CRC Failed",
                     r"解压\S*失败", r"文件不完整", r"大小对不上"],
        "title": "下载的文件不完整",
        "cause": "下载到一半断开了，文件只下到了一部分。一般是网络不稳定，或者当时用到的那个下载加速站出了问题，和你的电脑没有关系。",
        "steps": ["点「重试」：会删掉不完整的文件、换一个下载地址重新下载。",
                  "连续失败两三次，换一个网络再试（比如用手机开热点）。",
                  "平时开着代理或加速器的，打开后再试一次。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "virus_blocked",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"WinError 225", r"contains a virus", r"病毒", r"潜在的不需要", r"Windows Defender", r"被安全软件"],
        "title": "被杀毒软件拦住了",
        "cause": "杀毒软件（Windows 安全中心、360、火绒等）把整合包里的程序当成了可疑文件，删掉或隔离了。整合包里的程序都来自 AstrBot、NapCat 官方。",
        "steps": ["打开杀毒软件的「隔离区」或「保护历史记录」，把被拦下的文件恢复，并把整合包所在的文件夹加入信任（排除项）。",
                  "回到这里点「重试」。"],
        "actions": [("retry", "重试"), ("folder", "打开整合包文件夹")],
    },
    {
        "code": "disk_full",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"No space left", r"Errno 28", r"WinError 112", r"磁盘空间不足", r"空间不够"],
        "title": "磁盘空间不够",
        "cause": "安装位置所在的盘剩余空间不够。QQ 内核、AstrBot 和 Python 组件加起来大约需要 3 GB。",
        "steps": ["清理一下那个盘（回收站、下载文件夹），或者回到「安装位置与环境检查」换一个空间大的盘。",
                  "腾出空间后点「重试」。"],
        "actions": [("retry", "重试"), ("env", "更换安装位置")],
    },
    {
        "code": "port_busy",
        "strong": True,         # 过程输出里出现这类迹象就足以确定原因
        "patterns": [r"WinError 10048", r"Address already in use", r"端口\S*(被占用|占用)"],
        "title": "端口被别的程序占用",
        "cause": "AstrBot 或 NapCat 要用的端口已经被别的程序占着，常见的是之前开过的另一个机器人、旧版本的控制台没关干净。",
        "steps": ["关掉其他 QQ 机器人程序；托盘里有旧的控制台图标的，右键退出。",
                  "还不行就重启一次电脑，再打开控制台。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "file_locked",
        "patterns": [r"WinError 5\b", r"WinError 32", r"PermissionError", r"Access is denied", r"拒绝访问", r"另一个程序正在使用"],
        "title": "文件被占用或没有权限",
        "cause": "要改的文件正被别的程序打开着（比如 QQ、AstrBot 还在运行，或者文件夹正开在资源管理器里），或者被杀毒软件锁住了。",
        "steps": ["点总览右上角「全部停止」，关掉打开着整合包文件夹的资源管理器窗口。",
                  "再点「重试」。还不行就重启电脑后再试。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "pip_failed",
        "patterns": [r"pip 退出码", r"No matching distribution", r"Could not find a version", r"pip install", r"装 AstrBot 失败"],
        "title": "AstrBot 需要的组件没装上",
        "cause": "AstrBot 运行需要一批 Python 组件，要从国内镜像源下载。这次没下完，通常是网络不稳定。",
        "steps": ["点「重试」，已经装好的部分会跳过，不会从头来。",
                  "连续失败的话换一个网络再试，或者关掉代理再试（有的代理会挡住国内镜像源）。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "network",
        "patterns": [r"urlopen error", r"timed out", r"TimeoutError", r"ConnectionReset", r"ConnectionRefused(?!.*6185)",
                     r"getaddrinfo", r"Name or service not known", r"远程主机强迫关闭", r"WinError 1005\d", r"WinError 10060",
                     r"SSL", r"CERTIFICATE", r"所有加速站", r"下载失败", r"连不上 GitHub", r"Max retries exceeded"],
        "title": "网络连不上下载地址",
        "cause": "要下载的东西放在 GitHub 等国外网站上，国内直连不稳定。整合包已经自动换了几个加速站，这次都没连上。",
        "steps": ["等几分钟后点「重试」，加速站时好时坏。",
                  "换一个网络再试（比如用手机开热点），或者打开代理、加速器后再试。",
                  "回到「安装位置与环境检查」点「检查」，可以看到具体是哪个地址连不上。"],
        "actions": [("retry", "重试"), ("env", "检查网络")],
    },
    {
        "code": "deepseek_key",
        "patterns": [r"\b401\b", r"Authentication", r"invalid.{0,10}api.?key", r"API key", r"key 不对", r"密钥不对", r"Unauthorized"],
        "title": "DeepSeek 密钥不对",
        "cause": "填的 DeepSeek 密钥（以 sk- 开头的那一串）不对，或者在 DeepSeek 网站上被删掉了。",
        "steps": ["打开 DeepSeek 开放平台的「API keys」页面，新建一个密钥并复制。",
                  "回到这一步把新密钥粘贴进去，再点「检查」或「重试」。"],
        "actions": [("deepseek_key", "打开 DeepSeek 密钥页"), ("retry", "重试")],
    },
    {
        "code": "deepseek_balance",
        "patterns": [r"Insufficient Balance", r"\b402\b", r"余额不足", r"余额为 ?0"],
        "title": "DeepSeek 余额不足",
        "cause": "她每说一句话都要调用一次 DeepSeek，余额用完了就回不了消息。",
        "steps": ["打开 DeepSeek 开放平台充值，几块钱就能用很久。", "充值后几分钟内生效，不用重启。"],
        "actions": [("topup", "去充值")],
    },
    {
        "code": "qq_risk",
        "patterns": [r"风控", r"冻结", r"安全验证", r"登录失败.{0,20}(异常|限制)", r"账号.{0,6}(异常|受限)"],
        "title": "QQ 小号登录受限",
        "cause": "腾讯检测到这个号在用第三方程序登录，要求验证或限制了登录。新注册的号更容易出现。",
        "steps": ["用手机 QQ 登录这个小号，按提示完成安全验证；被冻结的按提示解冻。",
                  "验证完回来点「重试」或重新扫码。",
                  "经常被限制的话，建议换一个注册时间久一点的小号。"],
        "actions": [("qr", "扫码登录"), ("retry", "重试")],
    },
    {
        "code": "qq_login",
        "patterns": [r"二维码", r"扫码", r"need_qr", r"登录(超时|过期)", r"没登录"],
        "title": "QQ 小号还没登录",
        "cause": "需要用登着小号的手机 QQ 扫码，确认登录后她才能收发消息。",
        "steps": ["点「扫码登录」，用手机 QQ（登着小号）右上角「扫一扫」扫电脑上的二维码，在手机上点确认。",
                  "截图扫不了，要直接对着屏幕扫。二维码过期了会自动刷新。"],
        "actions": [("qr", "扫码登录")],
    },
    {
        "code": "test_no_message",
        "patterns": [r"没收到你的消息"],
        "title": "没收到你的测试消息",
        "cause": "要用主人号（你自己的大号）私聊小号。用别的号发、发在群里、或者小号还没登录，都收不到。",
        "steps": ["确认上一步扫码登录成功（总览里 QQ 显示「在线」）。",
                  "用主人号打开和小号的私聊，发一句「在吗」。",
                  "再点「重试」，等 1–2 分钟。"],
        "actions": [("retry", "重试"), ("diagnose", "故障诊断")],
    },
    {
        "code": "test_no_reply",
        "patterns": [r"收到你的消息了，但她没回"],
        "title": "收到了消息，但她没有回复",
        "cause": "常见原因是 DeepSeek 密钥不对或没余额、AstrBot 还在启动、或者主人号没加进白名单。",
        "steps": ["点「故障诊断」，按诊断结果里的提示处理。", "处理完再点「重试」。"],
        "actions": [("diagnose", "故障诊断"), ("retry", "重试")],
    },
    {
        "code": "astrbot_down",
        "patterns": [r"NEED_LOGIN", r"AstrBot 没在(运行|跑)", r"6185.{0,40}(refused|拒绝)", r"连不上 AstrBot"],
        "title": "AstrBot 没在运行",
        "cause": "AstrBot 是负责思考和回复的部分，它没在运行时她不会回任何消息，后台也打不开。",
        "steps": ["点「启动 AstrBot」，大约等半分钟到一分钟。", "启动后还是不行，点「故障诊断」。"],
        "actions": [("start_astrbot", "启动 AstrBot"), ("diagnose", "故障诊断")],
    },
    {
        "code": "napcat_install",
        "patterns": [r"NapCat 没装好", r"NapCatInstaller", r"NapCat 目录不完整"],
        "title": "NapCat 没装好",
        "cause": "NapCat 安装器要下载 QQ 内核（约 300MB）和 NapCat 本体，最常见的原因是下载中途断了，其次是被杀毒软件拦截。",
        "steps": ["点「重试」，会检查并删掉没下完的文件、重新下载。",
                  "连续失败的话换一个网络再试，或者检查杀毒软件有没有拦截（隔离区里有没有 QQ、NapCat 相关文件）。"],
        "actions": [("retry", "重试"), ("report", "生成问题报告")],
    },
    {
        "code": "path_bad",
        "patterns": [r"路径.{0,10}(中文|空格|英文)", r"UnicodeEncodeError"],
        "title": "安装位置的路径有问题",
        "cause": "AstrBot 和 NapCat 不支持带中文或空格的路径，比如「C:\\用户\\新建 文件夹」。",
        "steps": ["回到「安装位置与环境检查」，换一个全英文、没有空格的文件夹，比如 D:\\Dafeiyu。"],
        "actions": [("env", "更换安装位置")],
    },
]

DEFAULT = {
    "code": "unknown",
    "title": "这一步没有完成",
    "cause": "遇到了一个整合包还不认识的问题。",
    "steps": ["先点「重试」，很多问题重试一次就好。",
              "还不行，点「生成问题报告」，把生成的文件发给整合包作者，不用自己截图描述。"],
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
