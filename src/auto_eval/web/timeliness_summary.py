"""Live summaries for timeliness, sharing the export denominator policy."""
from __future__ import annotations

from copy import copy
from itertools import combinations
from statistics import mean

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol
from .qa_chat_summary import task_snapshot
from .timeliness_statistics import DIMENSIONS, GATES, dimension_metrics, gate_observable, timeliness_score_state


def summarize_timeliness(task, *, include_subsets: bool = True) -> dict:
    from .tasks import latest_results_by_index
    from .compare_statistics import Case
    snapshot = task_snapshot(task)
    recovery = recover_protocol_identity(snapshot)
    latest = {i: r for i, r in latest_results_by_index(task).items() if 0 <= i < len(task.items)}
    count = max([int(i.get("product_count") or 2) for i in task.items] + [2])
    ok = [r for r in latest.values() if not r.get("error")]
    summary = {"total": len(task.items), "done": len(ok), "failed": len(latest) - len(ok),
               "unfinished": len(task.items) - len(latest), "mode": "compare", "product_count": count,
               "standard_id": "timeliness_compare", "standard_version": recovery["manifest"].get("standard_version"),
               "evaluation_profile": recovery["evaluation_profile"], "protocol_diagnostics": recovery["diagnostics"],
               "needs_human_review_count": sum(bool(r.get("needs_human_review")) for r in latest.values()),
               "input_failed": sum(any(r.get(f"answer{p}_input_status") == "failed" for p in range(1, count + 1)) for r in ok)}
    if not recovery["executable"]:
        return summary
    protocol = require_snapshot_protocol(snapshot)
    cases = [Case(i, item, latest.get(i, {}),
                  "failed" if latest.get(i, {}).get("error") else "done" if i in latest else "pending",
                  int(item.get("product_count") or 2), protocol.standard_version, protocol.bundle_revision,
                  protocol.standard_id) for i, item in enumerate(task.items)]
    for p in range(1, count + 1):
        observed = [c for c in cases if gate_observable(c, p)]
        combined = []
        for g in ("response", "safety"):
            values = [c.result.get(f"answer{p}_{g}_gate") for c in observed]
            prefix = f"answer{p}_{g}_gate"
            for state in GATES:
                summary[f"{prefix}_{state}"] = values.count(state)
            summary[prefix + "_observed"] = sum(v in GATES for v in values)
            n = summary[prefix + "_observed"]
            summary[prefix + "_pass_rate"] = values.count("pass") / n if n else None
        for c in observed:
            states = [c.result.get(f"answer{p}_{g}_gate") for g in ("response", "safety")]
            if all(s in GATES for s in states):
                combined.append("fail" if "fail" in states else "unclear" if "unclear" in states else "pass")
        for state in GATES:
            summary[f"answer{p}_gate_{state}"] = combined.count(state)
        summary[f"answer{p}_gate_observed"] = len(combined)
        summary[f"answer{p}_understanding_1"] = sum(timeliness_score_state(c, p, "understanding", protocol)[1] == 1 for c in cases)
    comparable = set()
    for dim in DIMENSIONS:
        for p in range(1, count + 1):
            summary.update({f"{dim}_answer{p}_{key}": value for key, value in dimension_metrics(cases, p, dim, protocol).items()})
        all_valid = [c for c in cases if c.product_count == count and all(timeliness_score_state(c, p, dim, protocol)[1] is not None
                                           for p in range(1, c.product_count + 1))]
        summary[f"{dim}_all_products_scored"] = len(all_valid)
        for p in range(1, count + 1):
            values = [timeliness_score_state(c, p, dim, protocol)[1] for c in all_valid if c.product_count >= p]
            summary[f"{dim}_answer{p}_all_products_avg"] = round(mean(values), 3) if values else None
        for a, b in combinations(range(1, count + 1), 2):
            pairs = [(c.index, timeliness_score_state(c, a, dim, protocol)[1], timeliness_score_state(c, b, dim, protocol)[1])
                     for c in cases if c.product_count >= b]
            pairs = [(i, x, y) for i, x, y in pairs if x is not None and y is not None]
            comparable.update(i for i, _, _ in pairs)
            prefix = f"{dim}_answer{a}_answer{b}"
            summary[prefix + "_paired_count"] = len(pairs)
            summary[f"{prefix}_paired_answer{a}_avg"] = round(mean(x for _, x, _ in pairs), 3) if pairs else None
            summary[f"{prefix}_paired_answer{b}_avg"] = round(mean(y for _, _, y in pairs), 3) if pairs else None
    summary["comparable"] = len(comparable)
    if include_subsets:
        from .timeliness_statistics import timeliness_conversation_statistics
        aligned = [{**latest.get(i, {}), "评估状态": "评估失败" if latest.get(i, {}).get("error") else
                    "已完成" if i in latest else "待评估"} for i in range(len(task.items))]
        conversation = timeliness_conversation_statistics(snapshot, aligned)
        if conversation:
            summary["conversation_statistics"] = [{"title": t.title, "headers": t.headers, "rows": t.rows} for t in conversation]
        summary["input_modality_counts"], summary["by_input_modality"] = {}, {}
        for modality in ("text", "text_image"):
            indices = [i for i, item in enumerate(task.items) if (latest.get(i, {}).get("effective_input_modality") or
                       item.get("effective_input_modality") or ("text_image" if item.get("query_images") else "text")) == modality]
            subset = copy(task)
            subset.items = [task.items[i] for i in indices]
            subset.results = [{**latest[old], "index": new} for new, old in enumerate(indices) if old in latest]
            summary["input_modality_counts"][modality] = len(indices)
            summary["by_input_modality"][modality] = summarize_timeliness(subset, include_subsets=False)
    return summary
