# 参与开发

提交修改前请确保：

- 不直接导入 MaiBot `src.*`。
- 新能力通过 `_manifest.json` 明确声明。
- 配置通过 `PluginConfigBase` 定义，不提交实际 `config.toml`。
- 复杂逻辑放在 `core/` 并补充测试。
- 用户可见文字优先使用简体中文。
- 不在测试、日志或提交记录中放入真实 QQ 号、聊天内容或 API 密钥。
- `python -m unittest discover -s tests -v` 全部通过。
