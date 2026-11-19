# AGENTS.md

## 项目概述

MaiBot 第三方插件「麦麦验车」，插件 id `com.fsqhn.torrent-analyze`，Python 3.12 + MaiBot Plugin SDK 2.21.x。

功能：把磁力链 / 种子哈希换成内容信息（名称、类型、大小、文件数、截图卡片）。主数据源是 whatslink.info，兜底是 itorrents.org 的 `.torrent` 缓存。

面向生产环境：已部署在 `124.221.180.186` 的 `C:\bot\0.7.0\MaiBot\plugins\com.fsqhn.torrent-analyze`，改动要能直接上线，不写一次性脚本。

## 构建与开发命令

在插件根目录执行：

```bash
python -m py_compile plugin.py config.py __init__.py tests/test_parse.py
python tests/test_parse.py
```

本机没有 `pytest`、没有 `maibot_sdk`。`tests/test_parse.py` 会自己注入 SDK 桩，新增纯函数逻辑时优先补进这个文件，而不是引入新依赖。

生产部署（本机 Git Bash）：

```bash
scp -i "E:/丘浩南/ai/wechat/.pai/keys/maibot_win" plugin.py config.py \
  Administrator@124.221.180.186:C:/bot/0.7.0/MaiBot/plugins/com.fsqhn.torrent-analyze/
```

服务器 `plugins/` 目录有 watchfiles 监听，覆盖文件后宿主会自动重载插件，**不要重启 MaiBot 进程**。

## 架构概览

单文件实现，`plugin.py` 按顺序分四段：

1. **纯函数区**（文件顶部）：磁链/哈希正则、`normalize_magnet`、`extract_first_magnet`、`collect_texts`、`looks_like_forward`、`parse_group_list`、`parse_torrent_meta`（bencode）、`format_*`。这一层不依赖 SDK，测试覆盖的就是它。
2. **渲染区**：Pillow 画 720px 卡片，字体从系统目录探测；任何异常都上抛，由调用方降级纯文本。
3. **插件类 `TorrentAnalyzePlugin`**：配置读取、HTTP 客户端、缓存、截图下载、`_fetch_info`（whatslink → itorrents 降级）、`_deliver`（渲染或文字 + 发送）。
4. **组件区**：`@Command`、`@Tool`、`@HookHandler`。

数据流：消息 → 组件处理函数 → `normalize_magnet` / `extract_first_magnet` → `_claim` 去重 → `_fetch_info` → `_deliver` → `ctx.send.image/text`。

## 关键目录与数据流

- `plugin.py`：全部实现，约 1600 行。改动集中在这里。
- `config.py`：pydantic 配置模型，字段默认值就是生产默认值。
- `_manifest.json`：`id`、`version`、`entry`、`dependencies`、`author.url`/`urls`/`i18n` 均为必填，缺一个宿主直接拒绝加载。
- `tests/test_parse.py`：无 pytest 的极简执行器，`test_*` 函数会被自动发现。
- 运行时数据：`data/plugins/com.fsqhn.torrent-analyze/cache.json`（查询缓存，可删，删了会重建）。
- 生产配置：`plugins/com.fsqhn.torrent-analyze/config.toml`，与 `config.py` 的默认值不是同一份，改默认值时要同步改服务器那份。

## 开发约定

### 组件

- `@Command` 处理函数必须返回三元组 `(success: bool, response: str, intercept: bool)`，返回二元组会解包报错。
- **命令与 Tool 不能同名**：宿主遇到同名组件会打印「已存在，覆盖」，后注册的把先注册的顶掉。命令组件名固定 `torrent_analyze_cmd`，Tool 固定 `torrent_analyze`。
- 自动触发只能用 `@HookHandler("chat.receive.after_process", mode=HookMode.OBSERVE)`。`@EventHandler` 在本宿主 1.4.0 里是死代码，`dispatch_event` 没有业务调用点。
- Hook 载荷形如 `{"message": <serialize_session_message 的 dict>}`，字段有 `message_id`、`session_id`、`processed_plain_text`、`raw_message`、`message_info`。群号在 `message.message_info.group_info.group_id`。
- 命令通路的去重键可能在 `message.message_base_info.message_id`，`_message_id` 必须同时认 `message_base_info` 和 `message_id`。

### 网络与解析

- 只访问两个域名：`whatslink.info`（查询 + 截图）和 `itorrents.org`（兜底 `.torrent`）。新增源先确认生产服务器可达。
- 截图下载必须逐跳校验 `SCREENSHOT_PREFIX` 白名单，并用流式读取限制 `SCREENSHOT_MAX_BYTES`；不要把整个响应先读进内存。
- 查询失败不要抛异常给宿主，统一返回 `(None, 中文错误提示)`，由 `_deliver` 发文本。
- 兜底结果 `source == "itorrents"`，不带截图，走文字分支；`format_info_text` 会附文件清单并标注来源。
- 一个消息只处理第一个磁链；同一 `message_id` 10 秒内只响应一次（`_claim`），失败路径不要额外占位。

### 文本与配置

- 所有面向用户的文本用中文，日志也用中文；日志前缀统一 `PLUGIN_NAME`。
- 新增配置字段要同时做三件事：`config.py` 加 `Field`、服务器 `config.toml` 加同名键、`README.md` 的配置表加一行。
- 布尔开关的默认值要保守：自动接管型功能（如合并转发自动验车）默认关。
- 模糊半径语义是「白名单外的群」，白名单命中返回 0；白名单留空表示全部模糊。

## 测试与验证要求

- 每次改 `plugin.py` 后至少跑：`py_compile` + `python tests/test_parse.py`，两条都过再上传。
- 新增或修改纯函数（解析、格式化、白名单、bencode）必须补 `tests/test_parse.py` 用例。
- 改动组件签名、Hook 名、组件名后，必须看生产日志确认：出现「验车已加载」且没有「组件 … 已存在，覆盖」。
- 涉及网络的行为（whatslink、itorrents、截图）要拿到真实日志或真实返回再下结论，不接受「应该可以」。

## 提交与发布规则

- 生产文件改动前先在同目录备份成 `*.bak_<用途>`（如 `plugin.py.bak_hook`），改完回读校验。
- 上传只覆盖 `plugin.py` / `config.py` / `README.md` / `AGENTS.md`，不要动 `plugins/` 下其他插件、`src/`、`config/`。
- 不重启 MaiBot 进程；靠 watchfiles 重载。重载后等 10～20 秒再看日志。
- 修改 `config.toml` 用 UTF-8 无 BOM 写入，写完 `Get-Content` 回读确认编码与字段。
- 发布前确认插件日志没有新的 warning / traceback，再交给用户在群里实测。

## 项目特有流程

- 生产服务器是 Windows，SSH 远端是 cmd，执行命令要带 `chcp 65001 >nul`，路径用 `MSYS_NO_PATHCONV=1`。私钥路径 `E:/丘浩南/ai/wechat/.pai/keys/maibot_win`，不要写进任何文件。
- 服务器上有两套解释器：`.venv` 与 uv python。插件 Runner 用后者启动但依赖来自 `.venv`，装包前先确认装到哪套。
- 服务器没有 `data/playwright-browsers`，不要用 `ctx.render.html2png`，图片一律走 Pillow。
- 宿主对 QQ「转发卡片」只给 `[转发消息]` 占位，拿不到内部内容；「标准合并转发」会展开成 `【合并转发消息: ...】`。引用转发卡片时插件只能提示，不要试图绕过。
- 生产库里消息文本化结果存在 `data/MaiBot.db` 的 `mai_messages.processed_plain_text`，排查引用/转发问题时可以直接查这张表。
