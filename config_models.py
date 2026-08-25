"""MaiBot WebUI 使用的强类型配置模型。"""

from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件总开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "scan-search"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="关闭后停止监听消息和执行查询命令",
        json_schema_extra={"label": "启用麦麦群聊寻迹"},
    )
    config_version: str = Field(
        default="0.1.0",
        description="配置结构版本，请勿手动修改",
        json_schema_extra={"label": "配置版本", "readonly": True},
    )


class AccessSection(PluginConfigBase):
    """命令权限和可查询群聊白名单。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield-check"
    __ui_order__ = 2

    admin_user_ids: List[str] = Field(
        default_factory=list,
        description="允许创建监控和发起跨群查询的 QQ 号；留空时所有管理命令均拒绝执行",
        json_schema_extra={"label": "管理员 QQ", "hint": "推荐只填写你自己的 QQ 号"},
    )
    allowed_group_ids: List[str] = Field(
        default_factory=list,
        description="允许监控和检索的 QQ 群号；留空时不允许访问任何群",
        json_schema_extra={"label": "允许访问的群聊", "hint": "只填写确实需要查询的群号"},
    )
    notification_user_ids: List[str] = Field(
        default_factory=list,
        description="监控命中后额外私聊通知的 QQ 号；规则创建者始终会收到通知",
        json_schema_extra={"label": "额外通知用户"},
    )


class MonitoringSection(PluginConfigBase):
    """实时复合关键词观察设置。"""

    __ui_label__ = "关键词观察"
    __ui_icon__ = "radar"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="是否观察新进入 MaiBot 的群聊消息",
        json_schema_extra={"label": "启用实时观察"},
    )
    max_buffer_messages: int = Field(
        default=200,
        ge=20,
        le=1000,
        description="每个群在内存中保留的最近消息数量，用于跨消息复合匹配",
        json_schema_extra={"label": "每群上下文消息上限"},
    )
    evidence_messages: int = Field(
        default=12,
        ge=3,
        le=50,
        description="命中提醒中最多附带多少条群聊证据",
        json_schema_extra={"label": "提醒证据条数"},
    )
    semantic_verify_enabled: bool = Field(
        default=True,
        description="本地复合规则命中后，再由模型判断讨论是否真的相关",
        json_schema_extra={"label": "启用语义复核"},
    )
    search_on_trigger: bool = Field(
        default=False,
        description="监控命中后自动查询外部资料；需要先配置外部查询服务",
        json_schema_extra={"label": "命中后查询外部资料"},
    )


class RetrievalSection(PluginConfigBase):
    """指定群历史寻迹设置。"""

    __ui_label__ = "群聊寻迹"
    __ui_icon__ = "search"
    __ui_order__ = 4

    history_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="未指定时间时默认检索最近多少天",
        json_schema_extra={"label": "默认检索天数"},
    )
    max_history_messages: int = Field(
        default=1200,
        ge=100,
        le=10000,
        description="单页从 MaiBot 读取的历史消息数量；插件会从最新往回分页扫描直到覆盖完整时间范围",
        json_schema_extra={"label": "历史消息上限（单页）"},
    )
    scan_messages: int = Field(
        default=12000,
        ge=1000,
        le=100000,
        description="单次寻迹累计扫描的消息总数上限；高流量群建议调大，避免时间范围覆盖不全",
        json_schema_extra={"label": "扫描消息上限"},
    )
    lexical_candidates: int = Field(
        default=100,
        ge=20,
        le=500,
        description="先由本地文本检索筛出的候选消息数量",
        json_schema_extra={"label": "本地候选数量"},
    )
    evidence_messages: int = Field(
        default=16,
        ge=3,
        le=60,
        description="最终交给模型和展示的证据消息上限",
        json_schema_extra={"label": "查询证据条数"},
    )
    context_radius: int = Field(
        default=2,
        ge=0,
        le=10,
        description="每条匹配消息前后额外保留多少条上下文",
        json_schema_extra={"label": "前后上下文条数"},
    )
    use_embeddings: bool = Field(
        default=True,
        description="使用 MaiBot 已有嵌入任务进行语义重排；失败时保留本地检索结果",
        json_schema_extra={"label": "启用嵌入语义检索"},
    )
    local_index_enabled: bool = Field(
        default=True,
        description="把白名单群的文本消息写入插件本地索引，寻迹时按关键词直查；索引未覆盖的更早时段自动扫描补齐",
        json_schema_extra={"label": "启用本地关键词索引"},
    )
    index_retention_days: int = Field(
        default=180,
        ge=7,
        le=730,
        description="本地索引保留天数；超期消息和移出白名单群的消息会被自动清理",
        json_schema_extra={"label": "索引保留天数"},
    )


class ModelSection(PluginConfigBase):
    """可选的 MaiBot 高级模型任务名称。"""

    __ui_label__ = "模型任务"
    __ui_icon__ = "brain"
    __ui_order__ = 5

    intent_task: str = Field(
        default="utils",
        description="理解自然语言规则和寻迹需求的 MaiBot 模型任务名；留空时安全回退到 utils",
        json_schema_extra={"label": "需求理解任务", "placeholder": "默认使用 utils"},
    )
    verify_task: str = Field(
        default="utils",
        description="语义复核和证据回答的 MaiBot 模型任务名；留空时安全回退到 utils",
        json_schema_extra={"label": "事实复核任务", "placeholder": "默认使用 utils"},
    )
    embedding_task: str = Field(
        default="embedding",
        description="语义检索使用的 MaiBot 嵌入任务名",
        json_schema_extra={"label": "嵌入任务"},
    )
    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="规则解析和事实回答温度，较低更稳定",
        json_schema_extra={"label": "模型温度"},
    )


class SearchSection(PluginConfigBase):
    """可选的通用 JSON 查询接口。"""

    __ui_label__ = "外部资料查询"
    __ui_icon__ = "globe-search"
    __ui_order__ = 6

    enabled: bool = Field(
        default=False,
        description="启用后才会访问配置的查询接口；不影响群聊本地检索",
        json_schema_extra={"label": "启用外部查询"},
    )
    endpoint: str = Field(
        default="",
        description="返回 JSON 的 HTTP/HTTPS 查询地址，可用 {query} 占位符",
        json_schema_extra={"label": "查询接口地址", "placeholder": "https://example.com/search"},
    )
    api_key: str = Field(
        default="",
        description="可选 API 密钥；插件不会把它写入日志或查询结果",
        json_schema_extra={"label": "API 密钥"},
    )
    authorization_header: str = Field(
        default="Authorization",
        description="承载 API 密钥的请求头名称",
        json_schema_extra={"label": "密钥请求头"},
    )
    authorization_prefix: str = Field(
        default="Bearer ",
        description="添加在 API 密钥前面的文本；不需要前缀时留空",
        json_schema_extra={"label": "密钥前缀"},
    )
    query_parameter: str = Field(
        default="q",
        description="接口没有使用 {query} 时追加的查询参数名称",
        json_schema_extra={"label": "查询参数名"},
    )
    results_path: str = Field(
        default="results",
        description="JSON 中结果数组的点分路径，例如 data.results",
        json_schema_extra={"label": "结果数组路径"},
    )
    title_field: str = Field(default="title", description="结果标题字段", json_schema_extra={"label": "标题字段"})
    url_field: str = Field(default="url", description="结果链接字段", json_schema_extra={"label": "链接字段"})
    snippet_field: str = Field(
        default="snippet",
        description="结果摘要字段",
        json_schema_extra={"label": "摘要字段"},
    )
    date_field: str = Field(
        default="published_at",
        description="结果发布时间字段",
        json_schema_extra={"label": "时间字段"},
    )
    timeout_seconds: int = Field(
        default=15,
        ge=3,
        le=60,
        description="外部查询超时时间",
        json_schema_extra={"label": "查询超时（秒）"},
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="单次最多保留多少条外部资料",
        json_schema_extra={"label": "资料条数"},
    )


class GroupTraceConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    access: AccessSection = Field(default_factory=AccessSection)
    monitoring: MonitoringSection = Field(default_factory=MonitoringSection)
    retrieval: RetrievalSection = Field(default_factory=RetrievalSection)
    models: ModelSection = Field(default_factory=ModelSection)
    search: SearchSection = Field(default_factory=SearchSection)
