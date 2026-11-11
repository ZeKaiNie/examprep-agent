from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from kaopu_agent import Agent
from kaopu_llm import Gateway
from kaopu_llm.testing import FakeClock, FakeProvider, ScriptItem


def _gateway(fake: FakeProvider) -> Gateway:
    return Gateway({fake.name: fake}, {"tutor": [f"{fake.name}/m"]}, clock=FakeClock())


@pytest.fixture
def make_gateway() -> Callable[[FakeProvider], Gateway]:
    return _gateway


@pytest.fixture
def make_agent() -> Callable[..., tuple[Agent, FakeProvider]]:
    def make(script: Sequence[ScriptItem] = (), **kw: Any) -> tuple[Agent, FakeProvider]:
        fake = FakeProvider(script)
        kw.setdefault("role", "tutor")
        return Agent(_gateway(fake), **kw), fake

    return make
