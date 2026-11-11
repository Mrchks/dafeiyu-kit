# 大肥鱼整合包 · 协作规则（所有 AI 共用）

本文件是 GPT（Codex）、Claude 等所有参与开发的 AI 的共同规则。开工前完整读一遍；规则和实际情况不符时，先告诉作者，不要自行绕过。

## 开工三步
1. 读本文件和 `AGENTS.local.md`（作者本机的路径、账号等，不进 Git；不存在时问作者），再读 `docs/开发记录.md`（项目现状）和 `docs/交接记录.md` 最后三条（上一位做到哪）。
2. 运行 `git status`，确认工作区干净。有别人未提交的改动时先停下告诉作者，不要混进自己的提交。
3. 动手前必须先向作者说明方案，并等作者回复同意。先用大白话讲清这是什么、为什么要做、做完作者会看到什么变化，再讲改什么、影响范围、怎么验证。任务书里的「作者确认」只表示作者同意做这件事，不等于同意具体做法；只有作者在当次对话里亲口说「直接进行」才能跳过这一步。

## 收工三步
1. 自测：语法检查、`python -m unittest discover tests`、涉及界面的要实际打开界面看过。没做到的如实写明。
2. 更新记录：`CHANGELOG.md`「未发布」（写给用户看的）、`docs/开发记录.md`（写给开发者看的现状和结论）、`docs/交接记录.md` 末尾追加一条（格式见该文件）。
3. 一个任务一次提交，提交说明写清做了什么；不推送、不打标签、不升版本号，这些由作者决定。

## 项目是什么
- 「大肥鱼整合包」：把 DeepSeek 等模型接进 QQ 的整合包，给不懂技术的用户解压即用。组成：AstrBot（机器人框架）+ NapCat（QQ 协议端）+ 本仓库的桌面控制台。
- 本仓库 `C:\AI\Dafeiyu\Kit`：
  - `app/`：控制台（Python 后端 `server.py` `services.py` `manage.py` 等，前端 `app/ui/index.html`）
  - `app/configpack/`：随包分发的配置、人设和自带插件（`plugins/astrbot_plugin_dafeiyu_*`，同名 `.json` 里 `always_reinstall: true` 的会在控制台启动时自动装进 AstrBot）
  - `tests/`：离线回归测试，不联网、不调模型、不写生产数据
  - `docs/`：开发文档，不随发布包分发
- 作者自己的运行环境（不在仓库里，只读查看，改动前先备份到 `C:\AI\Dafeiyu\Backups\日期-主题\`）：
  - AstrBot 数据：`<作者用户目录>\.astrbot_launcher\instances\<实例ID>\core\data`（完整路径见 `AGENTS.local.md`）（`cmd_config.json`、`data_v4.db`、`plugins\`）
  - 控制台端口记在 `Kit\state.json` 的 `console_port`（现在 6290），AstrBot 后台 6185
- 人设文件夹 `C:\AI\Dafeiyu\人设` 只放人设草稿（.md），不放脚本、bat、结果文件。人设以 AstrBot 数据库为准，草稿改完要在控制台「人设」页导入并保存才生效。

## 作者的固定要求
- 回复作者用规范书面语，不口语化；先说结论。
- 控制台界面：标题、按钮、标签、选项名用规范用语（说明文字可以口语）；简约高级、交互有动效且流畅，不要浏览器原生样式控件；一屏放得下的页面不能出现滚动；设置类页面不要做成一条长滚动页；并排区块高度一致。界面视觉规范见 `app/ui/DESIGN.md`。
- 检测、分析类结果不要用会自动消失的右下角小提示，用居中、不自动消失的窗口。
- 版本号由作者定，不为每次小修升版本；平时改动记进 CHANGELOG「未发布」。
- 更新日志写法规范在 `CHANGELOG.md` 开头，照做。

## 技术约定和已知坑
- Windows 上运行脚本：Python 是 `Kit\runtime\python\python.exe`。`.bat` 用 CRLF 换行、正文只写 ASCII（中文文件名用 `%~dpn0.py` 这类写法）。
- `Kit\state.json` 只由控制台进程写；外部脚本要改状态，调控制台接口（`http://127.0.0.1:<console_port>/api/...`），否则会因文件占用报 WinError 5。
- 动 AstrBot 配置优先走它的后台接口（控制台 `app/astrbot_api.py` 封装了登录）；直接改 `cmd_config.json` 要先停 AstrBot。
- 提示词缓存：系统提示和工具列表要每次逐字相同；每次会变的内容（时间、情绪、检索结果）放进本轮用户消息并 `mark_as_temp()`，不要追加到系统提示末尾。
- 第三方插件（group_chat_plus、livingmemory、meme_manager 等）不直接改源码，需要修正时在 `astrbot_plugin_dafeiyu_fixes` 里包装。
- git：发布包按 `git ls-files` 打包，打包时排除 `AGENTS.md`、`CLAUDE.md`、`docs/`、`tests/`、`scripts/`。
- 仓库已公开（GitHub `Mrchks/dafeiyu-kit`）：提交里不要写进作者的 QQ 号、群号、本机用户目录、后台账号、密钥；需要时写进 `AGENTS.local.md`。

## 分工方式
- 作者会指定本次谁「实现」、谁「复核」。复核方只读检查：对照任务书的验收标准看改动（`git show` / `git diff`）、跑测试、查记录是否更新，结论写进交接记录；发现问题交回实现方或作者，不直接替对方改。
- 派给另一个 AI 的任务一律用 `docs/任务书模板.md` 的格式写，不靠聊天转述。写任务书的一方，交出去之前先用大白话向作者讲一遍要做什么、为什么，不能只给作者一句转发用的话。
- 复核发现问题时，在交接记录里写清问题和返工要求，并用大白话告诉作者；由作者转给实现方返工。
- 两个 AI 不同时改同一批文件。
