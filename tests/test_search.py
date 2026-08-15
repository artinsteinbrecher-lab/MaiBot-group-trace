from __future__ import annotations

import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from core.search import JsonSearchClient, SearchError, SearchSettings, _read_path


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class SearchClientTests(TestCase):
    def test_query_parameter_is_encoded(self) -> None:
        client = JsonSearchClient(SearchSettings(endpoint="https://example.com/search", query_parameter="q"))
        url = client._build_url("DSV4F 输出")
        self.assertIn("q=DSV4F+%E8%BE%93%E5%87%BA", url)

    def test_placeholder_is_encoded(self) -> None:
        client = JsonSearchClient(SearchSettings(endpoint="https://example.com/search/{query}"))
        self.assertTrue(client._build_url("a b").endswith("/a%20b"))

    def test_embedded_credentials_are_rejected(self) -> None:
        client = JsonSearchClient(SearchSettings(endpoint="https://user:pass@example.com/search"))
        with self.assertRaises(SearchError):
            client._build_url("test")

    def test_result_path(self) -> None:
        self.assertEqual(_read_path({"data": {"results": [1]}}, "data.results"), [1])

    def test_response_is_parsed_and_unsafe_results_are_skipped(self) -> None:
        settings = SearchSettings(
            endpoint="https://example.com/search",
            results_path="data.items",
            max_results=2,
        )
        client = JsonSearchClient(settings)
        payload = {
            "data": {
                "items": [
                    {
                        "title": "有效结果",
                        "url": "https://news.example.org/story",
                        "snippet": "匹配到的摘要",
                        "published_at": "2026-08-15",
                    },
                    {"title": "危险地址", "url": "https://user:pass@example.org/private"},
                    {"title": "缺少地址"},
                ]
            }
        }
        with patch("core.search.urlopen", return_value=_FakeResponse(payload)) as mocked_urlopen:
            results = client._search_sync("复合关键词")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "有效结果")
        self.assertEqual(results[0].source, "news.example.org")
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("q=%E5%A4%8D%E5%90%88%E5%85%B3%E9%94%AE%E8%AF%8D", request.full_url)
