"""验车插件配置模型。

所有字段都挂在 ``[plugin]`` 配置节下，WebUI 依据 ``__ui_label__`` /
``__ui_icon__`` / ``__ui_order__`` 把这一节渲染成一页表单。
配置改动支持热更新，不必重启 MaiBot。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import Field, PluginConfigBase


def _ui(
    label: str,
    hint: str = "",
    *,
    placeholder: str = "",
    hidden: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """构造 WebUI 字段的展示元数据。

    Args:
        label: 字段标题。
        hint: 字段下方的小字提示。
        placeholder: 输入框占位提示。
        hidden: 是否在表单中隐藏。
        extra: 额外透传给 WebUI 的键值。

    Returns:
        dict[str, Any]: 可直接塞进 ``json_schema_extra`` 的字典。
    """

    payload: dict[str, Any] = {"label": label, "hint": hint or None, "hidden": hidden}
    if placeholder:
        payload["placeholder"] = placeholder
    payload.update(extra)
    return payload


class PluginSectionConfig(PluginConfigBase):
    """验车插件设置。"""

    __ui_label__ = "麦麦验车设置"
    __ui_icon__ = "magnet"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="插件总开关，关闭后指令、工具与自动触发都不再响应。",
        json_schema_extra=_ui("启用插件", "关掉后不再响应「验车」，也不会自动验车。"),
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，由插件维护，不需要手动修改。",
        json_schema_extra=_ui("配置版本", hidden=True),
    )
    image_mode: bool = Field(
        default=True,
        description="是否返回「文字信息 + 截图」拼接的图片卡片，关闭则只发文字。",
        json_schema_extra=_ui("返回截图卡片", "开启后返回图片卡片；渲染失败会自动降级为纯文本。"),
    )
    blur_radius: int = Field(
        default=10,
        ge=0,
        le=50,
        description="白名单外群聊的截图高斯模糊半径，0 表示不模糊。",
        json_schema_extra=_ui(
            "模糊半径（白名单外）",
            "白名单内的群不模糊，其他群按这个半径模糊；0 = 原图，常用 10～20。",
        ),
    )
    blur_whitelist_groups: str = Field(
        default="",
        description="不模糊的群号列表，用逗号或空格分隔；留空表示所有群都模糊。",
        json_schema_extra=_ui(
            "模糊白名单（群号）",
            "这些群不模糊，其他群自动模糊；多个群号用逗号分隔，留空表示全部模糊。",
            placeholder="828605435, 123456789",
        ),
    )
    max_screenshots: int = Field(
        default=3,
        ge=0,
        le=3,
        description="卡片里最多拼接几张截图，0 表示不拼截图。",
        json_schema_extra=_ui("截图数量上限", "最多 3 张，0 表示只出文字卡片。"),
    )
    cache_ttl_hours: int = Field(
        default=168,
        ge=0,
        le=8760,
        description="查询结果缓存时长（小时），0 表示不缓存。",
        json_schema_extra=_ui("缓存时长（小时）", "默认 168 小时 = 7 天；0 表示每次都重新查询。"),
    )
    request_timeout: int = Field(
        default=15,
        ge=5,
        le=60,
        description="单次 HTTP 请求超时秒数。",
        json_schema_extra=_ui("请求超时（秒）", "网络慢可以调大，建议不超过 30。"),
    )
    retry_times: int = Field(
        default=2,
        ge=0,
        le=5,
        description="查询服务限频（quota_limited）时的重试次数，每次间隔 3 秒。",
        json_schema_extra=_ui("限频重试次数", "默认重试 2 次；总耗时约 15 秒封顶。"),
    )
    proxy: str = Field(
        default="",
        description="HTTP 代理地址，留空表示直连。",
        json_schema_extra=_ui(
            "代理地址",
            "例如 http://127.0.0.1:7890；留空表示直连。",
            placeholder="http://127.0.0.1:7890",
        ),
    )
    fallback_itorrents: bool = Field(
        default=True,
        description="whatslink 不可用时，改用 itorrents 按哈希取种子元数据（只能出文字和文件清单，没有截图）。",
        json_schema_extra=_ui(
            "itorrents 兜底",
            "whatslink 挂了自动切 itorrents，秒级返回、没有截图；关掉则只报错。",
        ),
    )
    file_list_limit: int = Field(
        default=15,
        ge=0,
        le=100,
        description="兜底文字里最多列出多少个文件，0 表示不列清单。",
        json_schema_extra=_ui("文件清单条数", "兜底结果最多列多少个文件，默认 15，0 表示不列。"),
    )
    auto_forward_trigger: bool = Field(
        default=False,
        description="收到含磁链的合并转发消息时自动验车（默认关闭）。",
        json_schema_extra=_ui(
            "合并转发自动验车",
            "默认关闭；开启后转发一条含磁链的聊天记录就会自动出结果。",
        ),
    )


class TorrentAnalyzeConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
