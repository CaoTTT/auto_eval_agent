"""Task progress summaries using the same per-product score policy as reports."""
from __future__ import annotations

from collections import Counter
from copy import copy
from itertools import combinations

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol


def task_snapshot(task) -> dict:
    return {"mode": task.mode, "items": task.items, "results": task.results,
            "evaluation_profile": task.evaluation_profile, "protocol_manifest": task.protocol_manifest,
            "options": task.options}


def summarize_chat(task, *, include_subsets: bool = True) -> dict:
    from .tasks import latest_results_by_index
    from .compare_statistics import Case, score_state, product_applicable, conversation_statistics
    snapshot = task_snapshot(task)
    recovery = recover_protocol_identity(snapshot)
    latest = {index: row for index, row in latest_results_by_index(task).items() if 0 <= index < len(task.items)}
    ok = [row for row in latest.values() if not row.get("error")]
    count = max([int(item.get("product_count") or 2) for item in task.items] +
                [int(row.get("product_count") or 2) for row in ok] + [2])
    summary = {"total": len(task.items), "done": len(ok), "failed": len(latest) - len(ok),
               "unfinished": len(task.items) - len(latest), "mode": "compare", "product_count": count,
               "standard_id": recovery["manifest"].get("standard_id", recovery["evaluation_profile"].split("@", 1)[0]), "standard_version": recovery["manifest"].get("standard_version"),
               "evaluation_profile": recovery["evaluation_profile"], "protocol_diagnostics": recovery["diagnostics"],
               "needs_human_review_count": sum(bool(row.get("needs_human_review")) for row in latest.values())}
    if not recovery["executable"]:
        return summary
    protocol = require_snapshot_protocol(snapshot)
    cases = [Case(index, item, latest.get(index, {}),
                  "failed" if latest.get(index, {}).get("error") else "done" if index in latest else "pending",
                  int(latest.get(index, {}).get("product_count") or item.get("product_count") or 2),
                  protocol.standard_version, protocol.bundle_revision, protocol.standard_id)
             for index, item in enumerate(task.items)]
    comparable = set()
    summary["input_failed"] = sum(any(row.get(f"answer{p}_input_status") == "failed" for p in range(1, int(row.get("product_count") or 2) + 1)) for row in ok)
    for product in range(1, count + 1):
        for gate in ("response", "safety"):
            summary[f"answer{product}_{gate}_gate_pass"] = sum(row.get(f"answer{product}_{gate}_gate") == "pass" and row.get(f"answer{product}_input_status") in {"complete", "partial"} for row in ok)
    for dimension in protocol.score_dimensions:
        all_valid = []
        for product in range(1, count + 1):
            present = [case for case in cases if case.product_count >= product]
            applicable = [product_applicable(case, product, dimension, protocol) for case in present]
            states = [score_state(case, product, dimension, protocol) for case in present]
            values = [value for _, value in states if value is not None]
            prefix = f"{dimension}_answer{product}"
            a = sum(value is True for value in applicable)
            summary.update({f"{prefix}_applicable": a,
                            f"{prefix}_applicable_unknown": sum(value is None for value in applicable),
                            f"{prefix}_scored": len(values), f"{prefix}_score_coverage": len(values) / a if a else None,
                            f"{prefix}_avg": round(sum(values) / len(values), 3) if values else None,
                            f"{prefix}_score_status_counts": dict(Counter(state for state, _ in states))})
        for case in cases:
            valid = {product for product in range(1, case.product_count + 1) if score_state(case, product, dimension, protocol)[1] is not None}
            if len(valid) >= 2:
                comparable.add(case.index)
            if len(valid) == case.product_count:
                all_valid.append(case)
        summary[f"{dimension}_all_products_scored"] = len(all_valid)
        summary[f"{dimension}_scored"] = len(all_valid)
        summary[f"{dimension}_applicable"] = sum(any(product_applicable(case, p, dimension, protocol) is True for p in range(1, case.product_count + 1)) for case in cases)
        for a, b in combinations(range(1, count + 1), 2):
            summary[f"{dimension}_answer{a}_answer{b}_paired_count"] = sum(case.product_count >= b and all(score_state(case, p, dimension, protocol)[1] is not None for p in (a, b)) for case in cases)
    summary["comparable"] = len(comparable)
    for state in ("yes", "no", "unclear"):
        summary[f"conflict_{state}"] = sum(row.get("has_conflict") == state for row in ok)
    if include_subsets:
        from ..conversation import is_conversation
        aligned = [{**latest.get(i, {}), "评估状态": "评估失败" if latest.get(i, {}).get("error") else "已完成" if i in latest else "待评估"} for i in range(len(task.items))]
        if any(is_conversation(item) for item in task.items):
            summary["conversation_statistics"] = [{"title": table.title, "headers": table.headers, "rows": table.rows} for table in conversation_statistics(snapshot, aligned)]
        summary["input_modality_counts"], summary["by_input_modality"] = {}, {}
        for modality in ("text", "text_image"):
            indexes = [i for i, item in enumerate(task.items) if (latest.get(i, {}).get("effective_input_modality") or item.get("effective_input_modality") or ("text_image" if item.get("query_images") else "text")) == modality]
            subset = copy(task)
            subset.items = [task.items[i] for i in indexes]
            subset.results = [{**latest[old], "index": new} for new, old in enumerate(indexes) if old in latest]
            summary["input_modality_counts"][modality] = len(indexes)
            summary["by_input_modality"][modality] = summarize_chat(subset, include_subsets=False)
    return summary
