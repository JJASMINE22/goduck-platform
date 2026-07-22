"""CLI HITL decision collector (migrated, retained as dev tool).

The HTTP counterpart lives in services/hitl_resolver.py — same decision format,
decision source switched from stdin to HTTP request body.
"""
from __future__ import annotations

import asyncio

_VALID_DECISIONS = {"approve", "edit", "reject", "respond"}

_CYAN = "\033[96m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_GREEN = "\033[92m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


async def _input_async(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


def _print_review_header(action: dict, review: dict, index: int) -> None:
    allowed = review.get("allowed_decisions", list(_VALID_DECISIONS))
    args = action.get("args") or {}

    print(
        f"\n{_BOLD}{_YELLOW}🛑 人机交互审核 #{index + 1}{_RESET}\n"
        f"  工具: {_CYAN}{action['name']}{_RESET}\n"
        f"  原始参数:"
    )
    for k, v in args.items():
        print(f"    {_CYAN}{k}{_RESET} = {_CYAN}{v!r}{_RESET}")
    if not args:
        print(f"    {_CYAN}(无参数){_RESET}")
    print(f"  允许操作: {_GREEN}{', '.join(allowed)}{_RESET}")


async def _collect_edit_args(original_args: dict) -> dict:
    if not original_args:
        print(f"  {_YELLOW}(无原始参数，将添加新参数){_RESET}")
        new_args = {}
        while True:
            raw = await _input_async("  添加参数 (key=value，空行结束): ")
            if not raw.strip():
                break
            if "=" in raw:
                k, v = raw.split("=", 1)
                new_args[k.strip()] = v.strip()
        return new_args

    new_args = {}
    for k, v in original_args.items():
        raw = await _input_async(f"  {_CYAN}{k}{_RESET} (当前: {v!r}) → 新值 (回车保留): ")
        if raw.strip():
            new_args[k] = _auto_cast(raw.strip())
        else:
            new_args[k] = v
    return new_args


def _auto_cast(value: str):
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


async def _prompt_decision(allowed: list[str]) -> str:
    allowed_set = set(allowed)
    while True:
        raw = await _input_async(f"  📍 请选择操作 ({'/'.join(allowed)}): ")
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            choice = "approve"
        elif choice in ("n", "no"):
            choice = "reject"
        if choice in allowed_set:
            return choice
        print(f"  {_RED}无效操作 '{choice}'，请从 [{', '.join(allowed)}] 中选择{_RESET}")


async def collect_cli_decisions(
    action_requests: list[dict],
    review_configs: list[dict],
) -> list[dict]:
    decisions = []
    for idx, action in enumerate(action_requests):
        review = review_configs[idx] if idx < len(review_configs) else {}
        allowed = review.get("allowed_decisions", list(_VALID_DECISIONS))
        _print_review_header(action, review, idx)

        decision_type = await _prompt_decision(allowed)

        if decision_type == "approve":
            decisions.append({"type": "approve"})

        elif decision_type == "edit":
            original_args = action.get("args") or {}
            edited_args = await _collect_edit_args(original_args)
            decisions.append({
                "type": "edit",
                "edited_action": {
                    "name": action["name"],
                    "args": edited_args,
                },
            })

        elif decision_type == "reject":
            reason = await _input_async(f"  {_RED}请输入拒绝原因 (回车使用默认消息): {_RESET}")
            message = reason.strip() if reason.strip() else (
                f"User rejected the action '{action['name']}'. "
                "Do not retry this tool call unless the user asks."
            )
            decisions.append({"type": "reject", "message": message})

        elif decision_type == "respond":
            reply = await _input_async(f"  {_CYAN}请输入直接回复内容: {_RESET}")
            message = reply.strip() if reply.strip() else "(无回复)"
            decisions.append({"type": "respond", "message": message})

    return decisions
