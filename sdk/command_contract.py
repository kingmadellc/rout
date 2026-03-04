"""Typed command contracts for Rout plugin handlers."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CommandContext:
    """Normalized context passed to handlers."""

    args: str = ""
    message: str = ""
    sender: str = ""
    chat_id: Optional[int] = None
    sender_name: str = ""
    is_group: bool = False
    attachments: List[str] = field(default_factory=list)


@dataclass
class CommandResult:
    """Structured output from handlers."""

    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def context_from_inputs(
    args: str = "",
    message: str = "",
    sender: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> CommandContext:
    """Build a typed context from watcher inputs."""
    meta = metadata or {}
    return CommandContext(
        args=args or "",
        message=message or args or "",
        sender=sender or "",
        chat_id=meta.get("chat_id"),
        sender_name=meta.get("sender_name", ""),
        is_group=bool(meta.get("is_group")),
        attachments=list(meta.get("attachments", []) or []),
    )


def text_result(text: str, **metadata: Any) -> CommandResult:
    """Convenience constructor for text-only command output."""
    return CommandResult(text=text, metadata=metadata)
