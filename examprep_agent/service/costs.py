# -*- coding: utf-8 -*-
"""Offline token/cost aggregation over the existing JSONL event logs.

No network, no new storage: the kernel already writes one JSON object per model
response under ``<workspace>/logs/agent-*.jsonl`` with a ``usage`` block.  This
module reads those files, groups token counts by ``session_id`` and by command,
and multiplies them by a configurable unit price.

Price resolution order (first match wins):

1. explicit ``input_price`` / ``output_price`` arguments (CLI flags, HTTP body);
2. ``EXAMPREP_PRICE_INPUT_PER_MTOK`` / ``EXAMPREP_PRICE_OUTPUT_PER_MTOK``;
3. the documented defaults below (they are a placeholder, not a billing
   guarantee; override them for your provider/contract).
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Iterable

from ..security.audit import read_jsonl
from .models import CommandCost, CostsResult, SessionCost

DEFAULT_INPUT_PRICE_PER_MTOK = 0.27   # USD per million prompt tokens (example)
DEFAULT_OUTPUT_PRICE_PER_MTOK = 1.10  # USD per million completion tokens (example)

_NEXT_SESSION = re.compile(r":ch\d+:p\d+$")


def _finite_price(value: Any, label: str) -> float:
    """Validate a user/env unit price: finite and non-negative (S4)."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % label) from exc
    if not math.isfinite(number):
        raise ValueError("%s must be a finite number" % label)
    return max(0.0, number)


def _env_price(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def resolve_prices(
    input_price: float | None = None,
    output_price: float | None = None,
) -> tuple[float, float]:
    """Resolve the two unit prices (USD per million tokens).

    Explicit values are validated (finite, non-negative); a ``NaN``/``inf``
    flag would otherwise produce non-standard JSON and crash the aggregator.
    """
    price_in = input_price
    if price_in is None:
        price_in = _env_price("EXAMPREP_PRICE_INPUT_PER_MTOK")
    if price_in is None:
        price_in = DEFAULT_INPUT_PRICE_PER_MTOK
    price_out = output_price
    if price_out is None:
        price_out = _env_price("EXAMPREP_PRICE_OUTPUT_PER_MTOK")
    if price_out is None:
        price_out = DEFAULT_OUTPUT_PRICE_PER_MTOK
    return (_finite_price(price_in, "input price"),
            _finite_price(price_out, "output price"))


def infer_command(session_id: str, metadata: Any = None) -> str:
    """Command label for an event, from its metadata or the session id shape."""
    if isinstance(metadata, dict):
        command = metadata.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    if ":ch" in session_id and _NEXT_SESSION.search(session_id):
        return "next"
    if session_id.startswith("mcp:"):
        return "mcp"
    return "agent"


def _usage_counts(event: dict[str, Any]) -> tuple[int, int]:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    try:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
    except (TypeError, ValueError, OverflowError):
        # ``int(float("inf"))`` raises OverflowError; a malformed usage block
        # must be skipped, not crash the whole aggregation.
        return 0, 0
    return prompt, completion


def _log_files(workspace: str) -> list[str]:
    logs = os.path.join(os.path.abspath(workspace), "logs")
    if not os.path.isdir(logs):
        return []
    return sorted(
        os.path.join(logs, name)
        for name in os.listdir(logs)
        if name.startswith("agent-") and name.endswith(".jsonl")
    )


def aggregate_events(
    events: Iterable[dict[str, Any]],
    *,
    input_price: float | None = None,
    output_price: float | None = None,
) -> tuple[list[SessionCost], list[CommandCost], tuple[int, int], tuple[float, float]]:
    """Group ``model_response`` events by session and command.

    Returns ``(sessions, commands, (prompt, completion), (price_in, price_out))``.
    """
    price_in, price_out = resolve_prices(input_price, output_price)
    sessions: dict[tuple[str, str], SessionCost] = {}
    counted = {"model_response", "compaction"}
    for event in events:
        if not isinstance(event, dict) or event.get("event") not in counted:
            continue
        prompt, completion = _usage_counts(event)
        if prompt == 0 and completion == 0:
            continue
        session_id = str(event.get("session_id") or "unknown")
        command = str(event.get("command") or "").strip() or infer_command(
            session_id, event.get("metadata"))
        model = str(event.get("model") or event.get("provider") or "unknown")
        key = (session_id, command)
        row = sessions.get(key)
        if row is None:
            row = sessions[key] = SessionCost(session_id=session_id, command=command, model=model)
        elif row.model == "unknown" and model != "unknown":
            row.model = model
        row.calls += 1
        row.prompt_tokens += prompt
        row.completion_tokens += completion

    session_rows: list[SessionCost] = []
    command_rows: dict[str, CommandCost] = {}
    command_sessions: dict[str, set[str]] = {}
    total_prompt = total_completion = 0
    for row in sessions.values():
        row.total_tokens = row.prompt_tokens + row.completion_tokens
        row.cost = (
            row.prompt_tokens / 1_000_000.0 * price_in
            + row.completion_tokens / 1_000_000.0 * price_out
        )
        session_rows.append(row)
        cmd = command_rows.get(row.command)
        if cmd is None:
            cmd = command_rows[row.command] = CommandCost(command=row.command)
        # S4: ``sessions`` is the number of distinct session ids, not the
        # number of (session, command) rows; one session running two commands
        # must not be counted twice under each command.
        command_sessions.setdefault(row.command, set()).add(row.session_id)
        cmd.calls += row.calls
        cmd.prompt_tokens += row.prompt_tokens
        cmd.completion_tokens += row.completion_tokens
        cmd.total_tokens += row.total_tokens
        total_prompt += row.prompt_tokens
        total_completion += row.completion_tokens
    for command, session_ids in command_sessions.items():
        command_rows[command].sessions = len(session_ids)
    for cmd in command_rows.values():
        cmd.cost = (
            cmd.prompt_tokens / 1_000_000.0 * price_in
            + cmd.completion_tokens / 1_000_000.0 * price_out
        )
    session_rows.sort(key=lambda item: (-item.total_tokens, item.session_id))
    command_rows_sorted = sorted(command_rows.values(), key=lambda item: (-item.total_tokens, item.command))
    return session_rows, command_rows_sorted, (total_prompt, total_completion), (price_in, price_out)


def aggregate_costs(
    workspace: str,
    *,
    input_price: float | None = None,
    output_price: float | None = None,
    command: str | None = None,
) -> CostsResult:
    """Aggregate every agent log in ``workspace`` (offline, read-only)."""
    files = _log_files(workspace)
    events: list[dict[str, Any]] = []
    for path in files:
        events.extend(read_jsonl(path))
    sessions, commands, (prompt, completion), (price_in, price_out) = aggregate_events(
        events, input_price=input_price, output_price=output_price)
    if command:
        sessions = [row for row in sessions if row.command == command]
        commands = [row for row in commands if row.command == command]
        prompt = sum(row.prompt_tokens for row in sessions)
        completion = sum(row.completion_tokens for row in sessions)
    total_cost = prompt / 1_000_000.0 * price_in + completion / 1_000_000.0 * price_out
    return CostsResult(
        workspace=os.path.abspath(workspace),
        sessions=sessions,
        commands=commands,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost=total_cost,
        input_price_per_mtok=price_in,
        output_price_per_mtok=price_out,
        log_files=[os.path.relpath(path, workspace) for path in files],
    )
