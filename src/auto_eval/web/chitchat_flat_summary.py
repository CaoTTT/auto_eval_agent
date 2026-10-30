"""Live seven-dimension chitchat summaries sharing the export's score and null policy."""
from __future__ import annotations

from copy import copy
from itertools import combinations
from statistics import mean

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol
from .qa_chat_summary import task_snapshot
from .chitchat_flat_statistics import DIMENSIONS, dimension_metrics, chitchat_flat_score_state


def summarize_chitchat_flat(task, *, include_subsets: bool = True) -> dict:
    from .tasks import latest_results_by_index
    from .compare_statistics import Case
    snapshot = task_snapshot(task)
    recovery = recover_protocol_identity(snapshot)
    latest = {i: row for i, row in latest_results_by_index(task).items() if 0 <= i < len(task.items)}
    count = max([int(item.get("product_count") or 2) for item in task.items] + [2])
    ok = [row for row in latest.values() if not row.get("error")]
    summary = {"total": len(task.items), "done": len(ok), "failed": len(latest) - len(ok),
               "unfinished": len(task.items) - len(latest), "mode": "compare", "product_count": count,
               "standard_id": "chitchat_evidence_first_flat", "standard_version": recovery["manifest"].get("standard_version"),
               "evaluation_profile": recovery["evaluation_profile"], "protocol_diagnostics": recovery["diagnostics"],
               "needs_human_review_count": sum(bool(row.get("needs_human_review")) for row in latest.values()),
               "input_failed": sum(any(row.get(f"answer{p}_input_status") == "failed" for p in range(1, count + 1)) for row in ok)}
    if not recovery["executable"]:
        return summary
    protocol = require_snapshot_protocol(snapshot)
    cases = [Case(i, item, latest.get(i, {}),
                  "failed" if latest.get(i, {}).get("error") else "done" if i in latest else "pending",
                  int(item.get("product_count") or 2), protocol.standard_version, protocol.bundle_revision,
                  protocol.standard_id) for i, item in enumerate(task.items)]
    comparable = set()
    for dimension in DIMENSIONS:
        summary[dimension + "_all_products_scored"] = 0
        for product in range(1, count + 1):
            summary.update({f"{dimension}_answer{product}_{key}": value
                            for key, value in dimension_metrics(cases, product, dimension, protocol).items()})
            summary[f"{dimension}_answer{product}_all_products_avg"] = None
        for n in sorted({case.product_count for case in cases}):
            valid = [[chitchat_flat_score_state(case, p, dimension, protocol)[1] for p in range(1, n + 1)]
                     for case in cases if case.product_count == n]
            valid = [scores for scores in valid if all(score is not None for score in scores)]
            prefix = dimension if n == count else f"{dimension}_{n}products"
            summary[prefix + "_all_products_scored"] = len(valid)
            for product in range(1, n + 1):
                summary[f"{prefix}_answer{product}_all_products_avg"] = mean(row[product - 1] for row in valid) if valid else None
        for a, b in combinations(range(1, count + 1), 2):
            pairs = [(case.index, chitchat_flat_score_state(case, a, dimension, protocol)[1],
                      chitchat_flat_score_state(case, b, dimension, protocol)[1])
                     for case in cases if case.product_count >= b]
            pairs = [(index, x, y) for index, x, y in pairs if x is not None and y is not None]
            comparable.update(index for index, _, _ in pairs)
            prefix = f"{dimension}_answer{a}_answer{b}"
            summary[prefix + "_paired_count"] = len(pairs)
            summary[f"{prefix}_paired_answer{a}_avg"] = mean(x for _, x, _ in pairs) if pairs else None
            summary[f"{prefix}_paired_answer{b}_avg"] = mean(y for _, _, y in pairs) if pairs else None
            for first, second in ((a, b), (b, a)):
                key = f"{dimension}_answer{first}_answer{second}"
                x, y = (summary[f"{dimension}_answer{p}_avg"] for p in (first, second))
                summary[key + "_mean_ratio"] = x / y if x is not None and y else None
                px, py = (summary[f"{prefix}_paired_answer{p}_avg"] for p in (first, second))
                summary[key + "_paired_mean_ratio"] = px / py if px is not None and py else None
                summary[key + "_paired_count"] = len(pairs)
                summary[f"{key}_paired_answer{first}_avg"] = px
                summary[f"{key}_paired_answer{second}_avg"] = py
    summary["comparable"] = len(comparable)
    if include_subsets:
        from .chitchat_flat_statistics import chitchat_flat_conversation_statistics
        aligned = [{**latest.get(i, {}), "评估状态": "评估失败" if latest.get(i, {}).get("error") else
                    "已完成" if i in latest else "待评估"} for i in range(len(task.items))]
        conversation = chitchat_flat_conversation_statistics(snapshot, aligned)
        if conversation:
            summary["conversation_statistics"] = [{"title": table.title, "headers": table.headers, "rows": table.rows} for table in conversation]
        summary["input_modality_counts"], summary["by_input_modality"] = {}, {}
        for modality in ("text", "text_image"):
            indices = [i for i, item in enumerate(task.items) if (latest.get(i, {}).get("effective_input_modality") or
                       item.get("effective_input_modality") or ("text_image" if item.get("query_images") else "text")) == modality]
            subset = copy(task)
            subset.items = [task.items[i] for i in indices]
            subset.results = [{**latest[old], "index": new} for new, old in enumerate(indices) if old in latest]
            summary["input_modality_counts"][modality] = len(indices)
            summary["by_input_modality"][modality] = summarize_chitchat_flat(subset, include_subsets=False)
    return summary
