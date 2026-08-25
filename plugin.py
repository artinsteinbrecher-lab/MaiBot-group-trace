"""麦麦群聊寻迹插件入口。

本模块只使用 maibot-plugin-sdk 暴露的正式能力，不直接导入 MaiBot ``src.*``。
实时观察使用非阻塞 ON_MESSAGE 事件；所有长期数据写入 ``ctx.paths.data_dir``。
"""

from __future__ import annotations

from asyncio import Semaphore, Task, create_task
from datetime import datetime
from time import time
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple, cast

from maibot_sdk import Command, EventHandler, MaiBotPlugin
from maibot_sdk.types import EventType

from .config_models import GroupTraceConfig
from .core.intent_parser import IntentParseError, RuleIntentParser, extract_json_object, format_rule_draft
from .core.message_utils import (
    earliest_raw_timestamp,
    extract_command_identity,
    extract_payload_text,
    normalize_message,
    normalize_messages,
    raw_message_count,
    resolve_stream_id,
)
from .core.models import CompositeRule, MessageRecord, PendingRule, QueryPlan, SearchResult
from .core.reporting import (
    attach_evidence_footer,
    build_answer_prompt,
    build_semantic_verify_prompt,
    fallback_history_answer,
    format_monitor_notification,
)
from .core.retrieval import (
    expand_context,
    extract_query_terms,
    matches_any_term,
    rank_lexically,
    rerank_with_embeddings,
    select_diverse_seeds,
)
from .core.rule_engine import CompositeRuleEngine, RuleValidationError, normalize_text
from .core.search import JsonSearchClient, SearchError, SearchSettings
from .core.storage import StateStore


# 关键词直查一次最多取回的候选消息数；先取回再评分，评分后才截断
_INDEX_FETCH_LIMIT = 2000


