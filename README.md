# 麦麦验车（com.fsqhn.torrent-analyze）

把磁力链或种子哈希丢给 [whatslink.info](https://whatslink.info) 查内容：种子名称、类型、大小、文件数，图片模式下再拼一张截图卡片。whatslink 不可用时，自动改用 [itorrents.org](https://itorrents.org) 取 `.torrent` 元数据，出文字 + 文件清单。

移植自 HoshinoBot 的 `torrent_analyze`，按 MaiBot 插件 SDK 2.x 的组件模型重写。

## 功能

- **指令**：`验车 <哈希|磁链>`，别名 `种子分析` / `种子信息` / `种子详情`；`验车帮助` 看用法。
- **LLM 工具**：群里出现磁链时，模型可自行调用 `torrent_analyze`，由插件直接把结果发到会话，模型只回一句摘要。
- **引用触发**：引用一条含磁链的消息，只发「验车」即可。被引的是「转发卡片」时宿主只给占位文本，插件会提示改用原始聊天记录。
- **合并转发触发**：转发一条含磁链的聊天记录自动出结果。**默认关闭**，需要时在配置里打开 `auto_forward_trigger`。
- **图片卡片**：720px 白底，标题 + 哈希 + 名称/类型/大小/文件数 + 最多 3 张截图竖向拼接；高斯模糊可配；渲染失败自动降级纯文本。
- **群白名单**：白名单里的群不模糊，白名单外的群按 `blur_radius` 模糊；白名单留空表示全部模糊。
- **接口兜底**：whatslink 返回异常或查不到时，自动切换到 itorrents 按哈希取元数据，出文字 + 文件清单（没有截图）。
- **缓存**：按种子哈希缓存 7 天（可配），同一 hash 第二次查询不再请求接口。
- **限频重试**：whatslink 返回 `quota_limited` 时重试（默认 2 次、间隔 3 秒）。
- **防刷屏**：一条消息只处理第一个磁链；同一消息 10 秒内只响应一次（指令 / 工具 / 合并转发三通道共用去重）。

## 安装

1. 把整个 `com.fsqhn.torrent-analyze` 目录放进 MaiBot 的 `plugins/` 下：

   ```
   plugins/com.fsqhn.torrent-analyze/
   ├── _manifest.json
   ├── config.py
   ├── plugin.py
   ├── __init__.py
   ├── README.md
   ├── AGENTS.md
   └── tests/
   ```

2. 确保运行插件的 Python 环境里装了 `Pillow >= 10` 与 `httpx >= 0.25`：

   ```bash
   python -m pip install "Pillow>=10.0.0" "httpx>=0.25.0"
   ```

3. 在 MaiBot 插件管理里启用插件，或重启 MaiBot。日志出现「验车已加载」即成功。
4. 首次启用会生成 `data/plugins/com.fsqhn.torrent-analyze/config.toml`，可在 WebUI 里直接改；改完宿主会热更新，不必重启。

## 配置

全部在 `[plugin]` 配置节下，WebUI 可改、支持热更新。

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关，关掉后指令、工具、自动触发都不响应 |
| `image_mode` | `true` | 返回图片卡片；关闭只发文字 |
| `blur_radius` | `10` | 白名单外群聊的截图模糊半径，0 = 不模糊 |
| `blur_whitelist_groups` | `""` | 不模糊的群号，逗号或空格分隔；留空 = 全部模糊 |
| `max_screenshots` | `3` | 卡片里最多拼几张截图，0 = 不拼 |
| `cache_ttl_hours` | `168` | 结果缓存小时数，0 = 不缓存 |
| `request_timeout` | `15` | 单次 HTTP 超时（秒） |
| `retry_times` | `2` | whatslink `quota_limited` 重试次数 |
| `proxy` | `""` | HTTP 代理，例如 `http://127.0.0.1:7890`；留空直连 |
| `fallback_itorrents` | `true` | whatslink 不可用时用 itorrents 兜底（无截图） |
| `file_list_limit` | `15` | 兜底文字里最多列多少个文件，0 = 不列 |
| `auto_forward_trigger` | `false` | 合并转发里含磁链时自动验车，**默认关** |

配置模板：

```toml
[plugin]
config_version = "1.0.0"
enabled = true
image_mode = true
blur_radius = 10
blur_whitelist_groups = ""
max_screenshots = 3
cache_ttl_hours = 168
request_timeout = 15
retry_times = 2
proxy = ""
fallback_itorrents = true
file_list_limit = 15
auto_forward_trigger = false
```

## 用法

### 直接查

```
验车 32181995C9D274FCFBE0A5E427F047210E82A53D
验车 magnet:?xt=urn:btih:32181995c9d274fcfbe0a5e427f047210e82a53d&dn=demo
种子分析 32181995C9D274FCFBE0A5E427F047210E82A53D
种子信息 32181995C9D274FCFBE0A5E427F047210E82A53D
种子详情 32181995C9D274FCFBE0A5E427F047210E82A53D
```

接受的输入：40 位 hex、32 位 base32、完整 `magnet:?xt=urn:btih:...`（`&dn=` 等参数原样保留）。

### 引用消息

引用一条带磁链的消息，然后只发：

```
验车
```

宿主会把被引内容文本化成 `[回复了xx的消息: magnet:...]`，插件从中提取第一个磁链。

被引的是 QQ「转发卡片」时，宿主只能拿到 `[转发消息]` 这类占位文本，里面没有磁链，插件会给出提示。要查转发里的内容，直接转发原始聊天记录，或把磁链单独发出来。

### 合并转发

`auto_forward_trigger = true` 时，直接转发一段含磁链的聊天记录即可，不用附加文字。一个转发只取第一个磁链，不会刷屏。默认关闭，避免群里正常聊天转发也被插件接管。

### 接口兜底

whatslink 返回非 200、超时或 `type = UNKNOWN` 时：

1. 用种子哈希请求 `https://itorrents.org/torrent/<HASH>.torrent`；
2. 本地解析 bencode，得到名称、总大小、文件数、文件清单；
3. 发文字卡片，标注「由 itorrents 兜底解析」，不拼截图。

itorrents 只覆盖被它缓存过的种子，没有命中时会回到原来的错误提示。

### LLM 调用

群里有人贴磁链并问「这是啥」，模型会调用 `torrent_analyze` 工具；插件自己发结果，模型只回一句「已发送种子信息」。

## 常见问题

**Q：提示「查询服务额度已用完（quota_limited）」？**
whatslink.info 是免费公共服务，按 IP 限频。插件已自动重试 2 次、间隔 3 秒；仍旧失败就等一会儿再查，或配置 `proxy` 换出口。

**Q：提示「查询服务返回异常状态（HTTP 500）」？**
whatslink 对个别种子会稳定 500。只要 `fallback_itorrents` 开着，插件会自动走 itorrents；两边都没结果才会报错。

**Q：提示「没查到这个种子的内容」？**
接口返回 `type = UNKNOWN`，且 itorrents 也没有缓存：新种子、死种或服务没收录。换一个磁链试试。

**Q：只收到文字，没有截图卡片？**
依次排查：`image_mode` 是否为 `true`；运行环境是否能 `import PIL`（日志会有「未找到 Pillow」warning）；截图下载失败或渲染异常时插件会自动降级纯文本并在日志留下 warning。兜底结果本身就不带截图。

**Q：图片太露骨？**
`blur_radius` 默认 10；想更糊就调大（常用 10～20）。想给某个群看原图，把群号填进 `blur_whitelist_groups`。

**Q：第二次查同一个 hash 还请求接口吗？**
不会，命中缓存直接返回（默认 7 天）。想看新数据就把 `cache_ttl_hours` 设小或删掉数据目录下的 `cache.json`。

**Q：为什么不用 aria2 / DHT 自己抓种子元数据？**
实测这台服务器到公共 DHT 路由只有一条能通，抓元数据 45 秒仍无结果；itorrents 是现成缓存，按 hash 直接取，几百毫秒出结果，更适合做兜底。

## 数据与网络

- 访问 `https://whatslink.info/api/v1/link`（查询）与 `https://whatslink.info/image/`（截图，白名单前缀，单张 ≤ 8MB）。
- 兜底访问 `https://itorrents.org/torrent/<HASH>.torrent`（≤ 4MB），本地解析，不建立任何 P2P 连接。
- UA 使用诚实标识 `MaiBot-TorrentAnalyze/1.0`，不伪装浏览器。
- 缓存写入 `data/plugins/com.fsqhn.torrent-analyze/cache.json`，仅存查询结果，不含用户信息。

## 开发与自测

```bash
python -m py_compile plugin.py config.py __init__.py tests/test_parse.py
python tests/test_parse.py          # 纯函数单测，不需要 SDK 运行时
```

自测覆盖：磁链/哈希解析、引用与合并转发文本提取、群白名单、itorrents bencode 解析与文字格式化、去重键。

## 免责声明

查询结果完全来自第三方免费服务 whatslink.info 与 itorrents.org，本插件不保证准确性、完整性与可用性，也不对截图内容负责。请遵守所在地法律法规与平台规则，不要用本插件传播违法或侵权内容。接口失效、限频、封禁均可能导致功能不可用，作者不承担由此产生的任何责任。

## 许可

MIT
