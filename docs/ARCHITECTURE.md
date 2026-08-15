# 开发架构

## 设计目标

1. 独立于日报插件和 MaiBot 主程序。
2. 绝大多数群消息只经过本地代码，不逐条调用模型。
3. 查询和提醒必须带可追溯证据。
4. 模型、嵌入和外部网络失败时行为明确。
5. 默认权限收紧，避免跨群信息泄露。

## MaiBot 接入层

`plugin.py` 负责：

- 三个强制生命周期方法。
- `@EventHandler(EventType.ON_MESSAGE)` 非阻塞观察。
- `@Command` 命令注册。
- 调用 `ctx.message`、`ctx.chat`、`ctx.llm` 和 `ctx.send`。
- 把业务请求交给 `core/`，不在入口文件重复实现算法。

## 业务模块

- `config_models.py`：WebUI 强类型配置。
- `core/models.py`：规则、消息、查询计划和外部资料模型。
- `core/message_utils.py`：官方消息载荷归一化。
- `core/rule_engine.py`：AND/OR/NOT、正则和跨消息窗口。
- `core/storage.py`：规则、草稿、命中与冷却状态。
- `core/intent_parser.py`：自然语言规则草稿和寻迹计划。
- `core/retrieval.py`：本地召回、嵌入重排和上下文扩展。
- `core/reporting.py`：证据提示词与确定性降级文本。
- `core/search.py`：可选通用 JSON 查询接口。

## 实时观察流程

```text
ON_MESSAGE 非阻塞事件
  → 只接受群聊文字和白名单群
  → 当前消息是否贡献关键词/正则信号
  → 读取该群内存时间窗
  → AND/OR/NOT/正则组合判断
  → 可选语义复核
  → SQLite 原子执行次数阈值与冷却判断
  → 可选外部查询
  → 私聊规则创建者和额外通知用户
```

语义描述不能单独成为实时规则。这样不会让模型检查每一条群消息。

## 历史寻迹流程

```text
命令或回复片段
  → 模型提取查询目标、关键词、排除词和时间
  → 解析目标群真实聊天流
  → ctx.message 读取有限历史
  → 本地文字相关性召回
  → 可选 ctx.llm.embed 语义重排
  → 补齐前后消息
  → 可选外部查询
  → 事实复核模型根据 E/S 编号回答
```

## 持久化

插件使用短生命周期 SQLite 连接和 WAL 模式。阈值统计、去重、冷却时间更新放在同一事务中，避免并发消息导致重复提醒。

## 不做的事情

- 不修改 MaiBot 模型配置文件。
- 不直接访问 MaiBot 内部 ORM。
- 不伪装成用户消息注入聊天历史。
- 不让外部查询失败中断本地功能。
- 不在日志中输出 API 密钥。
