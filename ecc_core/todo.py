from dataclasses import dataclass, field
from typing import Literal

Status = Literal["pending", "in_progress", "completed"]
Priority = Literal["high", "medium", "low"]

@dataclass
class TodoItem:
    id: str
    content: str
    status: Status = "pending"
    priority: Priority = "medium"

class TodoManager:
    STATUS_ICONS = {"pending": "○", "in_progress": "→", "completed": "✓"}
    PRIORITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    def __init__(self):
        self._todos: list[TodoItem] = []

    def update(self, raw_todos: list[dict]):
        self._todos = [
            TodoItem(
                id=t.get("id", f"t{i}"),
                content=t.get("content", ""),
                status=t.get("status", "pending"),
                priority=t.get("priority", "medium"),
            )
            for i, t in enumerate(raw_todos)
        ]

    def has_todos(self) -> bool:
        return bool(self._todos)

    def all_completed(self) -> bool:
        return bool(self._todos) and all(t.status == "completed" for t in self._todos)

    def in_progress_items(self) -> list[TodoItem]:
        return [t for t in self._todos if t.status == "in_progress"]

    def format_display(self) -> str:
        if not self._todos:
            return ""
        lines = ["  📋 Progress:"]
        for t in self._todos:
            s = self.STATUS_ICONS.get(t.status, "?")
            p = self.PRIORITY_ICONS.get(t.priority, "")
            lines.append(f"    {s} {p} [{t.id}] {t.content}")
        return "\n".join(lines)

    def format_for_llm(self) -> str:
        if not self._todos:
            return "(no todos)"
        return "\n".join(f"[{t.id}] {t.status} | {t.content}" for t in self._todos)

    def format_nag(self) -> str:
        remaining = [t for t in self._todos if t.status != "completed"]
        if not remaining:
            return ""
        lines = ["[Currently in progress]"]
        for t in remaining:
            lines.append(f"  {self.STATUS_ICONS.get(t.status,'?')} [{t.id}] {t.content}")
        return "\n".join(lines)
