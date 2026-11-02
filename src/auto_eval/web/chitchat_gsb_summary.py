"""Live direct GSB counts share the exact export denominator policy."""
from __future__ import annotations

from collections import defaultdict
from copy import copy

from .chitchat_gsb_statistics import all_pair_metrics, gsb_pair_state
from .protocol_identity import recover_protocol_identity, require_snapshot_protocol
from .qa_chat_summary import task_snapshot


def summarize_chitchat_gsb(task, *, include_subsets: bool = True) -> dict:
    from .compare_statistics import _cases
    from .tasks import latest_results_by_index
    snapshot = task_snapshot(task)
    latest = {index: row for index, row in latest_results_by_index(task).items()
              if 0 <= index < len(task.items)}
    aligned = [{**latest.get(index, {}), "评估状态": "评估失败" if latest.get(index, {}).get("error") else
               "已完成" if index in latest else "待评估"} for index in range(len(task.items))]
    snapshot["results"] = aligned
    recovery = recover_protocol_identity(snapshot)
    count = max([int(item.get("product_count") or 2) for item in task.items] + [2])
    failed = sum(bool(row.get("error")) for row in latest.values())
    summary = {
        "total": len(task.items), "done": len(latest) - failed, "failed": failed,
        "unfinished": len(task.items) - len(latest), "mode": "compare", "product_count": count,
        "standard_id": "chitchat_gsb_compare", "evaluation_profile": recovery["evaluation_profile"],
        "standard_version": recovery["manifest"].get("standard_version"),
        "result_schema_version": recovery["manifest"].get("result_schema_version"),
        "protocol_diagnostics": recovery["diagnostics"], "statistic_unit": "turn",
        "statistic_unit_note": "每个数据条目/目标轮次与产品对计一次；会话分组只展示逐轮分布，不合成会话GSB。",
        "needs_human_review_count": sum(bool(row.get("needs_human_review")) for row in latest.values()),
        "input_failed": sum(any(row.get(f"answer{p}_input_status") == "failed"
                                for p in range(1, int(row.get("product_count") or 2) + 1))
                            for row in latest.values() if not row.get("error")),
    }
    if not recovery["executable"]:
        return summary
    protocol = require_snapshot_protocol(snapshot)
    cases = _cases(snapshot, aligned)
    summary["gsb_pairs"] = all_pair_metrics(cases, protocol)
    summary["comparable"] = sum(any(gsb_pair_state(case, a, b, protocol)[0] == "evaluated"
        for a in range(1, case.product_count) for b in range(a + 1, case.product_count + 1)) for case in cases)
    if include_subsets:
        from .chitchat_gsb_statistics import chitchat_gsb_conversation_statistics
        tables = chitchat_gsb_conversation_statistics(snapshot, aligned)
        if tables:
            summary["conversation_statistics"] = [{"title": t.title, "headers": t.headers, "rows": t.rows} for t in tables]
        sessions, turns = defaultdict(list), defaultdict(list)
        for case in cases:
            if case.statistic_unit.startswith("conversation:"):
                sessions[str(case.item["session_id"])].append(case)
                turns[str(case.item["turn_index"])].append(case)
        summary["by_session_id"] = {key: all_pair_metrics(group, protocol) for key, group in sessions.items()}
        summary["by_turn_index"] = {key: all_pair_metrics(group, protocol) for key, group in turns.items()}
        summary["input_modality_counts"], summary["by_input_modality"] = {}, {}
        for modality in ("text", "text_image"):
            indices = [index for index, item in enumerate(task.items)
                       if (latest.get(index, {}).get("effective_input_modality") or item.get("effective_input_modality") or
                           ("text_image" if item.get("query_images") else "text")) == modality]
            subset = copy(task)
            subset.items = [task.items[index] for index in indices]
            subset.results = [{**latest[old], "index": new} for new, old in enumerate(indices) if old in latest]
            summary["input_modality_counts"][modality] = len(indices)
            summary["by_input_modality"][modality] = summarize_chitchat_gsb(subset, include_subsets=False)
    return summary
