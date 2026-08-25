from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import IsolatedAsyncioTestCase, TestCase

import asyncio
import json
import sys

from maibot_sdk.context import PluginContext, PluginPaths

from core.models import CompositeRule


def _load_plugin_like_maibot():
    module_name = "test_group_trace_plugin"
    plugin_dir = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location(
        module_name,
        plugin_dir / "plugin.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建测试插件模块")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


plugin_module = _load_plugin_like_maibot()


class ManifestContractTests(TestCase):
    def test_manifest_v2_and_runtime_baseline(self) -> None:
        manifest = json.loads(Path("_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["host_application"]["min_version"], "1.1.0")
        self.assertEqual(manifest["sdk"]["min_version"], "2.7.0")
        self.assertEqual(
            set(manifest["capabilities"]),
            {
                "send.text",
                "llm.generate",
                "llm.embed",
                "chat.get_group_streams",
                "chat.get_stream_by_group_id",
                "chat.open_session",
                "message.get_by_time_in_chat",
                "message.get_by_id",
            },
        )
        source = Path("plugin.py").read_text(encoding="utf-8")
        self.assertNotIn("from src", source)
        self.assertNotIn("import src", source)
        self.assertNotIn("from config_models", source)
        self.assertNotIn("from core", source)

    def test_plugin_components_and_webui_schema(self) -> None:
        instance = plugin_module.create_plugin()
        component_types = [item["type"] for item in instance.get_components()]
        self.assertEqual(component_types.count("EVENT_HANDLER"), 1)
        self.assertEqual(component_types.count("COMMAND"), 10)
        schema = instance.get_webui_config_schema(plugin_id="github.artinsteinbrecher-lab.group-trace")
        self.assertTrue(schema)


class PluginLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.calls = []
        self.page_calls = 0
        self.paging_base = time() - 1000

        async def rpc(method, plugin_id, payload, timeout_ms=None):
            self.calls.append((method, plugin_id, payload, timeout_ms))
            capability = payload.get("capability") if isinstance(payload, dict) else ""
            args = payload.get("args", {}) if isinstance(payload, dict) else {}
            if capability == "chat.open_session":
                return {"success": True, "stream_id": "private-stream", "stream": {"stream_id": "private-stream"}}
            if capability == "chat.get_stream_by_group_id":
                return {
                    "success": True,
                    "stream": {"stream_id": "group-stream", "group_id": "20001", "group_name": "模型群"},
                }
            if capability == "message.get_by_time_in_chat":
                if args.get("limit") == 100:
                    # 分页模式：第一页返回整页灌水消息，第二页返回更早的相关消息
                    self.page_calls += 1
                    base = self.paging_base
                    if self.page_calls == 1:
                        return {
                            "success": True,
                            "messages": [
                                self._message(f"noise-{index}", f"水群消息{index}", timestamp=str(base + index))
                                for index in range(100)
                            ],
                        }
                    if self.page_calls == 2:
                        return {
                            "success": True,
                            "messages": [
                                self._message(
                                    "m-old", "之前有人说 DSV4F 默认输出限制是 64K", timestamp=str(base - 5000)
                                ),
                                self._message("m-old-2", "max_tokens 需要另外配置", timestamp=str(base - 4990)),
                            ],
                        }
                    return {"success": True, "messages": []}
                base = self.paging_base
                return {
                    "success": True,
                    "messages": [
                        self._message("m-old", "之前有人说 DSV4F 默认输出限制是 64K", timestamp=str(base)),
                        self._message("m-new", "max_tokens 需要另外配置", timestamp=str(base + 10)),
                    ],
                }
            if capability == "llm.generate":
                prompt = str(args.get("prompt") or "")
                if "转换为群聊观察规则草稿" in prompt:
                    response = (
                        '{"name":"输出限制","required_terms":["DSV4F"],'
                        '"any_terms":["64K","max_tokens"],"excluded_terms":["价格"],'
                        '"regex_patterns":[],"semantic_description":"讨论 DSV4F 输出限制",'
                        '"window_seconds":600,"min_occurrences":1,"cooldown_seconds":1800}'
                    )
                elif "整理为检索计划" in prompt:
                    response = (
                        '{"search_query":"DSV4F 输出限制","keywords":["DSV4F","64K","max_tokens"],'
                        '"excluded_terms":[],"history_days":30}'
                    )
                else:
                    response = "群聊里有人提到 DSV4F 默认输出限制是 64K [E1]，并提到了 max_tokens 配置 [E2]。"
                return {"success": True, "response": response, "model": "test-model"}
            if capability == "llm.embed":
                texts = args.get("texts") or []
                return {
                    "success": True,
                    "results": [{"embedding": [1.0, float(index)]} for index, _text in enumerate(texts)],
                }
            if capability == "send.text":
                return {"success": True}
            return {"success": True}

        self.instance = plugin_module.create_plugin()
        self.instance.set_plugin_config(
            {
                **self.instance.get_default_config(),
                "access": {
                    "admin_user_ids": ["10001"],
                    "allowed_group_ids": ["20001"],
                    "notification_user_ids": [],
                },
                "monitoring": {
                    "enabled": True,
                    "max_buffer_messages": 200,
                    "evidence_messages": 12,
                    "semantic_verify_enabled": False,
                    "search_on_trigger": False,
                },
            }
        )
        context = PluginContext(
            "github.artinsteinbrecher-lab.group-trace",
            rpc,
            PluginPaths(
                data_dir=Path(self.temp_dir.name) / "data",
                runtime_dir=Path(self.temp_dir.name) / "temp",
            ),
        )
        self.instance._set_context(context)
        await self.instance.on_load()

    @staticmethod
    def _message(message_id: str, text: str, timestamp: str = "1000"):
        return {
            "message_id": message_id,
            "timestamp": timestamp,
            "platform": "qq",
            "session_id": "group-stream",
            "processed_plain_text": text,
            "message_info": {
                "group_info": {"group_id": "20001", "group_name": "模型群"},
                "user_info": {"user_id": "30001", "user_nickname": "群友", "user_cardname": ""},
            },
            "raw_message": [],
        }

    @staticmethod
    def _command_kwargs(command_text: str, matched_groups):
        return {
            "stream_id": "private-command-stream",
            "raw_message": command_text,
            "matched_groups": matched_groups,
            "message": {
                "message_id": "command-1",
                "session_id": "private-command-stream",
                "message_info": {
                    "group_info": None,
                    "user_info": {"user_id": "10001", "user_nickname": "管理员", "user_cardname": ""},
                },
            },
        }

    async def asyncTearDown(self) -> None:
        await self.instance.on_unload()
        self.temp_dir.cleanup()

    async def test_local_monitor_match_notifies_owner(self) -> None:
        rule = CompositeRule(
            id="abcdef1234",
            name="输出限制",
            owner_user_id="10001",
            group_ids=["20001"],
            required_terms=["DSV4F"],
            any_terms=["64K"],
            semantic_description="讨论输出限制",
            min_occurrences=1,
            cooldown_seconds=0,
            created_at=1.0,
        )
        await self.instance._store.save_rule(rule)
        await self.instance._refresh_rules()
        message = self._message("m1", "DSV4F 默认只有 64K 吗")
        await self.instance.observe_group_message(message=message)
        capabilities = [call[2].get("capability") for call in self.calls if isinstance(call[2], dict)]
        self.assertIn("chat.open_session", capabilities)
        self.assertIn("send.text", capabilities)

    async def test_create_then_confirm_rule(self) -> None:
        create_result = await self.instance.handle_create_rule(
            **self._command_kwargs(
                "/监控创建 20001 帮我关注 DSV4F 输出限制，价格不用管",
                {"group_id": "20001", "description": "帮我关注 DSV4F 输出限制，价格不用管"},
            )
        )
        self.assertTrue(create_result[0])
        self.assertIn("/监控确认", create_result[1])
        confirm_result = await self.instance.handle_confirm_rule(
            **self._command_kwargs("/监控确认", {})
        )
        self.assertTrue(confirm_result[0])
        rules = await self.instance._store.list_rules(owner_user_id="10001")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].excluded_terms, ["价格"])

    async def _search_and_wait_answer(self) -> str:
        """发起寻迹命令，等待后台任务完成，返回实际发送给用户的最后一条消息。"""

        result = await self.instance.handle_search(
            **self._command_kwargs(
                "/寻迹 20001 找之前讨论的输出限制",
                {"group_id": "20001", "query": "找之前讨论的输出限制"},
            )
        )
        self.assertTrue(result[0])
        await asyncio.gather(*tuple(self.instance._background_tasks))
        sent_texts = [
            str(call[2]["args"].get("text") or "")
            for call in self.calls
            if isinstance(call[2], dict) and call[2].get("capability") == "send.text"
        ]
        self.assertTrue(sent_texts)
        return sent_texts[-1]

    async def test_history_search_uses_intent_embedding_and_evidence_answer(self) -> None:
        answer = await self._search_and_wait_answer()
        self.assertIn("[E1]", answer)
        self.assertIn("检索说明", answer)
        self.assertIn("DSV4F", answer)
        capabilities = [call[2].get("capability") for call in self.calls if isinstance(call[2], dict)]
        self.assertIn("message.get_by_time_in_chat", capabilities)
        self.assertIn("llm.embed", capabilities)
        generation_calls = [
            call[2]["args"]
            for call in self.calls
            if isinstance(call[2], dict) and call[2].get("capability") == "llm.generate"
        ]
        self.assertTrue(generation_calls)
        self.assertTrue(all(call.get("model") == "utils" for call in generation_calls))

    async def test_history_search_pages_backwards_for_full_window(self) -> None:
        config = self.instance.get_default_config()
        self.instance.set_plugin_config(
            {
                **config,
                "access": {
                    "admin_user_ids": ["10001"],
                    "allowed_group_ids": ["20001"],
                    "notification_user_ids": [],
                },
                "retrieval": {**config["retrieval"], "max_history_messages": 100},
            }
        )
        answer = await self._search_and_wait_answer()
        fetch_calls = [
            call
            for call in self.calls
            if isinstance(call[2], dict) and call[2].get("capability") == "message.get_by_time_in_chat"
        ]
        # 第一页返回整整 100 条后应继续向回翻页，读到更早的相关消息
        self.assertEqual(len(fetch_calls), 2)
        self.assertIn("共扫描 102 条消息", answer)

    async def test_scan_floor_prevents_rescanning_exhausted_history(self) -> None:
        config = self.instance.get_default_config()
        self.instance.set_plugin_config(
            {
                **config,
                "access": {
                    "admin_user_ids": ["10001"],
                    "allowed_group_ids": ["20001"],
                    "notification_user_ids": [],
                },
                # 关闭结果缓存，验证扫描水位本身能阻止重复扫描
                "retrieval": {**config["retrieval"], "max_history_messages": 100, "answer_cache_seconds": 0},
            }
        )
        await self._search_and_wait_answer()
        await self._search_and_wait_answer()
        fetch_calls = [
            call
            for call in self.calls
            if isinstance(call[2], dict) and call[2].get("capability") == "message.get_by_time_in_chat"
        ]
        # 第一次查询扫描两页后已到宿主历史边界；第二次不应再发起扫描
        self.assertEqual(len(fetch_calls), 2)

    async def test_identical_query_returns_cached_answer(self) -> None:
        first = await self._search_and_wait_answer()
        second = await self._search_and_wait_answer()
        self.assertNotIn("缓存结果", first)
        self.assertIn("缓存结果", second)
