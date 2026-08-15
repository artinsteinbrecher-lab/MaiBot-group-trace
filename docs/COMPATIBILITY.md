# 兼容性

## 明确支持的基线

- MaiBot：`1.1.0` 至 `1.x`
- maibot-plugin-sdk：`2.7.0` 至 `2.x`

上述最低版本已经在 MaiBot `1.1.0`、maibot-plugin-sdk `2.7.0` 的真实容器中完成导入、组件发现和 WebUI 配置结构检查。
- Python：`3.12` 或更高的兼容版本
- 消息平台：首版针对 QQ/NapCat 聊天流

这些范围写入 `_manifest.json`，Host 会在加载前校验。

## 使用的官方能力

- `send.text`
- `llm.generate`
- `llm.embed`
- `chat.get_group_streams`
- `chat.get_stream_by_group_id`
- `chat.open_session`
- `message.get_by_time_in_chat`
- `message.get_recent`
- `message.get_by_id`
- `message.build_readable`

插件没有直接导入 `src.*`，也没有读取 MaiBot 内部数据库模型。

## 功能降级

- 嵌入任务失败：保留有明确文字命中的候选结果，并记录警告。
- 需求理解模型失败：按用户原文检索，并在结果中说明。
- 事实回答模型失败：返回原始证据，不伪造总结。
- 语义复核失败：本次监控不提醒，避免把不确定内容当成命中。
- 外部查询失败：不影响群聊历史检索和本地监控。

## 已知边界

- 只能查询 MaiBot 已经保存的消息。
- 首版不负责从 QQ 服务器补拉安装以前的完整历史。
- 首版不做截图 OCR。
- 多账号部署可以读取 Host 返回的既有聊天流；0.1.0 的私聊提醒按 QQ 用户号打开会话，后续会根据真实多账号测试补充账号路由设置。
