"""MaiBot「验车」插件：查询磁力链 / 种子哈希的内容信息。

移植自 HoshinoBot 的 torrent_analyze，按 MaiBot SDK 2.x 组件模型重做：

* ``@Command``：``验车 <hash|磁链>``，别名 ``种子分析`` / ``种子信息`` / ``种子详情``。
* ``@Tool``：LLM 在群聊里看到磁链时可自行调用，由插件直接发结果。
* ``@HookHandler("chat.receive.after_process")``：合并转发消息里出现磁链时自动验车。
* 结果默认渲染成「文字 + 截图」图片卡片，失败自动降级纯文本。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from maibot_sdk import Command, MaiBotPlugin, Tool

try:  # 有的 SDK 版本没有在顶层导出 HookHandler
    from maibot_sdk import HookHandler
except ImportError:  # pragma: no cover - 取决于宿主 SDK 版本
    from maibot_sdk.components import HookHandler

from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .config import TorrentAnalyzeConfig

try:  # Pillow 缺失时整个插件仍要能加载，只是不出图
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    _PIL_READY = True
except Exception:  # noqa: BLE001 - 任何导入失败都按「不可用」处理
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    _PIL_READY = False


PLUGIN_ID = "com.fsqhn.torrent-analyze"
PLUGIN_NAME = "麦麦验车"
USER_AGENT = "MaiBot-TorrentAnalyze/1.0"
API_URL = "https://whatslink.info/api/v1/link"
SCREENSHOT_PREFIX = "https://whatslink.info/image/"
FALLBACK_TORRENT_URL = "https://itorrents.org/torrent/"
TORRENT_MAX_BYTES = 4 * 1024 * 1024

COMMAND_PATTERN = r"(?:^|\s)[/!！]?(?:验车|种子分析|种子信息|种子详情)\s*(?P<query>.*)$"
HELP_PATTERN = r"(?:^|\s)[/!！]?验车帮助$"

DEDUPE_WINDOW = 10.0  # 秒：同一 platform_message_id 只处理一次
TOTAL_BUDGET = 15.0  # 秒：单次查询（含限频重试）的总时长上限
RETRY_INTERVAL = 3.0  # 秒：quota_limited 重试间隔
CACHE_FILE_NAME = "cache.json"

SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024
SCREENSHOT_TIMEOUT = 15.0
CARD_WIDTH = 720
CARD_PADDING = 28
SCREENSHOT_GAP = 12

HELP_WORDS = {"帮助", "help", "?", "？", "用法"}

HELP_TEXT = (
    "🧲 麦麦验车用法\n"
    "1. 直接查哈希：验车 32181995C9D274FCFBE0A5E427F047210E82A53D\n"
    "2. 贴完整磁链：验车 magnet:?xt=urn:btih:xxxxxxxx\n"
    "3. 引用一条含磁链的消息，只发「验车」也能查（转发卡片除外，拿不到内容）\n"
    "4. 转发一条含磁链的聊天记录会自动出结果（需在配置里打开「合并转发自动验车」）\n"
    "5. 别名：种子分析 / 种子信息 / 种子详情\n"
    "\n"
    "结果包含种子哈希、类型、名称、大小与文件数；图片模式下附截图。"
)

USAGE_HINT = (
    "没认出磁链或种子哈希。\n"
    "用法：验车 <40 位种子哈希> 或 验车 magnet:?xt=urn:btih:xxxx\n"
    "也可以引用 / 转发一条带磁链的消息再发「验车」。"
)

FORWARD_PLACEHOLDER_MARKS = ("[转发消息]", "[forward]", "[聊天记录]", "[合并转发]")
FORWARD_PLACEHOLDER_HINT = (
    "这条引用只有转发占位，拿不到里面的磁链。\n"
    "可以引用原始聊天记录（不是转发卡片），或者直接把磁链发出来。"
)

# 磁链本体：只认 btih，参数部分不跨空格与括号，避免把整句中文吃进来
_MAGNET_RE = re.compile(
    r"magnet:\?xt=urn:btih:(?P<hash>[A-Za-z0-9]+)"
    r"(?P<extra>(?:&[^\s&\]\}>】）>,，。；;、\"'“”‘’]*)*)",
    re.IGNORECASE,
)
_HASH40_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{40}(?![0-9A-Za-z])")
_HASH32_RE = re.compile(r"(?<![0-9A-Za-z])[A-Za-z2-7]{32}(?![0-9A-Za-z])")
_BASE32_FULL_RE = re.compile(r"[A-Za-z0-9]{32}")

_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_LOGGER = logging.getLogger(PLUGIN_ID)
_FONT_FILES = ("msyh.ttc", "simhei.ttf", "simsun.ttc", "msjh.ttc", "DejaVuSans.ttf")
_FONT_FILES_BOLD = ("msyhbd.ttc", "simhei.ttf", "simsun.ttc", "msjhbd.ttc", "DejaVuSans-Bold.ttf")


# ---------------------------------------------------------------------------
# 纯函数：解析与格式化（不依赖 SDK，可离线单测）
# ---------------------------------------------------------------------------


def build_magnet(raw_hash: str) -> str:
    """把裸哈希包装成磁链。

    Args:
        raw_hash: 40 位 hex 或 32 位 base32 种子哈希。

    Returns:
        str: 磁链字符串；空输入返回空串。
    """

    token = str(raw_hash or "").strip()
    if not token:
        return ""
    return f"magnet:?xt=urn:btih:{token}"


def normalize_magnet(value: str) -> str:
    """把用户输入规范成可查询的磁链。

    接受完整 magnet（含 ``&dn=`` 等参数，原样保留）、40 位 hex、32 位 base32；
    其余输入一律判为无效。

    Args:
        value: 用户给的原始参数。

    Returns:
        str: 规范化后的磁链；无法识别时返回空串。
    """

    text = str(value or "").strip()
    if not text:
        return ""
    found = _MAGNET_RE.search(text)
    if found:
        return f"magnet:?xt=urn:btih:{found.group('hash')}{found.group('extra') or ''}"
    token = text.strip("<>《》\"'` \t\r\n")
    if re.fullmatch(r"[0-9A-Fa-f]{40}", token) or _BASE32_FULL_RE.fullmatch(token):
        return build_magnet(token)
    return ""


def extract_first_magnet(text: str) -> str:
    """从一段长文本里取出第一个磁链或种子哈希。

    优先级：完整 magnet > 40 位 hex > 32 位 base32。引用消息与合并转发被宿主
    文本化后仍然适用，因为磁链本体是原样保留的。

    Args:
        text: 待扫描的文本。

    Returns:
        str: 磁链；没有则返回空串。
    """

    if not isinstance(text, str) or not text.strip():
        return ""
    found = _MAGNET_RE.search(text)
    if found:
        return f"magnet:?xt=urn:btih:{found.group('hash')}{found.group('extra') or ''}"
    found = _HASH40_RE.search(text)
    if found:
        return build_magnet(found.group(0))
    found = _HASH32_RE.search(text)
    if found:
        return build_magnet(found.group(0))
    return ""


def magnet_hash(magnet: str) -> str:
    """提取磁链里的种子哈希，用于缓存键。

    Args:
        magnet: 磁链或裸哈希。

    Returns:
        str: 大写哈希；取不到返回空串。
    """

    text = str(magnet or "").strip()
    found = _MAGNET_RE.search(text)
    if found:
        return found.group("hash").upper()
    token = text.strip("<>《》\"'` \t\r\n")
    if re.fullmatch(r"[0-9A-Fa-f]{40}", token) or _BASE32_FULL_RE.fullmatch(token):
        return token.upper()
    return ""


def format_size(size: Any) -> str:
    """把字节数格式化成人类可读的大小。

    Args:
        size: 字节数，允许是字符串或空值。

    Returns:
        str: 例如 ``1.5 GB``；无法判断时返回「未知」。
    """

    try:
        value = float(size)
    except (TypeError, ValueError):
        return "未知"
    if value <= 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while value >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[index]}"


def format_type(info: dict[str, Any]) -> str:
    """拼接 ``type-file_type`` 形式的内容类型。

    Args:
        info: 归一化后的种子信息。

    Returns:
        str: 类型文本；都没有时返回「未知」。
    """

    kind = str(info.get("type") or "").strip()
    sub = str(info.get("file_type") or "").strip()
    if kind and sub:
        return f"{kind}-{sub}"
    return kind or sub or "未知"


def bdecode(data: bytes, index: int = 0) -> tuple[Any, int]:
    """解析 bencode 数据（.torrent 文件格式）。

    Args:
        data: 原始字节。
        index: 起始偏移。

    Returns:
        tuple[Any, int]: 解析结果与下一个偏移。

    Raises:
        ValueError: 数据不合法时抛出，由调用方降级。
    """

    if index >= len(data):
        raise ValueError("bencode 数据不完整")
    head = data[index : index + 1]
    if head == b"i":
        end = data.find(b"e", index)
        if end < 0:
            raise ValueError("整数未闭合")
        return int(data[index + 1 : end]), end + 1
    if head == b"l":
        index += 1
        items: list[Any] = []
        while data[index : index + 1] != b"e":
            if index >= len(data):
                raise ValueError("列表未闭合")
            item, index = bdecode(data, index)
            items.append(item)
        return items, index + 1
    if head == b"d":
        index += 1
        mapping: dict[bytes, Any] = {}
        while data[index : index + 1] != b"e":
            if index >= len(data):
                raise ValueError("字典未闭合")
            key, index = bdecode(data, index)
            value, index = bdecode(data, index)
            mapping[key] = value
        return mapping, index + 1
    sep = data.find(b":", index)
    if sep < 0:
        raise ValueError("字符串长度缺失")
    try:
        length = int(data[index:sep])
    except ValueError as exc:
        raise ValueError("字符串长度非法") from exc
    start = sep + 1
    end = start + length
    if end > len(data):
        raise ValueError("字符串越界")
    return data[start:end], end


def parse_torrent_meta(blob: bytes, fallback_hash: str) -> dict[str, Any] | None:
    """把 .torrent 内容解析成插件内部结构（无截图）。

    Args:
        blob: .torrent 原始字节。
        fallback_hash: 解析不出哈希时的兜底值。

    Returns:
        dict | None: 归一化信息；解析失败返回 None。
    """

    try:
        root, _ = bdecode(blob)
    except (ValueError, IndexError):
        return None
    if not isinstance(root, dict):
        return None
    info = root.get(b"info")
    if not isinstance(info, dict):
        return None

    raw_name = info.get(b"name")
    name = raw_name.decode("utf-8", "replace").strip() if isinstance(raw_name, bytes) else str(raw_name or "").strip()
    if not name:
        name = "未知"

    files: list[dict[str, Any]] = []
    raw_files = info.get(b"files")
    if isinstance(raw_files, list):
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            try:
                length = int(item.get(b"length") or 0)
            except (TypeError, ValueError):
                length = 0
            segments: list[str] = []
            parts = item.get(b"path")
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, bytes):
                        segments.append(part.decode("utf-8", "replace"))
                    elif part is not None:
                        segments.append(str(part))
            files.append({"path": "/".join(segments) or "未命名", "length": length})
        total = sum(int(item["length"]) for item in files)
        kind, file_type, count = "FOLDER", "folder", len(files)
    else:
        try:
            total = int(info.get(b"length") or 0)
        except (TypeError, ValueError):
            total = 0
        suffix = Path(name).suffix.lstrip(".").lower()
        kind, file_type, count = "FILE", suffix or "file", 1

    return {
        "hash": str(fallback_hash or "").upper(),
        "type": kind,
        "file_type": file_type,
        "name": name,
        "size": total,
        "count": count,
        "screenshots": [],
        "files": files,
        "source": "itorrents",
    }


def format_info_text(info: dict[str, Any], file_limit: int = 15) -> str:
    """把种子信息格式化成纯文本（图片渲染失败或兜底时的输出）。

    Args:
        info: 归一化后的种子信息。
        file_limit: 最多列出多少个文件，0 表示不列清单。

    Returns:
        str: 多行中文文本。
    """

    name = str(info.get("name") or "未知")
    count = info.get("count")
    try:
        count_text = f"{int(count)} 个" if count is not None else "未知"
    except (TypeError, ValueError):
        count_text = "未知"
    lines = [
        "🧲 种子信息",
        f"哈希：{info.get('hash') or '未知'}",
        f"类型：{format_type(info)}",
        f"名称：{name}",
        f"大小：{format_size(info.get('size'))}",
        f"文件数：{count_text}",
    ]
    files = info.get("files")
    try:
        limit = max(0, int(file_limit))
    except (TypeError, ValueError):
        limit = 0
    if isinstance(files, list) and files and limit > 0:
        lines.append("文件清单：")
        for index, item in enumerate(files[:limit], start=1):
            if not isinstance(item, dict):
                continue
            lines.append(f"{index}. {item.get('path') or '未命名'}（{format_size(item.get('length'))}）")
        if len(files) > limit:
            lines.append(f"……另有 {len(files) - limit} 个文件未列出")
    if str(info.get("source") or "") == "itorrents":
        lines.append("（whatslink 暂不可用，本条由 itorrents 兜底解析，没有截图）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 文本收集：命令参数为空时，从整条消息（含引用 / 合并转发文本化结果）里找磁链
# ---------------------------------------------------------------------------


def _iter_strings(value: Any, depth: int = 0) -> list[str]:
    """递归收集容器里的所有非空字符串。

    Args:
        value: 任意对象。
        depth: 当前递归深度，超过 4 层即停止。

    Returns:
        list[str]: 收集到的字符串列表。
    """

    if depth > 4:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    results: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            results.extend(_iter_strings(item, depth + 1))
        return results
    if isinstance(value, (list, tuple)):
        for item in value:
            results.extend(_iter_strings(item, depth + 1))
        return results
    return results


def _as_mapping(value: Any) -> Any:
    """把 SDK 模型或字典统一成可 ``.get`` 的对象。

    Args:
        value: 消息对象、pydantic 模型或字典。

    Returns:
        Any: 字典或空字典。
    """

    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # noqa: BLE001 - 序列化失败按无内容处理
            return {}
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, (list, tuple)):
        return {"items": list(value)}
    return {}


def parse_group_list(raw: Any) -> set[str]:
    """把配置里的群号串解析成集合。

    Args:
        raw: 配置值，支持逗号/空格/顿号分隔的字符串，或字符串列表。

    Returns:
        set[str]: 群号集合。
    """

    if isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = re.split(r"[,\s，、;；]+", str(raw or ""))
    return {item.strip() for item in items if item and item.strip()}


def collect_texts(kwargs: dict[str, Any]) -> list[str]:
    """按优先级收集一条消息的所有文本形态。

    宿主会把引用与合并转发文本化进 ``processed_plain_text``，所以直接扫文本即可。

    Args:
        kwargs: 组件处理函数收到的关键字参数。

    Returns:
        list[str]: 去重后的文本列表。
    """

    texts: list[str] = []
    for key in ("processed_plain_text", "plain_text", "text"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    for key in ("message", "raw_message", "messages"):
        value = kwargs.get(key)
        if value is None:
            continue
        texts.extend(_iter_strings(_as_mapping(value)))
    return list(dict.fromkeys(texts))


def collect_text(kwargs: dict[str, Any]) -> str:
    """把消息的所有文本形态拼成一段，供正则提取。

    Args:
        kwargs: 组件处理函数收到的关键字参数。

    Returns:
        str: 拼接后的文本。
    """

    return "\n".join(collect_texts(kwargs))


def _has_forward_segment(value: Any, depth: int = 0) -> bool:
    """递归查找消息段里的 forward 类型。

    Args:
        value: 待检查的任意结构（dict / list / 标量）。
        depth: 当前递归深度，避免畸形结构无限下探。

    Returns:
        bool: 含 ``type == "forward"`` 的段返回 True。
    """

    if depth > 6:
        return False
    if isinstance(value, dict):
        if str(value.get("type") or "").strip().lower() == "forward":
            return True
        return any(_has_forward_segment(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_has_forward_segment(item, depth + 1) for item in value)
    return False


def looks_like_forward(kwargs: dict[str, Any], texts: list[str] | None = None) -> bool:
    """判断这条消息是不是合并转发。

    Args:
        kwargs: 组件处理函数收到的关键字参数。
        texts: 已收集的文本，缺省时内部自取。

    Returns:
        bool: 命中合并转发段或文本化标记时返回 True。
    """

    for key in ("message", "raw_message"):
        data = _as_mapping(kwargs.get(key))
        if data is None:
            continue
        if _has_forward_segment(data):
            return True
    if texts is None:
        texts = collect_texts(kwargs)
    return any("合并转发消息" in text for text in texts)


# ---------------------------------------------------------------------------
# 图片渲染（Pillow；任何异常都上抛由调用方降级）
# ---------------------------------------------------------------------------


def _font_dirs() -> list[Path]:
    """按环境探测可能的中文字体目录，避免写死绝对路径。

    Returns:
        list[Path]: 候选目录列表。
    """

    dirs: list[Path] = []
    for env_name in ("WINDIR", "SystemRoot"):
        value = os.environ.get(env_name)
        if value:
            candidate = Path(value) / "Fonts"
            if candidate not in dirs:
                dirs.append(candidate)
    dirs.append(Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts")
    dirs.append(Path("/usr/share/fonts"))
    dirs.append(Path("/usr/local/share/fonts"))
    dirs.append(Path("/System/Library/Fonts"))
    return dirs


def load_font(size: int, *, bold: bool = False) -> Any:
    """加载中文字体，逐级降级，绝不抛错。

    Args:
        size: 字号。
        bold: 是否优先使用粗体。

    Returns:
        Any: Pillow 字体对象；Pillow 不可用时返回 None。
    """

    if not _PIL_READY:
        return None
    key = (int(size), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    names = _FONT_FILES_BOLD if bold else _FONT_FILES
    for folder in _font_dirs():
        for name in names:
            path = folder / name
            try:
                if path.is_file():
                    font = ImageFont.truetype(str(path), int(size))
                    _FONT_CACHE[key] = font
                    return font
            except Exception:  # noqa: BLE001 - 单个候选字体失败就试下一个
                continue
    font: Any = None
    try:
        font = ImageFont.load_default(size=int(size))
    except Exception:  # noqa: BLE001 - 老版本 Pillow 不接受 size 参数
        try:
            font = ImageFont.load_default()
        except Exception:  # noqa: BLE001 - 连默认字体都没有就只能放弃
            font = None
    _FONT_CACHE[key] = font
    return font


def _text_width(font: Any, text: str) -> float:
    """测量文本宽度，失败时按字符数估算。

    Args:
        font: Pillow 字体对象。
        text: 待测文本。

    Returns:
        float: 像素宽度。
    """

    try:
        return float(font.getlength(text))
    except Exception:  # noqa: BLE001 - 位图字体可能没有 getlength
        return float(len(text) * 14)


def _text_height(font: Any, text: str = "汉字Ag") -> int:
    """测量单行文本高度。

    Args:
        font: Pillow 字体对象。
        text: 采样文本。

    Returns:
        int: 像素高度。
    """

    try:
        box = font.getbbox(text)
        return max(1, int(box[3] - box[1]))
    except Exception:  # noqa: BLE001 - 兜底给一个够用的行高
        return 18


def wrap_text(text: str, font: Any, max_width: float, *, max_lines: int = 6) -> list[str]:
    """按像素宽度逐字换行。

    Args:
        text: 原始文本。
        font: Pillow 字体对象。
        max_width: 单行最大宽度。
        max_lines: 最多保留行数，超出直接截断。

    Returns:
        list[str]: 换行后的文本行。
    """

    cleaned = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return [""]
    lines: list[str] = []
    current = ""
    for char in cleaned:
        candidate = current + char
        if current and _text_width(font, candidate) > max_width:
            lines.append(current)
            current = char
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    return lines or [""]


def render_card(info: dict[str, Any], screenshots: list[bytes], blur_radius: int = 0) -> bytes:
    """把种子信息与截图拼成一张白底卡片。

    Args:
        info: 归一化后的种子信息。
        screenshots: 已下载的截图字节列表。
        blur_radius: 高斯模糊半径，0 表示不模糊。

    Returns:
        bytes: JPEG 图片字节。

    Raises:
        RuntimeError: Pillow 不可用时抛出，由调用方降级为纯文本。
    """

    if not _PIL_READY:
        raise RuntimeError("Pillow 不可用")

    pad = CARD_PADDING
    content_width = CARD_WIDTH - pad * 2
    title_font = load_font(28, bold=True)
    hash_font = load_font(15)
    label_font = load_font(19, bold=True)
    value_font = load_font(19)

    prepared: list[Any] = []
    for data in screenshots:
        try:
            with Image.open(io.BytesIO(data)) as raw:
                frame = raw.convert("RGB")
                if int(blur_radius) > 0:
                    frame = frame.filter(ImageFilter.GaussianBlur(float(blur_radius)))
                ratio = content_width / float(max(1, frame.width))
                target_height = max(1, int(frame.height * ratio))
                prepared.append(frame.resize((content_width, target_height), Image.LANCZOS))
        except Exception:  # noqa: BLE001 - 单张坏图不影响整张卡片
            continue

    label_width = 92
    name_lines = wrap_text(str(info.get("name") or "未知"), value_font, content_width - label_width)
    hash_lines = wrap_text(f"哈希  {info.get('hash') or '未知'}", hash_font, content_width)
    row_height = _text_height(value_font) + 10
    title_height = _text_height(title_font, "验车结果") + 8
    hash_height = (_text_height(hash_font) + 2) * len(hash_lines)
    entries: list[tuple[str, list[str]]] = [
        ("名称", name_lines),
        ("类型", [format_type(info)]),
        ("大小", [format_size(info.get("size"))]),
        ("文件数", [f"{int(info.get('count') or 0)} 个"]),
    ]
    entries_height = sum(row_height * len(lines) for _, lines in entries)
    shots_height = (
        sum(image.height for image in prepared) + SCREENSHOT_GAP * len(prepared)
        if prepared
        else 0
    )

    total_height = pad + title_height + hash_height + 28 + entries_height
    if prepared:
        total_height += SCREENSHOT_GAP + shots_height
    total_height += pad

    canvas = Image.new("RGB", (CARD_WIDTH, max(total_height, 140)), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    y = pad
    draw.text((pad, y), "验车结果", font=title_font, fill=(24, 24, 28))
    y += title_height
    for line in hash_lines:
        draw.text((pad, y), line, font=hash_font, fill=(120, 120, 128))
        y += _text_height(hash_font) + 2
    y += 10
    draw.line([(pad, y), (CARD_WIDTH - pad, y)], fill=(228, 228, 234), width=1)
    y += 14

    for label, lines in entries:
        draw.text((pad, y), label, font=label_font, fill=(96, 96, 104))
        for index, line in enumerate(lines):
            draw.text((pad + label_width, y + index * row_height), line, font=value_font, fill=(28, 28, 32))
        y += row_height * len(lines)

    if prepared:
        y += 4
        draw.line([(pad, y), (CARD_WIDTH - pad, y)], fill=(228, 228, 234), width=1)
        y += SCREENSHOT_GAP
        for image in prepared:
            canvas.paste(image, (pad, y))
            y += image.height + SCREENSHOT_GAP

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class TorrentAnalyzePlugin(MaiBotPlugin):
    """验车插件。"""

    config_model = TorrentAnalyzeConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, Any] | None = None
        self._cache_lock = asyncio.Lock()
        self._recent: dict[str, float] = {}

    # -- 基础工具 ---------------------------------------------------------

    def _section(self) -> Any:
        """取 ``[plugin]`` 配置节，取不到时返回 None。

        Returns:
            Any: 配置模型实例或 None。
        """

        config = getattr(self, "config", None)
        return getattr(config, "plugin", None)

    def _log(self) -> Any:
        """取宿主 logger，取不到时退回标准库 logging。

        Returns:
            Any: 具备 info/warning 方法的日志对象。
        """

        logger = getattr(self.ctx, "logger", None)
        if logger is not None:
            return logger
        return _LOGGER

    def _stream_id(self, kwargs: dict[str, Any], fallback: str = "") -> str:
        """解析目标会话 ID。

        Args:
            kwargs: 组件处理函数收到的关键字参数。
            fallback: 宿主直接注入的 stream_id。

        Returns:
            str: 会话 ID；取不到返回空串。
        """

        for key in ("stream_id", "session_id", "chat_id"):
            value = kwargs.get(key)
            if value:
                return str(value)
        data = _as_mapping(kwargs.get("message"))
        if isinstance(data, dict):
            for key in ("session_id", "stream_id", "chat_id"):
                if data.get(key):
                    return str(data[key])
            info = data.get("message_info")
            if isinstance(info, dict):
                for group_key in ("group_info", "session_info"):
                    group = info.get(group_key)
                    if isinstance(group, dict):
                        for key in ("session_id", "stream_id", "chat_id"):
                            if group.get(key):
                                return str(group[key])
        return str(fallback or "")

    def _message_id(self, kwargs: dict[str, Any]) -> str:
        """解析消息 ID，用于 10 秒内去重。

        Args:
            kwargs: 组件处理函数收到的关键字参数。

        Returns:
            str: 消息 ID；取不到返回空串。
        """

        value = kwargs.get("message_id") or kwargs.get("platform_message_id")
        if value:
            return str(value)
        data = _as_mapping(kwargs.get("message"))
        if isinstance(data, dict):
            # 宿主把 ID 放在 message_base_info 下（命令通路），Hook 载荷则直接给
            # message_id；两处都要认，否则三条通道的去重键对不上。
            holders: list[dict[str, Any]] = [data]
            for holder_key in ("message_info", "message_base_info"):
                holder = data.get(holder_key)
                if isinstance(holder, dict):
                    holders.append(holder)
                    inner = holder.get("message_info")
                    if isinstance(inner, dict):
                        holders.append(inner)
            for holder in holders:
                for key in ("message_id", "platform_message_id"):
                    if holder.get(key):
                        return str(holder[key])
        return ""

    def _group_id(self, kwargs: dict[str, Any]) -> str:
        """解析群号，用于判断是否命中模糊白名单。

        Args:
            kwargs: 组件处理函数收到的关键字参数。

        Returns:
            str: 群号；私聊或取不到时返回空串。
        """

        for key in ("group_id", "groupId"):
            value = kwargs.get(key)
            if value:
                return str(value).strip()
        holders: list[Any] = []
        data = _as_mapping(kwargs.get("message"))
        if isinstance(data, dict):
            holders.append(data)
            info = data.get("message_info")
            if isinstance(info, dict):
                holders.append(info)
                group = info.get("group_info")
                if isinstance(group, dict):
                    holders.append(group)
        info = _as_mapping(kwargs.get("message_info"))
        if isinstance(info, dict):
            holders.append(info)
            group = info.get("group_info")
            if isinstance(group, dict):
                holders.append(group)
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            for key in ("group_id", "groupId"):
                value = holder.get(key)
                if value:
                    return str(value).strip()
        return ""

    def _blur_radius_for(self, kwargs: dict[str, Any]) -> int:
        """按群号白名单决定模糊半径。

        Args:
            kwargs: 组件处理函数收到的关键字参数。

        Returns:
            int: 白名单内返回 0（不模糊），其余返回配置的模糊半径。
        """

        section = self._section()
        if section is None:
            return 0
        radius = max(0, int(section.blur_radius))
        whitelist = parse_group_list(getattr(section, "blur_whitelist_groups", ""))
        if not whitelist:
            return radius
        group_id = self._group_id(kwargs)
        if group_id and group_id in whitelist:
            return 0
        return radius

    def _file_list_limit(self) -> int:
        """取兜底文字里文件清单的条数上限。

        Returns:
            int: 最多列出的文件数，0 表示不列。
        """

        section = self._section()
        if section is None:
            return 15
        try:
            return max(0, int(getattr(section, "file_list_limit", 15)))
        except (TypeError, ValueError):
            return 15

    def _claim(self, kwargs: dict[str, Any]) -> bool:
        """抢占一次处理权，10 秒内同一消息只放行一次。

        指令、LLM 工具、合并转发事件三条通路共用同一把锁：同一条消息无论被
        几个通道命中，都只会有一个真正去查询和发消息。

        Args:
            kwargs: 组件处理函数收到的关键字参数。

        Returns:
            bool: 首次处理返回 True，重复返回 False。
        """

        key = self._message_id(kwargs)
        if not key:
            return True
        now = time.monotonic()
        if len(self._recent) > 256:
            for item_key, stamp in list(self._recent.items()):
                if now - stamp >= DEDUPE_WINDOW:
                    self._recent.pop(item_key, None)
        last = self._recent.get(key)
        if last is not None and now - last < DEDUPE_WINDOW:
            return False
        self._recent[key] = now
        return True

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        """加载时初始化 HTTP 客户端并打印生效配置。"""

        section = self._section()
        if section is not None:
            self._log().info(
                "%s已加载：图片模式=%s，模糊半径=%s（白名单 %s 个群），截图上限=%s，缓存=%s 小时，自动转发=%s，itorrents 兜底=%s",
                PLUGIN_NAME,
                "开" if section.image_mode else "关",
                section.blur_radius,
                len(parse_group_list(getattr(section, "blur_whitelist_groups", ""))),
                section.max_screenshots,
                section.cache_ttl_hours,
                "开" if section.auto_forward_trigger else "关",
                "开" if getattr(section, "fallback_itorrents", True) else "关",
            )
        else:
            self._log().warning("%s已加载，但没读到配置节，将使用默认值", PLUGIN_NAME)
        if not _PIL_READY:
            self._log().warning("%s未找到 Pillow，图片卡片不可用，将只发文字", PLUGIN_NAME)
        try:
            await self._ensure_client()
        except Exception as exc:  # noqa: BLE001 - 客户端按需重建，这里失败不阻塞加载
            self._log().warning("%s初始化 HTTP 客户端失败：%s", PLUGIN_NAME, exc)

    async def on_unload(self) -> None:
        """卸载时关闭 HTTP 客户端，避免连接泄漏。"""

        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                self._log().warning("%s关闭 HTTP 客户端失败：%s", PLUGIN_NAME, exc)
        self._recent.clear()
        self._log().info("%s已卸载", PLUGIN_NAME)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新：重建 HTTP 客户端以让代理 / 超时立即生效。

        Args:
            scope: 更新范围，只有 ``self`` 才处理。
            config_data: 新的配置字典。
            version: 配置版本号。
        """

        if scope != "self":
            return
        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                self._log().warning("%s重建客户端时关闭旧连接失败：%s", PLUGIN_NAME, exc)
        self._log().info("%s配置已更新：版本=%s", PLUGIN_NAME, version or "无")

    # -- 网络 -------------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        """懒加载 httpx 客户端。

        Returns:
            httpx.AsyncClient: 可复用的异步客户端。
        """

        if self._client is not None and not self._client.is_closed:
            return self._client
        section = self._section()
        timeout = 15.0 if section is None else float(max(5, int(section.request_timeout)))
        proxy = "" if section is None else str(section.proxy or "").strip()
        options: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
            "follow_redirects": True,
            "headers": {"User-Agent": USER_AGENT},
        }
        if proxy:
            options["proxy"] = proxy
        self._client = httpx.AsyncClient(**options)
        return self._client

    async def _request_payload(self, magnet: str) -> tuple[dict[str, Any] | None, str]:
        """请求 whatslink，含限频重试。

        Args:
            magnet: 规范化后的磁链。

        Returns:
            tuple[dict | None, str]: ``(响应体, 错误提示)``，成功时错误为空串。
        """

        section = self._section()
        retry_times = 2 if section is None else max(0, int(section.retry_times))
        deadline = time.monotonic() + TOTAL_BUDGET
        last_error = "查询失败，请稍后再试。"

        for attempt in range(retry_times + 1):
            try:
                client = await self._ensure_client()
                response = await client.get(API_URL, params={"url": magnet})
            except httpx.TimeoutException:
                return None, "查询超时了，稍后再试一次。"
            except httpx.HTTPError as exc:
                self._log().warning("%s请求失败：%s", PLUGIN_NAME, exc)
                return None, "网络异常，连不上查询服务，稍后再试。"
            except Exception as exc:  # noqa: BLE001 - 代理地址非法等配置问题
                self._log().warning("%s请求异常：%s", PLUGIN_NAME, exc)
                return None, "请求查询服务时出错，请检查代理配置或稍后再试。"

            if response.status_code != 200:
                self._log().warning("%s接口返回 HTTP %s", PLUGIN_NAME, response.status_code)
                return None, f"查询服务返回异常状态（HTTP {response.status_code}），稍后再试。"

            try:
                payload = response.json()
            except ValueError:
                return None, "查询服务返回了无法解析的内容，稍后再试。"
            if not isinstance(payload, dict):
                return None, "查询服务返回了意外的数据格式。"

            error = str(payload.get("error") or "").strip()
            if not error:
                return payload, ""
            if error == "quota_limited":
                last_error = "查询服务额度已用完（quota_limited），请稍后再试。"
                if attempt >= retry_times or time.monotonic() + RETRY_INTERVAL >= deadline:
                    break
                await asyncio.sleep(RETRY_INTERVAL)
                continue
            if error == "no_files":
                return None, "没有查到这个种子的文件信息。"
            return None, f"查询失败：{error}"

        return None, last_error

    def _normalize_payload(self, payload: dict[str, Any], fallback_hash: str) -> dict[str, Any] | None:
        """把 whatslink 响应整理成插件内部结构。

        Args:
            payload: 接口原始响应。
            fallback_hash: 响应里没有 hash 时的兜底值。

        Returns:
            dict | None: 归一化信息；内容类型为 UNKNOWN 时返回 None。
        """

        kind = str(payload.get("type") or "").strip()
        if not kind or kind.upper() == "UNKNOWN":
            return None

        screenshots: list[str] = []
        raw_shots = payload.get("screenshots")
        if isinstance(raw_shots, list):
            for item in raw_shots:
                if isinstance(item, dict):
                    url = str(item.get("screenshot") or "").strip()
                else:
                    url = str(item or "").strip()
                if url:
                    screenshots.append(url)

        def _as_int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return {
            "hash": str(payload.get("hash") or fallback_hash or "").upper(),
            "type": kind,
            "file_type": str(payload.get("file_type") or "").strip(),
            "name": str(payload.get("name") or "未知").strip() or "未知",
            "size": _as_int(payload.get("size")),
            "count": _as_int(payload.get("count")),
            "screenshots": screenshots,
        }

    # -- 缓存 -------------------------------------------------------------

    def _cache_path(self) -> Path:
        """缓存文件路径。

        Returns:
            Path: ``<data_dir>/cache.json``。
        """

        return Path(self.ctx.paths.data_dir) / CACHE_FILE_NAME

    async def _cache_load(self) -> dict[str, Any]:
        """惰性加载缓存文件。

        Returns:
            dict: 缓存字典。
        """

        if self._cache is not None:
            return self._cache
        async with self._cache_lock:
            if self._cache is not None:
                return self._cache
            data: dict[str, Any] = {}
            try:
                path = self._cache_path()
                if path.is_file():
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
            except (OSError, ValueError, AttributeError) as exc:
                self._log().warning("%s读取缓存失败，按空缓存处理：%s", PLUGIN_NAME, exc)
            self._cache = data
        return self._cache

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        """读取未过期的缓存。

        Args:
            key: 种子哈希。

        Returns:
            dict | None: 命中的信息，未命中返回 None。
        """

        section = self._section()
        ttl_hours = 168 if section is None else int(section.cache_ttl_hours)
        if ttl_hours <= 0 or not key:
            return None
        cache = await self._cache_load()
        entry = cache.get(key)
        if not isinstance(entry, dict):
            return None
        try:
            stamp = float(entry.get("ts") or 0)
        except (TypeError, ValueError):
            return None
        if time.time() - stamp > ttl_hours * 3600:
            cache.pop(key, None)
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None

    async def _cache_put(self, key: str, info: dict[str, Any]) -> None:
        """写入缓存并落盘。

        Args:
            key: 种子哈希。
            info: 归一化后的种子信息。
        """

        section = self._section()
        ttl_hours = 168 if section is None else int(section.cache_ttl_hours)
        if ttl_hours <= 0 or not key:
            return
        async with self._cache_lock:
            cache = self._cache if isinstance(self._cache, dict) else {}
            cache[key] = {"ts": time.time(), "data": info}
            deadline = time.time() - ttl_hours * 3600
            for item_key in list(cache.keys()):
                entry = cache.get(item_key)
                try:
                    stamp = float(entry.get("ts") or 0) if isinstance(entry, dict) else 0.0
                except (TypeError, ValueError):
                    stamp = 0.0
                if stamp < deadline:
                    cache.pop(item_key, None)
            self._cache = cache
            self._write_cache(cache)

    def _write_cache(self, cache: dict[str, Any]) -> None:
        """原子写缓存文件。

        Args:
            cache: 待写入的缓存字典。
        """

        try:
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{CACHE_FILE_NAME}.tmp")
            temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            self._log().warning("%s写入缓存失败：%s", PLUGIN_NAME, exc)

    # -- 查询与发送 -------------------------------------------------------

    async def _fetch_torrent_meta(self, key: str) -> dict[str, Any] | None:
        """从 itorrents 按哈希取 .torrent 并解析（无截图）。

        Args:
            key: 种子哈希。

        Returns:
            dict | None: 归一化信息；取不到或解析失败返回 None。
        """

        if not key:
            return None
        url = f"{FALLBACK_TORRENT_URL}{key.upper()}.torrent"
        try:
            client = await self._ensure_client()
            response = await client.get(url, timeout=SCREENSHOT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 兜底失败只记日志
            self._log().warning("%sitorrents 请求失败：%s", PLUGIN_NAME, exc)
            return None
        if response.status_code != 200:
            self._log().info(
                "%sitorrents 没有这个种子（HTTP %s）：%s",
                PLUGIN_NAME,
                response.status_code,
                key[:12],
            )
            return None
        if len(response.content) > TORRENT_MAX_BYTES:
            self._log().warning("%sitorrents 返回的种子文件过大，已跳过：%s", PLUGIN_NAME, key[:12])
            return None
        meta = parse_torrent_meta(response.content, key)
        if meta is None:
            self._log().warning("%sitorrents 返回内容无法解析：%s", PLUGIN_NAME, key[:12])
        return meta

    async def _fallback_info(self, key: str) -> dict[str, Any] | None:
        """按配置决定是否启用 itorrents 兜底。

        Args:
            key: 种子哈希。

        Returns:
            dict | None: 兜底信息；未启用或失败返回 None。
        """

        section = self._section()
        if section is None or not bool(getattr(section, "fallback_itorrents", True)):
            return None
        return await self._fetch_torrent_meta(key)

    async def _fetch_info(self, magnet: str) -> tuple[dict[str, Any] | None, str]:
        """查询种子信息，优先走缓存；whatslink 不可用时用 itorrents 兜底。

        Args:
            magnet: 规范化后的磁链。

        Returns:
            tuple[dict | None, str]: ``(信息, 错误提示)``。
        """

        key = magnet_hash(magnet)
        if key:
            cached = await self._cache_get(key)
            if cached is not None:
                return cached, ""
        payload, error = await self._request_payload(magnet)
        info = self._normalize_payload(payload, key) if payload is not None else None
        if info is None:
            fallback = await self._fallback_info(key)
            if fallback is not None:
                self._log().info("%swhatslink 不可用，改用 itorrents 兜底：%s", PLUGIN_NAME, key[:12])
                if key:
                    await self._cache_put(key, fallback)
                return fallback, ""
            if payload is None:
                return None, error
            return None, "没查到这个种子的内容，可能是新种子，或查询服务还没收录。"
        if key:
            await self._cache_put(key, info)
        return info, ""

    async def _download_one_screenshot(self, url: str) -> bytes:
        """下载单张截图，失败重试一次（图片服务偶发 500）。

        Args:
            url: 截图地址。

        Returns:
            bytes: 图片字节；失败返回空 bytes。
        """

        for attempt in range(2):
            data = await self._fetch_screenshot_once(url)
            if data:
                return data
            if attempt == 0:
                await asyncio.sleep(0.5)
        return b""

    async def _fetch_screenshot_once(self, url: str) -> bytes:
        """下载一次截图，带大小上限、超时与逐跳白名单校验。

        Args:
            url: 截图地址。

        Returns:
            bytes: 图片字节；失败返回空 bytes。
        """

        try:
            client = await self._ensure_client()
            target = url
            # 手动跟随重定向：每一跳都复核白名单，避免白名单地址 302 到任意主机；
            # 流式累加字节数，防止超大响应先吃满内存再被丢弃。
            for _ in range(3):
                if not target.startswith(SCREENSHOT_PREFIX):
                    self._log().warning("%s跳过非白名单截图地址：%s", PLUGIN_NAME, target[:80])
                    return b""
                async with client.stream(
                    "GET",
                    target,
                    timeout=SCREENSHOT_TIMEOUT,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = str(response.headers.get("location") or "").strip()
                        if not location:
                            return b""
                        target = str(httpx.URL(target).join(location))
                        continue
                    if response.status_code != 200:
                        self._log().warning(
                            "%s截图返回 HTTP %s：%s", PLUGIN_NAME, response.status_code, target[:80]
                        )
                        return b""
                    declared = str(response.headers.get("content-length") or "").strip()
                    if declared.isdigit() and int(declared) > SCREENSHOT_MAX_BYTES:
                        self._log().warning("%s截图超过 8MB，已跳过：%s", PLUGIN_NAME, target[:80])
                        return b""
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > SCREENSHOT_MAX_BYTES:
                            self._log().warning("%s截图超过 8MB，已跳过：%s", PLUGIN_NAME, target[:80])
                            return b""
                        chunks.append(chunk)
                    return b"".join(chunks)
            self._log().warning("%s截图重定向次数过多，已跳过：%s", PLUGIN_NAME, url[:80])
            return b""
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响整体
            self._log().warning("%s下载截图失败：%s", PLUGIN_NAME, exc)
            return b""

    async def _download_screenshots(self, urls: list[str], limit: int) -> list[bytes]:
        """按白名单下载截图。

        Args:
            urls: 接口返回的截图地址列表。
            limit: 最多下载几张。

        Returns:
            list[bytes]: 成功下载的图片字节列表。
        """

        if limit <= 0:
            return []
        candidates: list[str] = []
        for url in urls:
            if len(candidates) >= limit:
                break
            target = str(url or "").strip()
            if not target.startswith(SCREENSHOT_PREFIX):
                self._log().warning("%s跳过非白名单截图地址：%s", PLUGIN_NAME, target[:80])
                continue
            candidates.append(target)
        if not candidates:
            return []
        # 并发下载：串行最坏情况会是 3 × 15 秒，用户等不了
        results = await asyncio.gather(
            *(self._download_one_screenshot(url) for url in candidates),
            return_exceptions=True,
        )
        shots: list[bytes] = []
        for item in results:
            if isinstance(item, BaseException):
                self._log().warning("%s截图下载异常：%s", PLUGIN_NAME, item)
                continue
            if item:
                shots.append(item)
        return shots

    async def _render_card(self, info: dict[str, Any], blur_radius: int = 0) -> bytes | None:
        """渲染卡片，任何异常都降级成 None。

        Args:
            info: 归一化后的种子信息。
            blur_radius: 截图高斯模糊半径，0 表示不模糊。

        Returns:
            bytes | None: 图片字节；不可用时返回 None。
        """

        section = self._section()
        if not _PIL_READY or section is None:
            return None
        try:
            shots = await self._download_screenshots(
                list(info.get("screenshots") or []),
                int(section.max_screenshots),
            )
            return await asyncio.to_thread(render_card, info, shots, int(blur_radius))
        except Exception as exc:  # noqa: BLE001 - 渲染失败必须能降级
            self._log().warning("%s渲染卡片失败，改用纯文本：%s", PLUGIN_NAME, exc)
            return None

    async def _send_text(self, stream_id: str, text: str) -> bool:
        """发送文本，失败只记日志。

        Args:
            stream_id: 目标会话。
            text: 文本内容。

        Returns:
            bool: 是否发送成功。
        """

        if not stream_id:
            return False
        try:
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception as exc:  # noqa: BLE001
            self._log().warning("%s发送文本失败：%s", PLUGIN_NAME, exc)
            return False

    async def _send_image(self, stream_id: str, data: bytes) -> bool:
        """发送图片（base64）。

        Args:
            stream_id: 目标会话。
            data: 图片字节。

        Returns:
            bool: 是否发送成功。
        """

        if not stream_id or not data:
            return False
        try:
            encoded = base64.b64encode(data).decode("ascii")
            return bool(await self.ctx.send.image(encoded, stream_id))
        except Exception as exc:  # noqa: BLE001
            self._log().warning("%s发送图片失败：%s", PLUGIN_NAME, exc)
            return False

    async def _deliver(
        self,
        magnet: str,
        stream_id: str,
        kwargs: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """查询并发结果，返回给调用方一句摘要。

        Args:
            magnet: 规范化后的磁链。
            stream_id: 目标会话。
            kwargs: 触发通道的原始参数，用于判断群号白名单。

        Returns:
            tuple[bool, str]: ``(是否成功发送, 中文摘要)``。
        """

        if not stream_id:
            self._log().warning("%s拿不到 stream_id，无法发送结果", PLUGIN_NAME)
            return False, "拿不到会话 ID，没法发结果。"
        info, error = await self._fetch_info(magnet)
        if info is None:
            await self._send_text(stream_id, f"❌ {error}")
            return False, error

        summary = f"{info.get('name') or '未知'}，{format_size(info.get('size'))}，{int(info.get('count') or 0)} 个文件"
        section = self._section()
        is_fallback = str(info.get("source") or "") == "itorrents"
        if section is not None and section.image_mode and not is_fallback:
            blur_radius = self._blur_radius_for(kwargs or {})
            self._log().info(
                "%s模糊判定：群=%s，半径=%s",
                PLUGIN_NAME,
                self._group_id(kwargs or {}) or "未知",
                blur_radius,
            )
            card = await self._render_card(info, blur_radius)
            if card and await self._send_image(stream_id, card):
                return True, summary
            if card:
                self._log().warning("%s卡片发送失败，改用纯文本", PLUGIN_NAME)
        if await self._send_text(stream_id, format_info_text(info, self._file_list_limit())):
            return True, summary
        return False, "结果发送失败。"

    # -- 组件 -------------------------------------------------------------

    @Command(
        "torrent_analyze_cmd",
        description="验车：查询磁力链或种子哈希的名称、类型、大小与截图",
        pattern=COMMAND_PATTERN,
    )
    async def torrent_analyze_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理「验车 / 种子分析 / 种子信息 / 种子详情」指令。

        Args:
            stream_id: 宿主注入的会话 ID。
            kwargs: 含 matched_groups、message 等。

        Returns:
            tuple[bool, str]: ``(是否已处理, 说明)``。
        """

        section = self._section()
        if section is None or not section.enabled:
            return False, "", False

        stream = self._stream_id(kwargs, stream_id)
        matched = kwargs.get("matched_groups")
        query = ""
        if isinstance(matched, dict):
            query = str(matched.get("query") or "").strip()

        if query in HELP_WORDS:
            if self._claim(kwargs):
                await self._send_text(stream, HELP_TEXT)
            return True, "已发送验车帮助", True

        if not self._claim(kwargs):
            return True, "", True

        query_magnet = normalize_magnet(query)
        text = collect_text(kwargs)
        magnet = query_magnet or extract_first_magnet(text)
        if not magnet:
            if any(mark in text for mark in FORWARD_PLACEHOLDER_MARKS):
                await self._send_text(stream, FORWARD_PLACEHOLDER_HINT)
                return True, "引用的是转发占位，取不到磁链", True
            await self._send_text(stream, USAGE_HINT)
            return True, "未识别到磁链或种子哈希", True

        _ok, message = await self._deliver(magnet, stream, kwargs)
        return True, message, True

    @Command(
        "torrent_help",
        description="验车帮助：查看用法",
        pattern=HELP_PATTERN,
    )
    async def torrent_help_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """处理「验车帮助」指令。

        Args:
            stream_id: 宿主注入的会话 ID。
            kwargs: 组件关键字参数。

        Returns:
            tuple[bool, str]: ``(是否已处理, 说明)``。
        """

        section = self._section()
        if section is None or not section.enabled:
            return False, "", False
        stream = self._stream_id(kwargs, stream_id)
        if self._claim(kwargs):
            await self._send_text(stream, HELP_TEXT)
        return True, "已发送验车帮助", True

    @Tool(
        "torrent_analyze",
        brief_description="查询磁力链或种子哈希的内容信息：名称、类型、大小、文件数与截图。",
        detailed_description=(
            "群里出现 magnet 磁力链或 40 位 / 32 位 btih 种子哈希、需要知道里面是什么内容时调用。"
            "插件会自己把结果（图片卡片或文字）发到当前会话，"
            "你只需要回一句「已发送种子信息」之类的说明，不要复述名称、大小等细节，也不要再补充解释。"
            "一次只传一个磁链；同一条消息不要重复调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="magnet",
                param_type=ToolParamType.STRING,
                description="磁力链（magnet:?xt=urn:btih:...）或 40 位 hex / 32 位 base32 种子哈希",
                required=True,
            )
        ],
    )
    async def torrent_analyze_tool(self, magnet: str = "", **kwargs: Any) -> dict[str, Any]:
        """LLM 工具入口：查完自己发消息，只回一句摘要。

        Args:
            magnet: LLM 传入的磁链或哈希。
            kwargs: 含 stream_id / message 等。

        Returns:
            dict[str, Any]: ``{"content": "..."}``。
        """

        section = self._section()
        if section is None or not section.enabled:
            return {"content": "验车插件已停用，无法查询。"}

        stream = self._stream_id(kwargs)
        if not stream:
            return {"content": "拿不到当前会话 ID，无法发送查询结果。"}

        target = normalize_magnet(magnet) or extract_first_magnet(collect_text(kwargs))
        if not target:
            return {"content": "这不是有效的磁链或种子哈希，无法查询。"}

        if not self._claim(kwargs):
            return {"content": "这条消息刚查过，结果已经发送过了。"}

        ok, message = await self._deliver(target, stream, kwargs)
        if not ok:
            return {"content": f"查询没成功：{message}"}
        return {"content": f"已发送种子信息：{message}。无需复述细节。"}

    @HookHandler(
        "chat.receive.after_process",
        name="auto_forward_torrent",
        description="合并转发消息里出现磁链时自动验车",
        mode=HookMode.OBSERVE,
    )
    async def auto_forward_torrent(self, message: Any = None, **kwargs: Any) -> None:
        """合并转发自动验车（挂 chat.receive.after_process Hook）。

        Args:
            message: 宿主注入的序列化 SessionMessage 字典。
            kwargs: Hook 附带的其他参数。
        """

        try:
            if message is not None:
                kwargs.setdefault("message", message)
            section = self._section()
            if section is None or not section.enabled or not section.auto_forward_trigger:
                return
            texts = collect_texts(kwargs)
            if not texts:
                return
            if not looks_like_forward(kwargs, texts):
                return
            self._log().info("%s检测到合并转发，开始提取磁链", PLUGIN_NAME)
            magnet = extract_first_magnet("\n".join(texts))
            if not magnet:
                self._log().info("%s合并转发里没提取到磁链", PLUGIN_NAME)
                return
            stream = self._stream_id(kwargs)
            if not stream:
                return
            if not self._claim(kwargs):
                return
            await self._deliver(magnet, stream, kwargs)
        except Exception as exc:  # noqa: BLE001 - 事件处理绝不能把异常抛给宿主
            self._log().warning("%s合并转发自动验车失败：%s", PLUGIN_NAME, exc)


def create_plugin() -> TorrentAnalyzePlugin:
    """创建插件实例。

    Returns:
        TorrentAnalyzePlugin: 插件实例。
    """

    return TorrentAnalyzePlugin()
