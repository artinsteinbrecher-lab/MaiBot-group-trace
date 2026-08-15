"""可选的通用 JSON 外部查询适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import asyncio
import json

from .models import SearchResult


@dataclass(frozen=True, slots=True)
class SearchSettings:
    endpoint: str
    api_key: str = ""
    authorization_header: str = "Authorization"
    authorization_prefix: str = "Bearer "
    query_parameter: str = "q"
    results_path: str = "results"
    title_field: str = "title"
    url_field: str = "url"
    snippet_field: str = "snippet"
    date_field: str = "published_at"
    timeout_seconds: int = 15
    max_results: int = 5


class SearchError(RuntimeError):
    """外部查询配置或请求失败。"""


class JsonSearchClient:
    """不绑定供应商的只读 JSON GET 查询客户端。"""

    _MAX_RESPONSE_BYTES = 2_000_000

    def __init__(self, settings: SearchSettings) -> None:
        self._settings = settings

    async def search(self, query: str) -> List[SearchResult]:
        return await asyncio.to_thread(self._search_sync, query)

    def _search_sync(self, query: str) -> List[SearchResult]:
        url = self._build_url(query)
        headers = {"Accept": "application/json", "User-Agent": "MaiBot-group-trace/0.1.0"}
        if self._settings.api_key:
            header_name = self._settings.authorization_header.strip()
            if not header_name or any(char in header_name for char in "\r\n:"):
                raise SearchError("外部查询密钥请求头名称不合法")
            headers[header_name] = self._settings.authorization_prefix + self._settings.api_key
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:
                body = response.read(self._MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise SearchError(f"外部查询返回 HTTP {exc.code}") from exc
        except URLError as exc:
            raise SearchError(f"外部查询连接失败：{exc.reason}") from exc
        if len(body) > self._MAX_RESPONSE_BYTES:
            raise SearchError("外部查询响应超过 2MB 安全上限")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearchError("外部查询没有返回有效 UTF-8 JSON") from exc
        values = _read_path(payload, self._settings.results_path)
        if not isinstance(values, list):
            raise SearchError("外部查询结果路径没有指向数组")
        results: List[SearchResult] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            title = str(value.get(self._settings.title_field) or "").strip()
            result_url = str(value.get(self._settings.url_field) or "").strip()
            if not title or not _is_http_url(result_url):
                continue
            results.append(
                SearchResult(
                    title=title[:300],
                    url=result_url,
                    snippet=str(value.get(self._settings.snippet_field) or "").strip()[:1000],
                    published_at=str(value.get(self._settings.date_field) or "").strip()[:100],
                    source=urlsplit(result_url).hostname or "",
                )
            )
            if len(results) >= self._settings.max_results:
                break
        return results

    def _build_url(self, query: str) -> str:
        endpoint = self._settings.endpoint.strip()
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
            raise SearchError("外部查询地址必须是没有内嵌账号密码的 HTTP/HTTPS URL")
        if "{query}" in endpoint:
            return endpoint.replace("{query}", quote(query, safe=""))
        parameter = self._settings.query_parameter.strip()
        if not parameter:
            raise SearchError("外部查询参数名称不能为空")
        query_values = parse_qsl(parts.query, keep_blank_values=True)
        query_values.append((parameter, query))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_values), parts.fragment))


def _read_path(payload: Any, path: str) -> Any:
    current = payload
    for part in (item for item in path.split(".") if item):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and bool(parts.netloc) and not parts.username and not parts.password
