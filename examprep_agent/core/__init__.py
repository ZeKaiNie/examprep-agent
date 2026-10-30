# -*- coding: utf-8 -*-
"""Agent kernel: providers, the plan–act–observe loop, context engine and sessions."""

from .context import CompactionResult, ContextBudget, ContextEngine, estimate_tokens
from .loop import EventLogger, LoopLimits, LoopResult, Tool, ToolRegistry, run_agent
from .provider import (
    ChatMessage,
    FakeProvider,
    ModelProvider,
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
    provider_from_env,
)
from .session import Session, SessionCheckpoint

__all__ = [
    "ChatMessage",
    "CompactionResult",
    "ContextBudget",
    "ContextEngine",
    "EventLogger",
    "FakeProvider",
    "LoopLimits",
    "LoopResult",
    "ModelProvider",
    "ModelResponse",
    "OpenAICompatibleProvider",
    "ProviderConfig",
    "ProviderError",
    "Session",
    "SessionCheckpoint",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "Usage",
    "estimate_tokens",
    "provider_from_env",
    "run_agent",
]
