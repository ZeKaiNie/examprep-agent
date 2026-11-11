"""模型配置（R7.2）：数据目录下的 ``config.toml``，或只设一个 API Key 环境变量。

config.toml 示例：

    [providers.deepseek]            # 与内置预设合并；api_key 也可以只放在环境变量 DEEPSEEK_API_KEY
    [roles]
    tutor = ["deepseek/deepseek-flash"]
    grader = ["deepseek/deepseek-flash"]
    verifier = ["deepseek/deepseek-flash"]
    [reasoning]                     # 思考强度：off / low / medium / high，默认都是 high
    tutor = "high"                  # 讲解：关掉更快更省，但自编例子更容易算错

单次学习也可以用 ``examprep study --reasoning low`` 只改讲解的思考强度。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from examprep_llm import GatewayConfig

CONFIG_FILE = "config.toml"
ROLES = ("tutor", "grader", "verifier")
REASONING_LEVELS = ("off", "low", "medium", "high")
# 默认都开思考（high）：讲解里的例子要算对、判分要准，正确率优先于速度和花费
DEFAULT_REASONING: dict[str, str] = dict.fromkeys(ROLES, "high")

# (环境变量, provider, tutor 模型, 便宜模型)
_ENV_DEFAULTS: tuple[tuple[str, str, str, str], ...] = (
    ("DEEPSEEK_API_KEY", "deepseek", "deepseek-flash", "deepseek-flash"),
    ("OPENAI_API_KEY", "openai", "gpt-5-mini", "gpt-4.1-mini"),
    ("ANTHROPIC_API_KEY", "anthropic", "claude-sonnet-4-5", "claude-haiku-4-5"),
    ("GEMINI_API_KEY", "gemini", "gemini-2.5-flash", "gemini-2.5-flash-lite"),
)
OLLAMA_ENV = "EXAMPREP_OLLAMA_MODEL"

HELP = (
    "还没有配置模型。任选一种：\n"
    "  1. 设置环境变量 DEEPSEEK_API_KEY（或 OPENAI_API_KEY、ANTHROPIC_API_KEY、GEMINI_API_KEY）\n"
    "  2. 本地 Ollama：设置 EXAMPREP_OLLAMA_MODEL=qwen3:8b（需支持工具调用）\n"
    "  3. 在数据目录写 config.toml，见 examprep/settings.py 开头的示例"
)


class SettingsError(RuntimeError):
    pass


def load_settings(data_dir: str | Path, env: Mapping[str, str] | None = None) -> GatewayConfig:
    environ = os.environ if env is None else env
    path = Path(data_dir) / CONFIG_FILE
    if path.is_file():
        try:
            data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"{path} 格式错误：{exc}") from exc
        roles: dict[str, Any] = dict(data.get("roles") or {})
        tutor = roles.get("tutor")
        if not tutor:
            raise SettingsError(f"{path} 缺少 [roles] tutor")
        for role in ROLES:
            roles.setdefault(role, tutor)
        data["roles"] = roles
        data["reasoning"] = {**DEFAULT_REASONING, **dict(data.get("reasoning") or {})}
        try:
            return GatewayConfig.model_validate(data)
        except ValueError as exc:
            raise SettingsError(f"{path} 配置无效：{exc}") from exc
    for var, provider, strong, cheap in _ENV_DEFAULTS:
        if environ.get(var):
            return GatewayConfig.model_validate(
                {
                    "providers": {provider: {}},
                    "roles": {
                        "tutor": [f"{provider}/{strong}"],
                        "grader": [f"{provider}/{cheap}"],
                        "verifier": [f"{provider}/{cheap}"],
                    },
                    "reasoning": DEFAULT_REASONING,
                }
            )
    model = environ.get(OLLAMA_ENV)
    if model:
        ref = f"ollama/{model}"
        # 本地模型由服务端决定是否思考，不发送思考参数
        return GatewayConfig.model_validate({"providers": {"ollama": {}}, "roles": {r: [ref] for r in ROLES}})
    raise SettingsError(HELP)
