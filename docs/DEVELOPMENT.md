# 开发与测试

## 规范来源

本插件以当前 MaiBot 插件 SDK 文档为准：

- <https://docs.mai-mai.org/plugin/>
- <https://docs.mai-mai.org/plugin/manifest>
- <https://docs.mai-mai.org/plugin/lifecycle>
- <https://docs.mai-mai.org/plugin/config>
- <https://docs.mai-mai.org/plugin/event-handlers>
- <https://docs.mai-mai.org/plugin/commands>
- <https://docs.mai-mai.org/plugin/api-reference>
- <https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md>

不使用旧版 `PluginAction`、`BaseEventHandler` 或 `WorkflowStep`。

## 本地测试

把官方 SDK 源码目录加入 `PYTHONPATH` 后运行：

```powershell
$env:PYTHONPATH = "C:\path\to\maibot-plugin-sdk"
python -m unittest discover -s tests -v
```

语法检查：

```powershell
python -m compileall -q .
```

如果本机安装了 Ruff：

```powershell
python -m ruff check .
python -m ruff format --check .
```

## 构建发行包

```powershell
python scripts/build_release.py
```

生成：

```text
dist/MaiBot-group-trace-v0.1.0.zip
dist/SHA256SUMS.txt
```

发行包不包含：

- `config.toml`
- SQLite 数据
- 测试缓存
- Git 元数据
- 本地密钥

## 真实环境验收

1. Host 正常校验 Manifest 并加载插件。
2. WebUI 能显示全部配置分组。
3. 未配置管理员和群白名单时命令被拒绝。
4. 配置后 `/寻迹群列表` 显示真实群名。
5. `/监控创建` 只产生草稿，确认后才生效。
6. 跨两条消息的复合关键词能够命中。
7. 排除词、次数阈值和冷却时间正常。
8. `/寻迹` 返回带 E 编号的真实消息依据。
9. 模型、嵌入或外部查询失败时符合兼容性文档。