class GroupTracePlugin(MaiBotPlugin):
    """指定群历史寻迹与复合关键词观察。"""

    config_model = GroupTraceConfig

    async def on_load(self) -> None:
        self._ready = False
        self._rules: List[CompositeRule] = []
        self._admin_user_ids: Set[str] = set()
        self._allowed_group_ids: Set[str] = set()
        self._notification_user_ids: Set[str] = set()
        self._buffer_limit = 0
        self._engine = CompositeRuleEngine()
        self._model_semaphore = Semaphore(2)
        self._search_client: JsonSearchClient | None = None
        self._background_tasks: Set[Task[None]] = set()
        # 相同查询的结果缓存：key -> (过期时间, 生成时间, 回答)
        self._answer_cache: Dict[str, Tuple[float, float, str]] = {}
        self._store = StateStore(self.ctx.paths.data_dir / "group_trace.sqlite3")
        await self._store.initialize()
        await self._refresh_runtime_state(rebuild_engine=True)
        self._ready = True
        self.ctx.logger.info(
            "麦麦群聊寻迹已加载：%d 条规则，%d 个允许群聊",
            len(self._rules),
            len(self._allowed_group_ids),
        )

    async def on_unload(self) -> None:
        self._ready = False
        self._rules = []
        for task in tuple(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self.ctx.logger.info("麦麦群聊寻迹已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != "self" or not hasattr(self, "_store"):
            return
        await self._refresh_runtime_state(rebuild_engine=False)
        self.ctx.logger.info("麦麦群聊寻迹配置已热更新：version=%s", version)

    @EventHandler(
        "group_trace_observer",
        description="旁路观察允许群聊中的复合关键词，不阻塞正常消息链",
        event_type=EventType.ON_MESSAGE,
        intercept_message=False,
        weight=-20,
    )
    async def observe_group_message(self, message: Any = None, **kwargs: Any) -> None:
        del kwargs
        if not self._ready or not self._config().plugin.enabled:
            return
        if isinstance(message, Mapping) and bool(message.get("is_command")):
            return
        record = normalize_message(message)
        if record is None or not self._is_group_allowed(record.group_id):
            return
        if self._config().retrieval.local_index_enabled:
            try:
                await self._store.index_messages([record])
            except Exception as exc:
                self.ctx.logger.warning("写入本地消息索引失败：%s", exc)
        if not self._config().monitoring.enabled:
            return
        self._engine.push(record)

        for rule in tuple(self._rules):
            if not rule.enabled or record.group_id not in rule.group_ids:
                continue
            if not self._engine.message_has_signal(rule, record):
                continue
            context = self._engine.recent_for_rule(rule, now=record.timestamp)
            decision = self._engine.evaluate(rule, context)
            if not decision.matched:
                continue
            if self._config().monitoring.semantic_verify_enabled and rule.semantic_description:
                if not await self._semantic_verify(rule, context):
                    continue
            should_trigger = await self._store.record_candidate_and_should_trigger(
                rule,
                message_id=record.message_id,
                group_id=record.group_id,
                matched_text=record.text,
                occurred_at=record.timestamp,
            )
            if not should_trigger:
                continue
            evidence_limit = self._config().monitoring.evidence_messages
            evidence = context[-evidence_limit:]
            search_results: List[SearchResult] = []
            if self._config().monitoring.search_on_trigger:
                search_results = await self._search_external(rule.semantic_description or rule.name)
            notification = format_monitor_notification(
                rule,
                record.group_name,
                decision.matched_terms,
                evidence,
                search_results,
            )
            await self._notify_users(rule.owner_user_id, notification)

    @Command(
        "group_trace_search",
        description="根据文字或回复片段检索指定 QQ 群的历史消息",
        pattern=r"^/寻迹\s+(?P<group_id>\d+)(?:\s+(?P<query>[\s\S]+))?$",
    )
    async def handle_search(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        group_id = str(groups.get("group_id") or "").strip()
        if not self._is_group_allowed(group_id):
            return await self._reply(stream_id, "这个群不在插件允许访问的群聊名单中。", success=False)
        query = str(groups.get("query") or "").strip()
        if not query:
            query = await self._query_from_reply(kwargs)
        if not query:
            return await self._reply(
                stream_id,
                "请在群号后写出要找的线索，或者回复一条文字消息后发送 /寻迹 群号。",
                success=False,
            )

        await self.ctx.send.text(
            "我开始查看这个群现有的历史消息，完成后会把结果发到这里。首次检索需要建立索引，可能需要几分钟。",
            stream_id,
        )
        # 首次建索引 + 扫描 + 模型整理可能超过宿主的命令超时，
        # 因此在后台完成检索，结果通过消息发送而不是命令返回值。
        task = create_task(self._search_and_send(stream_id, group_id, query))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True, "寻迹任务已开始，结果将异步发送。", 2

    async def _search_and_send(self, stream_id: str, group_id: str, query: str) -> None:
        cache_ttl = self._config().retrieval.answer_cache_seconds
        cache_key = f"{group_id}|{normalize_text(query)}"
        now = time()
        cached = self._answer_cache.get(cache_key)
        if cache_ttl > 0 and cached and cached[0] > now:
            age_minutes = max(1, int((now - cached[1]) / 60))
            answer = cached[2] + f"\n\n（相同查询的缓存结果，生成于约 {age_minutes} 分钟前）"
            await self.ctx.send.text(answer[:14000], stream_id)
            return
        try:
            answer = await self._search_group_history(group_id, query)
        except Exception as exc:
            self.ctx.logger.error("寻迹执行失败：%s", exc, exc_info=True)
            answer = "寻迹执行过程中出现内部错误，请稍后重试；如果反复出现请查看服务端日志。"
        else:
            if cache_ttl > 0:
                if len(self._answer_cache) > 64:
                    self._answer_cache = {
                        key: value for key, value in self._answer_cache.items() if value[0] > now
                    }
                self._answer_cache[cache_key] = (now + cache_ttl, now, answer)
        sent = await self.ctx.send.text(answer[:14000], stream_id)
        if not sent:
            self.ctx.logger.error("寻迹结果发送失败：stream=%s", stream_id)

    @Command(
        "group_trace_create_rule",
        description="用自然语言创建一条等待确认的复合关键词观察规则",
        pattern=r"^/监控创建\s+(?P<group_id>\d+)\s+(?P<description>[\s\S]+)$",
    )
    async def handle_create_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        group_id = str(groups.get("group_id") or "").strip()
        description = str(groups.get("description") or "").strip()
        if not self._is_group_allowed(group_id):
            return await self._reply(stream_id, "这个群不在插件允许访问的群聊名单中。", success=False)
        try:
            parser = RuleIntentParser(self._intent_generate)
            rule = await parser.parse_rule(description, group_id, user_id)
            self._engine.validate(rule)
        except (IntentParseError, RuleValidationError) as exc:
            return await self._reply(stream_id, f"我没能生成安全可执行的规则：{exc}", success=False)
        pending = PendingRule(
            user_id=user_id,
            source_stream_id=stream_id,
            rule=rule,
            expires_at=time() + 900,
        )
        await self._store.save_pending(pending)
        return await self._reply(stream_id, format_rule_draft(rule), success=True)

    @Command("group_trace_confirm_rule", description="确认最近生成的观察规则", pattern=r"^/监控确认\s*$")
    async def handle_confirm_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        pending = await self._store.pop_pending(user_id)
        if pending is None:
            return await self._reply(stream_id, "没有等待确认的规则，或者草稿已经超过15分钟。", success=False)
        try:
            self._engine.validate(pending.rule)
        except RuleValidationError as exc:
            return await self._reply(stream_id, f"规则没有保存：{exc}", success=False)
        if any(not self._is_group_allowed(group_id) for group_id in pending.rule.group_ids):
            return await self._reply(stream_id, "规则目标群已经不在允许名单中，因此没有保存。", success=False)
        await self._store.save_rule(pending.rule)
        await self._refresh_rules()
        return await self._reply(
            stream_id,
            f"规则已启用：{pending.rule.name}\n规则编号：{pending.rule.id}",
            success=True,
        )

    @Command("group_trace_cancel_rule", description="取消最近生成的观察规则", pattern=r"^/监控取消\s*$")
    async def handle_cancel_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        pending = await self._store.pop_pending(user_id)
        text = "已取消规则草稿。" if pending else "当前没有等待确认的规则草稿。"
        return await self._reply(stream_id, text, success=True)

    @Command("group_trace_list_rules", description="列出自己创建的观察规则", pattern=r"^/监控列表\s*$")
    async def handle_list_rules(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        rules = await self._store.list_rules(owner_user_id=user_id)
        if not rules:
            return await self._reply(stream_id, "你还没有保存任何观察规则。", success=True)
        lines = ["你的观察规则："]
        for rule in rules:
            state = "运行中" if rule.enabled else "已暂停"
            lines.append(f"- {rule.id}｜{rule.name}｜群 {'、'.join(rule.group_ids)}｜{state}")
        return await self._reply(stream_id, "\n".join(lines), success=True)

    @Command(
        "group_trace_pause_rule",
        description="暂停自己创建的观察规则",
        pattern=r"^/监控暂停\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_pause_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        return await self._set_rule_state(kwargs, enabled=False)

    @Command(
        "group_trace_resume_rule",
        description="恢复自己创建的观察规则",
        pattern=r"^/监控恢复\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_resume_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        return await self._set_rule_state(kwargs, enabled=True)

    @Command(
        "group_trace_delete_rule",
        description="删除自己创建的观察规则",
        pattern=r"^/监控删除\s+(?P<rule_id>[a-fA-F0-9]{6,32})\s*$",
    )
    async def handle_delete_rule(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        rule_id = str(groups.get("rule_id") or "").lower()
        deleted = await self._store.delete_rule(rule_id, owner_user_id=user_id)
        if deleted:
            await self._refresh_rules()
        text = f"已删除规则 {rule_id}。" if deleted else "没有找到属于你的这条规则。"
        return await self._reply(stream_id, text, success=deleted)

    @Command("group_trace_groups", description="查看当前允许访问且 MaiBot 已知的群聊", pattern=r"^/寻迹群列表\s*$")
    async def handle_group_list(self, **kwargs: Any) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        streams = await self.ctx.chat.get_group_streams(platform="qq")
        lines = ["当前可用于寻迹的群聊："]
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, Mapping):
                    continue
                group_id = str(stream.get("group_id") or "").strip()
                if not self._is_group_allowed(group_id):
                    continue
                group_name = str(stream.get("group_name") or f"群聊{group_id}").strip()
                lines.append(f"- {group_name}（{group_id}）")
        if len(lines) == 1:
            lines.append("暂时没有同时满足白名单和已建立聊天流条件的群。")
        return await self._reply(stream_id, "\n".join(lines), success=True)

    @Command("group_trace_help", description="查看麦麦群聊寻迹的简明帮助", pattern=r"^/寻迹帮助\s*$")
    async def handle_help(self, **kwargs: Any) -> Tuple[bool, str, int]:
        _user_id, stream_id = extract_command_identity(kwargs)
        text = (
            "麦麦群聊寻迹\n"
            "/寻迹 群号 线索 —— 查找指定群的历史消息\n"
            "也可以回复一条文字消息后发送 /寻迹 群号\n"
            "/监控创建 群号 需求 —— 生成复合关键词规则草稿\n"
            "/监控确认 或 /监控取消\n"
            "/监控列表\n"
            "/监控暂停 规则编号\n"
            "/监控恢复 规则编号\n"
            "/监控删除 规则编号\n"
            "/寻迹群列表"
        )
        return await self._reply(stream_id, text, success=True)

    async def _search_group_history(self, group_id: str, original_query: str) -> str:
        config = self._config()
        plan_note = ""
        try:
            parser = RuleIntentParser(self._intent_generate)
            plan = await parser.parse_query(original_query, config.retrieval.history_days)
        except IntentParseError as exc:
            self.ctx.logger.warning("寻迹需求理解失败，使用用户原文检索：%s", exc)
            plan = QueryPlan(search_query=original_query, history_days=config.retrieval.history_days)
            plan_note = "\n提示：需求理解模型暂时不可用，本次按原文完成检索。"

        stream = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform="qq")
        group_stream_id = resolve_stream_id(stream)
        if not group_stream_id:
            return "MaiBot 还没有建立这个群的聊天流，因此暂时无法读取该群历史消息。"
        end_time = time()
        start_time = end_time - plan.history_days * 86400
        # 索引直查只用用户原词和模型明确列出的别名；模型改写的整句
        # search_query 不再参与关键词提取，避免拆出泛词污染命中统计。
        primary_terms = extract_query_terms(original_query)
        expansion_terms = [
            term
            for term in extract_query_terms(" ".join(plan.keywords))
            if term not in primary_terms
        ]
        retrieval_query = " ".join([original_query, *plan.keywords]).strip()
        primary_display = _display_terms(primary_terms)
        expansion_display = "、".join(
            [keyword for keyword in plan.keywords if normalize_text(keyword) not in primary_terms][:8]
        )

        use_index = config.retrieval.local_index_enabled and bool(primary_terms or expansion_terms)
        scanned_count = 0
        scan_truncated = False
        primary_hit_count = 0
        expansion_extra_count = 0
        context_pool: List[MessageRecord] | None = None
        scope_lines: List[str] = ["———— 检索说明 ————"]
        term_line = f"关键词（原词）：{primary_display or '（无）'}｜扩展：{expansion_display or '（无）'}"
        scope_lines.append(term_line)
        if use_index:
            # 关键词直查本地索引；索引未覆盖的更早时段先分页扫描补齐
            coverage = await self._store.index_coverage(group_id)
            indexed_from = coverage[0] if coverage else None
            need_scan = indexed_from is None or indexed_from > start_time
            if need_scan:
                scanned_until = await self._store.get_scanned_until(group_id)
                if scanned_until is not None and scanned_until <= start_time:
                    # 这个范围之前已完整扫描过且宿主没有更早历史，不再重复扫描
                    need_scan = False
            if need_scan:
                gap_end = min(indexed_from, end_time) if indexed_from else end_time
                gap_messages, scanned_count, scan_truncated = await self._fetch_group_window(
                    group_stream_id, start_time, gap_end
                )
                if gap_messages:
                    await self._store.index_messages(gap_messages)
                    coverage = await self._store.index_coverage(group_id)
                if not scan_truncated:
                    # 扫描自然结束（到达请求起点或宿主历史边界），记录水位
                    await self._store.set_scanned_until(group_id, start_time)
            primary_hits = await self._index_keyword_hits(
                group_id, primary_terms, start_time, end_time, _INDEX_FETCH_LIMIT
            )
            primary_hit_count = len(primary_hits)
            primary_ids = {message.message_id for message in primary_hits}
            expansion_extra = [
                message
                for message in await self._index_keyword_hits(
                    group_id, expansion_terms, start_time, end_time, _INDEX_FETCH_LIMIT
                )
                if message.message_id not in primary_ids
            ]
            expansion_extra_count = len(expansion_extra)
            messages = sorted(
                [*primary_hits, *expansion_extra], key=lambda item: (item.timestamp, item.message_id)
            )
            if not messages:
                # 关键词无直接命中时取范围内最新消息，交给语义重排兜底
                messages = await self._store.search_index(
                    group_id, [], start_time, end_time, config.retrieval.lexical_candidates
                )
            # 无论有没有扩展词都完整显示两个数值，便于对照判断
            scope_lines.append(
                f"索引命中：原词 {primary_hit_count} 条，扩展额外 {expansion_extra_count} 条"
            )
            if coverage:
                scope_lines.append(f"索引覆盖：{_format_time(coverage[0])} 至 {_format_time(coverage[1])}")
            if scanned_count:
                scope_lines.append(f"本次共扫描 {scanned_count} 条消息补齐更早索引")
        else:
            messages, scanned_count, scan_truncated = await self._fetch_group_window(
                group_stream_id, start_time, end_time
            )
            scan_line = f"共扫描 {scanned_count} 条消息"
            if messages:
                scan_line += (
                    f"，其中文本消息 {len(messages)} 条"
                    f"（{_format_time(messages[0].timestamp)} 至 {_format_time(messages[-1].timestamp)}）"
                )
            scope_lines.append(scan_line)
            context_pool = messages
        if scan_truncated:
            scope_lines.append("已达扫描上限，更早的消息未纳入本次检索；可在插件设置中调大“扫描消息上限”")
        scope_note = "\n\n" + "\n".join(scope_lines)
        if not messages:
            return f"这个群最近 {plan.history_days} 天没有可供查询的文本消息。{scope_note}"

        if plan.excluded_terms:
            messages = [
                message
                for message in messages
                if not any(normalize_text(term) in normalize_text(message.text) for term in plan.excluded_terms)
            ]
        if not messages:
            return f"排除“{'、'.join(plan.excluded_terms)}”后，这个群没有剩余可查询的文本消息。{scope_note}"
        if context_pool is not None:
            context_pool = messages

        # 先对全部候选评分，再截断，避免时间靠前的完整讨论被提前丢弃
        candidates = rank_lexically(retrieval_query, messages, config.retrieval.lexical_candidates)
        seed_limit = max(
            3,
            config.retrieval.evidence_messages // max(1, config.retrieval.context_radius + 1),
        )
        selected: List[MessageRecord] = []
        embedding_succeeded = False
        if config.retrieval.use_embeddings:
            try:
                reranked = await rerank_with_embeddings(
                    retrieval_query,
                    candidates,
                    self._embed_texts,
                    seed_limit * 3,
                )
                # 按时间片段分散选证据，避免全部证据挤在同一段对话里
                selected = select_diverse_seeds(reranked, seed_limit)
                embedding_succeeded = True
            except Exception as exc:
                self.ctx.logger.warning("嵌入语义重排失败，保留本地关键词结果：%s", exc)
        if not embedding_succeeded:
            ordered = [message for message, score in candidates if score > 0]
            selected = select_diverse_seeds(ordered, seed_limit)
        if context_pool is not None:
            evidence = expand_context(
                context_pool,
                selected,
                config.retrieval.context_radius,
                config.retrieval.evidence_messages,
            )
        else:
            evidence = await self._expand_context_from_index(
                group_id,
                selected,
                config.retrieval.context_radius,
                config.retrieval.evidence_messages,
                plan.excluded_terms,
            )
        group_name = evidence[0].group_name if evidence else f"群聊{group_id}"
        search_results = await self._search_external(plan.search_query)
        if not evidence and not search_results:
            return f"没有在“{group_name}”最近 {plan.history_days} 天的记录中找到足够相关的证据。{scope_note}{plan_note}"

        prompt = build_answer_prompt(original_query, group_name, evidence, search_results)
        result = await self._generate(prompt, self._config().models.verify_task, max_tokens=2400)
        if result.get("success") and str(result.get("response") or "").strip():
            answer = attach_evidence_footer(str(result["response"]).strip(), evidence)
        else:
            answer = fallback_history_answer(original_query, group_name, evidence)
        if search_results:
            answer += "\n\n外部资料链接：\n" + "\n".join(
                f"- {item.title}：{item.url}" for item in search_results
            )
        return (answer + scope_note + plan_note)[:14000]

    async def _index_keyword_hits(
        self,
        group_id: str,
        terms: Sequence[str],
        start_time: float,
        end_time: float,
        limit: int,
    ) -> List[MessageRecord]:
        """索引直查加独立词过滤：LIKE 只保证子串出现，这里过滤掉英文词
        混在其他字母数字串（网址、编号、chatglm 等）里的误命中。"""

        if not terms:
            return []
        raw_hits = await self._store.search_index(group_id, terms, start_time, end_time, limit)
        return [message for message in raw_hits if matches_any_term(normalize_text(message.text), terms)]

    async def _expand_context_from_index(
        self,
        group_id: str,
        selected: Sequence[MessageRecord],
        radius: int,
        limit: int,
        excluded_terms: Sequence[str] = (),
    ) -> List[MessageRecord]:
        """从本地索引取每条命中消息前后的对话，组装按时间排序的证据。"""

        pool: Dict[str, MessageRecord] = {}
        for seed in selected:
            for neighbor in await self._store.index_neighbors(group_id, seed.timestamp, radius):
                if any(normalize_text(term) in normalize_text(neighbor.text) for term in excluded_terms):
                    continue
                key = neighbor.message_id or f"{neighbor.timestamp}:{neighbor.user_id}"
                pool[key] = neighbor
        ordered = sorted(pool.values(), key=lambda item: (item.timestamp, item.message_id))
        return expand_context(ordered, selected, radius, limit)

    async def _fetch_group_window(
        self, stream_id: str, start_time: float, end_time: float
    ) -> Tuple[List[MessageRecord], int, bool]:
        """从最新往回分页读取整个时间窗。

        单次“最新 N 条”读取在高流量群中只能覆盖几个小时，更早的时间段会静默缺失。
        返回（文本消息升序、累计扫描条数、是否因扫描上限截断）。
        """

        config = self._config().retrieval
        page_limit = config.max_history_messages
        scan_limit = config.scan_messages
        collected: Dict[str, MessageRecord] = {}
        scanned = 0
        current_end = end_time
        truncated = False
        # 页数硬上限只是防御宿主异常返回导致的死循环，正常由扫描上限终止。
        for _page in range(1000):
            raw = await self.ctx.message.get_by_time_in_chat(
                stream_id,
                str(start_time),
                str(current_end),
                limit=page_limit,
                limit_mode="latest",
                filter_mai=True,
                filter_command=True,
            )
            page_count = raw_message_count(raw)
            if page_count == 0:
                break
            scanned += page_count
            for record in normalize_messages(raw):
                key = record.message_id or f"{record.timestamp}:{record.user_id}:{record.text[:40]}"
                collected[key] = record
            earliest = earliest_raw_timestamp(raw)
            if page_count < page_limit or earliest is None or earliest <= start_time:
                break
            if scanned >= scan_limit:
                truncated = True
                break
            current_end = earliest - 0.001
        messages = sorted(collected.values(), key=lambda item: (item.timestamp, item.message_id))
        return messages, scanned, truncated

    async def _semantic_verify(self, rule: CompositeRule, evidence: Sequence[MessageRecord]) -> bool:
        prompt = build_semantic_verify_prompt(rule, evidence)
        result = await self._generate(prompt, self._config().models.verify_task, max_tokens=300)
        if not result.get("success"):
            self.ctx.logger.warning("规则 %s 的语义复核失败，本次不提醒", rule.id)
            return False
        try:
            payload = extract_json_object(str(result.get("response") or ""))
        except IntentParseError as exc:
            self.ctx.logger.warning("规则 %s 的语义复核结果无效，本次不提醒：%s", rule.id, exc)
            return False
        return payload.get("relevant") is True

    async def _search_external(self, query: str) -> List[SearchResult]:
        if self._search_client is None:
            return []
        try:
            return await self._search_client.search(query)
        except SearchError as exc:
            self.ctx.logger.warning("外部资料查询失败，不影响群聊检索：%s", exc)
            return []

    async def _notify_users(self, owner_user_id: str, text: str) -> None:
        recipients = set(self._notification_user_ids)
        if owner_user_id:
            recipients.add(owner_user_id)
        for user_id in sorted(recipients):
            try:
                session = await self.ctx.chat.open_session(
                    platform="qq",
                    chat_type="private",
                    user_id=user_id,
                )
                stream_id = resolve_stream_id(session)
                if not stream_id:
                    raise RuntimeError("没有获得私聊聊天流")
                sent = await self.ctx.send.text(text[:14000], stream_id)
                if not sent:
                    raise RuntimeError("发送能力返回失败")
            except Exception as exc:
                self.ctx.logger.error("向用户 %s 发送观察提醒失败：%s", user_id, exc)

    async def _query_from_reply(self, kwargs: Mapping[str, Any]) -> str:
        message = kwargs.get("message")
        if not isinstance(message, Mapping):
            return ""
        reply_to = str(message.get("reply_to") or "").strip()
        if not reply_to:
            return ""
        current_stream = str(message.get("session_id") or kwargs.get("stream_id") or "").strip()
        replied = await self.ctx.message.get_by_id(reply_to, stream_id=current_stream)
        return extract_payload_text(replied)

    async def _set_rule_state(self, kwargs: Mapping[str, Any], enabled: bool) -> Tuple[bool, str, int]:
        user_id, stream_id = extract_command_identity(kwargs)
        denied = self._command_denied_reason(user_id)
        if denied:
            return await self._reply(stream_id, denied, success=False)
        groups = kwargs.get("matched_groups") or {}
        rule_id = str(groups.get("rule_id") or "").lower()
        changed = await self._store.set_rule_enabled(rule_id, enabled, owner_user_id=user_id)
        if changed:
            await self._refresh_rules()
        action = "恢复" if enabled else "暂停"
        text = f"已{action}规则 {rule_id}。" if changed else "没有找到属于你的这条规则。"
        return await self._reply(stream_id, text, success=changed)

    async def _intent_generate(self, prompt: str) -> Dict[str, Any]:
        return await self._generate(prompt, self._config().models.intent_task, max_tokens=1200)

    async def _embed_texts(self, texts: List[str]) -> Dict[str, Any]:
        rpc_timeout_ms = self._config().models.llm_timeout_seconds * 1000
        async with self._model_semaphore:
            return await self.ctx.llm.embed(
                texts=texts,
                task_name=self._config().models.embedding_task,
                max_concurrent=4,
                # 双通道设置 RPC 等待时间：timeout_ms 由 SDK 客户端消费，
                # rpc_timeout_ms 随能力参数传给宿主运行器；否则默认 30 秒
                # 会中断慢渠道的在途请求。
                timeout_ms=rpc_timeout_ms,
                rpc_timeout_ms=rpc_timeout_ms,
            )

    async def _generate(self, prompt: str, task_name: str, max_tokens: int) -> Dict[str, Any]:
        rpc_timeout_ms = self._config().models.llm_timeout_seconds * 1000
        async with self._model_semaphore:
            return await self.ctx.llm.generate(
                prompt=prompt,
                model=task_name.strip() or "utils",
                temperature=self._config().models.temperature,
                max_tokens=max_tokens,
                timeout_ms=rpc_timeout_ms,
                rpc_timeout_ms=rpc_timeout_ms,
            )

    async def _refresh_runtime_state(self, *, rebuild_engine: bool) -> None:
        config = self._config()
        self._admin_user_ids = _normalize_numeric_ids(config.access.admin_user_ids)
        self._allowed_group_ids = _normalize_numeric_ids(config.access.allowed_group_ids)
        self._notification_user_ids = _normalize_numeric_ids(config.access.notification_user_ids)
        new_buffer_limit = config.monitoring.max_buffer_messages
        if rebuild_engine or new_buffer_limit != self._buffer_limit:
            self._engine = CompositeRuleEngine(max_messages_per_group=new_buffer_limit)
            self._buffer_limit = new_buffer_limit
        await self._refresh_rules()
        self._search_client = None
        if config.search.enabled and config.search.endpoint.strip():
            self._search_client = JsonSearchClient(
                SearchSettings(
                    endpoint=config.search.endpoint,
                    api_key=config.search.api_key,
                    authorization_header=config.search.authorization_header,
                    authorization_prefix=config.search.authorization_prefix,
                    query_parameter=config.search.query_parameter,
                    results_path=config.search.results_path,
                    title_field=config.search.title_field,
                    url_field=config.search.url_field,
                    snippet_field=config.search.snippet_field,
                    date_field=config.search.date_field,
                    timeout_seconds=config.search.timeout_seconds,
                    max_results=config.search.max_results,
                )
            )
        try:
            removed = await self._store.prune_index(
                sorted(self._allowed_group_ids), config.retrieval.index_retention_days
            )
            if removed:
                self.ctx.logger.info("已清理 %d 条过期或移出白名单群的索引消息", removed)
        except Exception as exc:
            self.ctx.logger.warning("清理本地消息索引失败：%s", exc)
        if not self._admin_user_ids:
            self.ctx.logger.warning("管理员 QQ 名单为空，寻迹和监控管理命令将拒绝执行")
        if not self._allowed_group_ids:
            self.ctx.logger.warning("允许访问的群聊名单为空，插件不会读取或观察任何群")

    async def _refresh_rules(self) -> None:
        rules = await self._store.list_rules()
        valid_rules: List[CompositeRule] = []
        for rule in rules:
            try:
                self._engine.validate(rule)
            except RuleValidationError as exc:
                self.ctx.logger.error("规则 %s 无效，已跳过：%s", rule.id, exc)
                continue
            valid_rules.append(rule)
        self._rules = valid_rules

    def _config(self) -> GroupTraceConfig:
        return cast(GroupTraceConfig, self.config)

    def _command_denied_reason(self, user_id: str) -> str:
        if not self._config().plugin.enabled:
            return "麦麦群聊寻迹当前已在 WebUI 中关闭。"
        if not user_id or user_id not in self._admin_user_ids:
            return "你不在麦麦群聊寻迹的管理员名单中，不能执行这个操作。"
        return ""

    def _is_group_allowed(self, group_id: str) -> bool:
        return bool(group_id and group_id in self._allowed_group_ids)

    async def _reply(self, stream_id: str, text: str, *, success: bool) -> Tuple[bool, str, int]:
        if not stream_id:
            return False, text, 1
        sent = await self.ctx.send.text(text[:14000], stream_id)
        return success and sent, text, 2 if success else 1


def _normalize_numeric_ids(values: Sequence[str]) -> Set[str]:
    return {str(value).strip() for value in values if str(value).strip().isdigit()}


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def _display_terms(terms: Sequence[str]) -> str:
    """展示检索词时只保留最长的词，隐藏它们的滑窗子片段。"""

    maximal = [term for term in terms if not any(term != other and term in other for other in terms)]
    return "、".join(maximal[:8])


def create_plugin() -> GroupTracePlugin:
    """MaiBot Host 的标准插件工厂入口。"""

    return GroupTracePlugin()
