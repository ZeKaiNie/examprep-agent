"""模型能力表与成本计算（R4、R2.3）。

查找顺序：用户覆盖 > 内置条目 > 保守默认值。内置价格只是参考，可能过时，以用户配置为准。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .types import Usage

ProtocolName = Literal["openai", "anthropic", "gemini", "fake"]

# DeepSeek 条目于 2026-11 按官方文档核对；其他厂商仍是较早记录的参考价，使用前请自行核对
PRICING_AS_OF = "2026-11"
LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "vllm", "llamacpp"})


class Pricing(BaseModel):
    """每百万 token 的价格。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float
    output: float
    cache_read: float | None = None  # None：按 input 价格计
    cache_write: float | None = None
    currency: str = "USD"


FREE = Pricing(input=0.0, output=0.0, cache_read=0.0, cache_write=0.0)


class Cost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    currency: str = "USD"

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_read + self.cache_write


def compute_cost(usage: Usage, pricing: Pricing | None) -> Cost | None:
    if pricing is None:
        return None
    m = 1_000_000
    cr = pricing.cache_read if pricing.cache_read is not None else pricing.input
    cw = pricing.cache_write if pricing.cache_write is not None else pricing.input
    return Cost(
        input=usage.input_tokens * pricing.input / m,
        output=usage.output_tokens * pricing.output / m,
        cache_read=usage.cache_read_tokens * cr / m,
        cache_write=usage.cache_write_tokens * cw / m,
        currency=pricing.currency,
    )


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    provider: str
    protocol: ProtocolName
    context_window: int = 8192
    max_output: int = 2048
    supports_tools: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_prompt_cache: bool = False
    pricing: Pricing | None = None

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"


def parse_model_ref(ref: str) -> tuple[str, str]:
    """``"provider/model"`` → (provider, model)。只按第一个 ``/`` 切分（OpenRouter 模型名含 ``/``）。"""
    provider, sep, model = ref.partition("/")
    if not sep or not provider or not model:
        raise ValueError(f"模型引用应写成 'provider/model'：{ref!r}")
    return provider, model


def _m(
    provider: str,
    protocol: ProtocolName,
    id: str,
    ctx: int,
    out: int,
    *,
    vision: bool = False,
    reasoning: bool = False,
    cache: bool = False,
    pricing: Pricing | None = None,
) -> ModelInfo:
    return ModelInfo(
        id=id,
        provider=provider,
        protocol=protocol,
        context_window=ctx,
        max_output=out,
        supports_tools=True,
        supports_vision=vision,
        supports_reasoning=reasoning,
        supports_prompt_cache=cache,
        pricing=pricing,
    )


def _usd(i: float, o: float, cr: float | None = None, cw: float | None = None) -> Pricing:
    return Pricing(input=i, output=o, cache_read=cr, cache_write=cw)


def _cny(i: float, o: float, cr: float | None = None) -> Pricing:
    return Pricing(input=i, output=o, cache_read=cr, currency="CNY")


BUILTIN_MODELS: tuple[ModelInfo, ...] = (
    # OpenAI
    _m(
        "openai",
        "openai",
        "gpt-4o-mini",
        128_000,
        16_384,
        vision=True,
        cache=True,
        pricing=_usd(0.15, 0.6, 0.075),
    ),
    _m(
        "openai", "openai", "gpt-4.1", 1_047_576, 32_768, vision=True, cache=True, pricing=_usd(2.0, 8.0, 0.5)
    ),
    _m(
        "openai",
        "openai",
        "gpt-4.1-mini",
        1_047_576,
        32_768,
        vision=True,
        cache=True,
        pricing=_usd(0.4, 1.6, 0.1),
    ),
    _m(
        "openai",
        "openai",
        "gpt-5",
        400_000,
        128_000,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(1.25, 10.0, 0.125),
    ),
    _m(
        "openai",
        "openai",
        "gpt-5-mini",
        400_000,
        128_000,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(0.25, 2.0, 0.025),
    ),
    # DeepSeek（2026-11 官方文档；按高峰价记，非高峰为半价。deepseek-chat / deepseek-reasoner 已于 2026-07 下线）
    _m(
        "deepseek",
        "openai",
        "deepseek-flash",  # DeepSeek-V4.1-Flash
        1_048_576,
        393_216,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(0.3, 1.2, 0.006),
    ),
    _m(
        "deepseek",
        "openai",
        "deepseek-v4-pro",
        1_048_576,
        393_216,
        reasoning=True,
        cache=True,
        pricing=_usd(1.32, 3.96, 0.044),
    ),
    # 通义千问（DashScope 兼容模式）
    _m("qwen", "openai", "qwen-plus", 131_072, 8_192, reasoning=True, pricing=_cny(0.8, 2.0)),
    _m("qwen", "openai", "qwen-max", 32_768, 8_192),
    # 智谱、Kimi（价格未内置）
    _m("glm", "openai", "glm-4.6", 200_000, 128_000, reasoning=True, cache=True),
    _m("kimi", "openai", "kimi-k2-0905-preview", 262_144, 16_384, cache=True),
    # Anthropic
    _m(
        "anthropic",
        "anthropic",
        "claude-sonnet-4-5",
        200_000,
        64_000,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(3.0, 15.0, 0.3, 3.75),
    ),
    _m(
        "anthropic",
        "anthropic",
        "claude-haiku-4-5",
        200_000,
        64_000,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(1.0, 5.0, 0.1, 1.25),
    ),
    _m(
        "anthropic",
        "anthropic",
        "claude-opus-4-1",
        200_000,
        32_000,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(15.0, 75.0, 1.5, 18.75),
    ),
    # Gemini
    _m(
        "gemini",
        "gemini",
        "gemini-2.5-pro",
        1_048_576,
        65_536,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(1.25, 10.0, 0.31),
    ),
    _m(
        "gemini",
        "gemini",
        "gemini-2.5-flash",
        1_048_576,
        65_536,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(0.3, 2.5, 0.075),
    ),
    _m(
        "gemini",
        "gemini",
        "gemini-2.5-flash-lite",
        1_048_576,
        65_536,
        vision=True,
        reasoning=True,
        cache=True,
        pricing=_usd(0.1, 0.4, 0.025),
    ),
)


class ModelRegistry:
    def __init__(self, overrides: Iterable[ModelInfo] = (), *, include_builtin: bool = True) -> None:
        self._builtin: dict[tuple[str, str], ModelInfo] = (
            {(m.provider, m.id): m for m in BUILTIN_MODELS} if include_builtin else {}
        )
        self._overrides: dict[tuple[str, str], ModelInfo] = {}
        for m in overrides:
            self.register(m)

    def register(self, info: ModelInfo) -> None:
        self._overrides[(info.provider, info.id)] = info

    def known(self) -> list[ModelInfo]:
        merged = {**self._builtin, **self._overrides}
        return list(merged.values())

    def get(self, provider: str, model_id: str, protocol: ProtocolName) -> ModelInfo:
        key = (provider, model_id)
        if key in self._overrides:
            return self._overrides[key]
        if key in self._builtin:
            return self._builtin[key]
        return ModelInfo(
            id=model_id,
            provider=provider,
            protocol=protocol,
            pricing=FREE if provider in LOCAL_PROVIDERS or protocol == "fake" else None,
        )
