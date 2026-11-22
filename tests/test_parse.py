"""验车插件纯函数单测。

不依赖 MaiBot SDK 运行时：本机 / 服务器上若没装 ``maibot_sdk``，会自动注入最小桩模块，
只为把 ``plugin.py`` 导入起来跑解析与格式化逻辑。

跑法：
    python tests/test_parse.py          # 直接跑，无需 pytest
    python -m pytest tests -q           # 装了 pytest 也可以
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "torrent_plugin_under_test"

HASH40 = "32181995C9D274FCFBE0A5E427F047210E82A53D"
HASH32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
MAGNET40 = f"magnet:?xt=urn:btih:{HASH40}"


def _install_sdk_stub() -> None:
    """SDK 不可用时注入最小桩，保证 plugin.py 能被导入。"""

    try:
        import maibot_sdk  # noqa: F401
    except ImportError:
        pass
    else:
        return

    class _ParamInfo:
        """ToolParameterInfo 桩。"""

        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class _PluginConfigBase:
        """PluginConfigBase 桩。"""

    class _MaiBotPlugin:
        """MaiBotPlugin 桩。"""

        def __init__(self) -> None:
            pass

    def _decorator(*args: object, **kwargs: object):
        """装饰器桩：原样返回被装饰函数。"""

        def wrap(func):
            return func

        return wrap

    def _field(default=None, **kwargs: object):
        """Field 桩：直接返回默认值。"""

        return default

    package = types.ModuleType("maibot_sdk")
    package.__path__ = []  # type: ignore[attr-defined]
    package.Command = _decorator
    package.Tool = _decorator
    package.EventHandler = _decorator
    package.HookHandler = _decorator
    package.Field = _field
    package.PluginConfigBase = _PluginConfigBase
    package.MaiBotPlugin = _MaiBotPlugin

    types_module = types.ModuleType("maibot_sdk.types")
    types_module.EventType = types.SimpleNamespace(
        ON_MESSAGE_PRE_PROCESS="on_message_pre_process",
        ON_MESSAGE="on_message",
    )
    types_module.ToolParamType = types.SimpleNamespace(
        STRING="string",
        INTEGER="integer",
        NUMBER="number",
        BOOLEAN="boolean",
        ARRAY="array",
        OBJECT="object",
    )
    types_module.ToolParameterInfo = _ParamInfo
    types_module.HookMode = types.SimpleNamespace(BLOCKING="blocking", OBSERVE="observe")

    sys.modules["maibot_sdk"] = package
    sys.modules["maibot_sdk.types"] = types_module


def _load_plugin():
    """以包的形式加载 plugin.py，避免相对导入失败。"""

    _install_sdk_stub()
    cached = sys.modules.get(MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法构造 plugin.py 的导入规格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin()


# ---------------------------------------------------------------------------
# 磁链解析
# ---------------------------------------------------------------------------


def test_normalize_hash40() -> None:
    """40 位 hex 补成磁链。"""

    assert PLUGIN.normalize_magnet(HASH40) == MAGNET40


def test_normalize_hash32() -> None:
    """32 位 base32 补成磁链。"""

    assert PLUGIN.normalize_magnet(HASH32) == f"magnet:?xt=urn:btih:{HASH32}"


def test_normalize_full_magnet() -> None:
    """完整磁链原样透传。"""

    assert PLUGIN.normalize_magnet(MAGNET40) == MAGNET40


def test_normalize_magnet_keeps_dn() -> None:
    """带 &dn= 的磁链参数不能丢。"""

    raw = f"magnet:?xt=urn:btih:{HASH40}&dn=demo%20name&tr=udp://tracker.example:80"
    assert PLUGIN.normalize_magnet(raw) == raw


def test_normalize_invalid_inputs() -> None:
    """非法输入统一返回空串。"""

    for value in ("", "  ", "随便", "验车 随便", "12345", "magnet:?xt=urn:sha1:abc", "not-a-magnet"):
        assert PLUGIN.normalize_magnet(value) == "", value


def test_extract_from_reply_text() -> None:
    """引用消息被文本化后仍能提取磁链。"""

    text = f"[回复了小明 的消息: {MAGNET40}&dn=demo] 验车"
    assert PLUGIN.extract_first_magnet(text) == f"{MAGNET40}&dn=demo"


def test_extract_from_forward_text() -> None:
    """合并转发被文本化后仍能提取磁链。"""

    text = f"【合并转发消息: \n【小明】: 你看这个 {MAGNET40}\n【小红】: 收到\n】"
    assert PLUGIN.extract_first_magnet(text) == MAGNET40


def test_extract_prefers_magnet_over_hash() -> None:
    """同时出现裸 hash 与磁链时优先取磁链。"""

    text = f"哈希 {HASH40} 磁链 {MAGNET40}&dn=demo"
    assert PLUGIN.extract_first_magnet(text) == f"{MAGNET40}&dn=demo"


def test_extract_hash40_from_text() -> None:
    """长文本里的裸 40 位 hash 能被识别。"""

    text = f"求助 验车 {HASH40} 谢谢"
    assert PLUGIN.extract_first_magnet(text) == MAGNET40


def test_extract_hash32_from_text() -> None:
    """长文本里的裸 32 位 base32 能被识别。"""

    text = f"试一下 {HASH32} 看看"
    assert PLUGIN.extract_first_magnet(text) == f"magnet:?xt=urn:btih:{HASH32}"


def test_extract_empty_and_invalid() -> None:
    """没有磁链时返回空串。"""

    for text in ("", "   ", "随便聊聊", "magnet:?xt=urn:sha1:abc", "[回复了xx的消息: 你好]"):
        assert PLUGIN.extract_first_magnet(text) == "", text


def test_magnet_hash() -> None:
    """缓存键取磁链里的 hash。"""

    assert PLUGIN.magnet_hash(MAGNET40) == HASH40
    assert PLUGIN.magnet_hash(f"{MAGNET40}&dn=x") == HASH40
    assert PLUGIN.magnet_hash(HASH40) == HASH40
    assert PLUGIN.magnet_hash("") == ""


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def test_format_size() -> None:
    """大小格式化保持 1 位小数。"""

    assert PLUGIN.format_size(0) == "未知"
    assert PLUGIN.format_size(None) == "未知"
    assert PLUGIN.format_size("abc") == "未知"
    assert PLUGIN.format_size(512) == "512 B"
    assert PLUGIN.format_size(1024) == "1.0 KB"
    assert PLUGIN.format_size(1536) == "1.5 KB"
    assert PLUGIN.format_size(1024**3) == "1.0 GB"
    assert PLUGIN.format_size(1234567890) == "1.1 GB"


def test_format_type() -> None:
    """类型拼接成 type-file_type。"""

    assert PLUGIN.format_type({"type": "video", "file_type": "mp4"}) == "video-mp4"
    assert PLUGIN.format_type({"type": "video", "file_type": ""}) == "video"
    assert PLUGIN.format_type({}) == "未知"


def test_format_info_text() -> None:
    """纯文本兜底包含五个字段。"""

    info = {
        "hash": HASH40,
        "type": "video",
        "file_type": "mp4",
        "name": "示例种子",
        "size": 1536,
        "count": 4,
    }
    text = PLUGIN.format_info_text(info)
    assert "🧲 种子信息" in text
    assert f"哈希：{HASH40}" in text
    assert "类型：video-mp4" in text
    assert "名称：示例种子" in text
    assert "大小：1.5 KB" in text
    assert "文件数：4 个" in text


# ---------------------------------------------------------------------------
# 消息文本收集
# ---------------------------------------------------------------------------


def test_collect_texts_from_message() -> None:
    """从 message 字典里收集文本，供给正则提取。"""

    kwargs = {"message": {"processed_plain_text": f"[回复了小明 的消息: {MAGNET40}] 验车"}}
    texts = PLUGIN.collect_texts(kwargs)
    assert PLUGIN.extract_first_magnet("\n".join(texts)) == MAGNET40


def test_looks_like_forward() -> None:
    """合并转发段与文本化标记都能识别。"""

    forward = {"message": {"message_segments": [{"type": "forward", "data": {}}]}}
    plain = {"message": {"message_segments": [{"type": "text", "data": {}}]}}
    text_only = {"processed_plain_text": "【合并转发消息: \n【小明】: hi\n】"}
    assert PLUGIN.looks_like_forward(forward) is True
    assert PLUGIN.looks_like_forward(text_only) is True
    assert PLUGIN.looks_like_forward(plain) is False


def test_hook_payload_fields() -> None:
    """Hook 载荷（序列化 SessionMessage）能取出会话 ID、消息 ID 与转发标记。"""

    payload = {
        "message": {
            "message_id": "1763126789",
            "session_id": "qq_group_828605435",
            "processed_plain_text": f"【合并转发消息: \n【小明】: {MAGNET40}\n】",
            "raw_message": {
                "segments": [
                    {
                        "type": "forward",
                        "data": {"content": [{"type": "text", "data": MAGNET40}]},
                    }
                ]
            },
            "message_info": {"message_info": {"message_id": "1763126789"}},
        }
    }
    plugin = PLUGIN.TorrentAnalyzePlugin()
    assert PLUGIN.looks_like_forward(payload) is True
    assert PLUGIN.extract_first_magnet("\n".join(PLUGIN.collect_texts(payload))) == MAGNET40
    assert plugin._stream_id(payload) == "qq_group_828605435"
    assert plugin._message_id(payload) == "1763126789"
    # 命令通路的载荷把 ID 放在 message_base_info 下
    assert plugin._message_id({"message": {"message_base_info": {"message_id": "9988"}}}) == "9988"


def test_blur_whitelist() -> None:
    """群号白名单：解析、群号提取与半径判定。"""

    assert PLUGIN.parse_group_list("828605435, 123456789") == {"828605435", "123456789"}
    assert PLUGIN.parse_group_list("828605435 666666") == {"828605435", "666666"}
    assert PLUGIN.parse_group_list("") == set()

    plugin = PLUGIN.TorrentAnalyzePlugin()
    payload = {
        "message": {
            "message_id": "1",
            "session_id": "hash",
            "message_info": {"group_info": {"group_id": "828605435"}},
        }
    }
    assert plugin._group_id(payload) == "828605435"
    assert plugin._group_id({"group_id": 828605435}) == "828605435"
    assert plugin._group_id({}) == ""

    plugin.config = types.SimpleNamespace(
        plugin=types.SimpleNamespace(blur_radius=10, blur_whitelist_groups="828605435")
    )
    assert plugin._blur_radius_for({"group_id": "828605435"}) == 0
    assert plugin._blur_radius_for({"group_id": "111111"}) == 10
    # 白名单留空：全部按白名单外处理
    plugin.config = types.SimpleNamespace(
        plugin=types.SimpleNamespace(blur_radius=10, blur_whitelist_groups="")
    )
    assert plugin._blur_radius_for({"group_id": "828605435"}) == 10


def _bencode(value) -> bytes:
    """测试用最小 bencode 编码器。"""

    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in sorted(value.items())) + b"e"
    raise TypeError(type(value))


def test_parse_torrent_meta() -> None:
    """bencode 解析：多文件、单文件、坏数据。"""

    folder = _bencode(
        {
            b"info": {
                b"name": "测试包".encode(),
                b"files": [
                    {b"length": 100, b"path": [b"a.jpg"]},
                    {b"length": 200, b"path": [b"sub", b"b.jpg"]},
                ],
            }
        }
    )
    meta = PLUGIN.parse_torrent_meta(folder, "ABC")
    assert meta is not None
    assert meta["type"] == "FOLDER" and meta["count"] == 2 and meta["size"] == 300
    assert meta["files"][1]["path"] == "sub/b.jpg"
    assert meta["source"] == "itorrents"
    text = PLUGIN.format_info_text(meta, 1)
    assert "文件清单" in text and "另有 1 个" in text and "itorrents" in text

    single = _bencode({b"info": {b"name": b"movie.mkv", b"length": 1024}})
    one = PLUGIN.parse_torrent_meta(single, "DEF")
    assert one is not None and one["type"] == "FILE" and one["count"] == 1 and one["size"] == 1024
    assert one["file_type"] == "mkv"

    assert PLUGIN.parse_torrent_meta(b"not-a-torrent", "X") is None
    assert PLUGIN.parse_torrent_meta(b"", "X") is None
    # 嵌套过深会触发 RecursionError，必须按解析失败降级，不能冒泡给宿主
    assert PLUGIN.parse_torrent_meta(b"l" * 3000 + b"e" * 3000, "X") is None


def _run_all() -> int:
    """不装 pytest 时的极简执行器。

    Returns:
        int: 失败用例数量。
    """

    failures = 0
    for name, func in list(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"\n共 {failures} 个失败")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
