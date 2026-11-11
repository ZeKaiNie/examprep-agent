from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaopu_llm.testing import sse_response

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Recorder:
    """MockTransport：按顺序返回预设响应，并记录请求。"""

    def __init__(self, responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        return item(request) if callable(item) else item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    return load_fixture


@pytest.fixture
def replay() -> Callable[..., Recorder]:
    def make(*items: str | httpx.Response, chunk_size: int = 0) -> Recorder:
        responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]] = []
        for it in items:
            if isinstance(it, httpx.Response):
                responses.append(it)
            else:
                text = load_fixture(it) if it.endswith(".sse") else it
                responses.append(sse_response(text, chunk_size=chunk_size))
        return Recorder(responses)

    return make
