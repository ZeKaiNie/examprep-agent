"""配置（R7.2）：数据目录下的 ``config.toml``，或只设一个 API Key 环境变量。

config.toml 示例：

    [providers.deepseek]            # 与内置预设合并；api_key 也可以只放在环境变量 DEEPSEEK_API_KEY
    [roles]
    tutor = ["deepseek/deepseek-flash"]
    grader = ["deepseek/deepseek-flash"]
    verifier = ["deepseek/deepseek-flash"]
    [reasoning]                     # 思考强度：off / low / medium / high，默认都是 high
    tutor = "high"                  # 讲解：关掉更快更省，但自编例子更容易算错
    [compaction]                    # 上下文压缩，默认开启
    auto = true                     # 对话太长时自动压缩；false 时到上限直接开新对话（只保留数据库里的进度）
    ratio = 0.7                     # 占到模型窗口的 70% 时压缩
    max_tokens = 60000              # 不管窗口多大，超过这么多 token 就压缩（大窗口时 70% 太晚）

单次学习也可以用 ``examprep study --reasoning low`` 只改讲解的思考强度，``--no-auto-compact`` 关闭自动压缩；
学习时说“压缩上下文”随时手动压缩。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from examprep_llm import GatewayConfig

CONFIG_FILE = "config.toml"
SESSIONS_DB = "sessions.db"  # 数据目录下的会话事件库（对话、压缩分段、界面对话记录）
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


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """上下文压缩策略。阈值取“窗口 × ratio”和 max_tokens 中较小的一个。"""

    auto: bool = True
    ratio: float = 0.7
    max_tokens: int = 60_000

    def __post_init__(self) -> None:
        if not 0.1 <= self.ratio <= 0.95:
            raise ValueError("ratio 要在 0.1 到 0.95 之间")
        if self.max_tokens < 2_000:
            raise ValueError("max_tokens 至少 2000")

    def threshold(self, context_window: int) -> int:
        return min(int(context_window * self.ratio), self.max_tokens)


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} 格式错误：{exc}") from exc


def load_compaction(data_dir: str | Path) -> CompactionPolicy:
    path = Path(data_dir) / CONFIG_FILE
    raw: Any = _read(path).get("compaction") if path.is_file() else None
    if raw is None:
        return CompactionPolicy()
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} 配置无效：[compaction] 必须是一个表")
    data: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    unknown = set(data) - {"auto", "ratio", "max_tokens"}
    if unknown:
        raise SettingsError(f"{path} 配置无效：[compaction] 不认识 {', '.join(sorted(unknown))}")
    auto = data.get("auto", True)
    if not isinstance(auto, bool):
        raise SettingsError(f"{path} 配置无效：[compaction] auto 只能是 true 或 false")
    try:
        return CompactionPolicy(
            auto=auto,
            ratio=float(data.get("ratio", 0.7)),
            max_tokens=int(data.get("max_tokens", 60_000)),
        )
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{path} 配置无效：[compaction] {exc}") from exc


def load_settings(data_dir: str | Path, env: Mapping[str, str] | None = None) -> GatewayConfig:
    environ = os.environ if env is None else env
    path = Path(data_dir) / CONFIG_FILE
    if path.is_file():
        data = _read(path)
        data.pop("compaction", None)  # 应用层配置，不属于模型网关
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
