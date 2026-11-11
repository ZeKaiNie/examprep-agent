"""provider 配置与内置预设。"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .registry import ProtocolName


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    protocol: ProtocolName
    base_url: str = ""
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    timeout: float = 120.0  # 读超时：两次数据之间的最长间隔
    connect_timeout: float = 10.0
    rpm: int | None = None  # 每分钟请求数上限
    max_concurrency: int | None = None
    # Anthropic / Gemini 版本等协议级选项
    options: dict[str, Any] = Field(default_factory=dict[str, Any])

    def resolve_api_key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        return None


def _p(
    name: str, protocol: ProtocolName, base_url: str, key_env: str | None, **options: Any
) -> ProviderConfig:
    return ProviderConfig(
        name=name, protocol=protocol, base_url=base_url, api_key_env=key_env, options=options
    )


PROVIDER_PRESETS: dict[str, ProviderConfig] = {
    p.name: p
    for p in (
        _p("openai", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        # DeepSeek 思考模式默认开启；请求带 tools 时必须回传历史 reasoning_content，否则可能返回 400
        _p("deepseek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", send_reasoning=True),
        _p("qwen", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
        _p("glm", "openai", "https://open.bigmodel.cn/api/paas/v4", "ZHIPUAI_API_KEY"),
        _p("kimi", "openai", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
        _p("siliconflow", "openai", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
        _p("openrouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        _p("ollama", "openai", "http://localhost:11434/v1", None),
        _p("lmstudio", "openai", "http://localhost:1234/v1", None),
        _p("vllm", "openai", "http://localhost:8000/v1", None),
        _p("llamacpp", "openai", "http://localhost:8080/v1", None),
        _p("anthropic", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
        _p("gemini", "gemini", "https://generativelanguage.googleapis.com", "GEMINI_API_KEY"),
    )
}


def provider_config(name: str, **overrides: Any) -> ProviderConfig:
    """取预设并覆盖部分字段；不在预设里的名字必须给出 protocol 和 base_url。"""
    base = PROVIDER_PRESETS.get(name)
    data: dict[str, Any] = base.model_dump() if base is not None else {"name": name}
    if base is not None and base.api_key is not None:
        data["api_key"] = base.api_key
    extra_options = overrides.pop("options", None)
    data.update({k: v for k, v in overrides.items() if v is not None})
    if extra_options:  # 协议选项与预设合并，而不是整体替换
        data["options"] = {**data.get("options", {}), **extra_options}
    data["name"] = name
    return ProviderConfig.model_validate(data)
