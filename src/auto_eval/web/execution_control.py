"""Durable selection for resuming the same evaluation without losing results."""
from __future__ import annotations

from .tasks import Task, latest_results_by_index


def resume_indexes(task: Task, include_failed: bool = False) -> list[int]:
    latest = latest_results_by_index(task)
    selected = set(task.execution_control.get("pending_indexes", []))
    for batch in task.execution_control.get("update_batches", {}).values():
        selected.update(batch.get("remaining", []))
    for index in range(len(task.items)):
        result = latest.get(index)
        if result is None or (include_failed and result.get("error")):
            selected.add(index)
    # Re-evaluating a failed/invalidated turn invalidates subsequent summaries.
    if task.mode == "rich_content":
        groups: dict[str, list[int]] = {}
        for index, item in enumerate(task.items):
            if item.get("session_group"):
                groups.setdefault(str(item["session_group"]), []).append(index)
        for indexes in groups.values():
            indexes.sort(key=lambda i: task.items[i].get("turn_index", 0))
            positions = [pos for pos, index in enumerate(indexes) if index in selected]
            if positions:
                selected.update(indexes[min(positions):])
    return sorted(index for index in selected if 0 <= index < len(task.items))
