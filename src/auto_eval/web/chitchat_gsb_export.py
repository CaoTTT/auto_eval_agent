"""Preserve direct overall GSB and its evidence instead of exporting fake scores."""
from __future__ import annotations

from itertools import combinations

from .chitchat_gsb_statistics import STATUS_LABELS, gsb_pair_state
from .protocol_identity import recover_protocol_identity, require_snapshot_protocol


def chitchat_gsb_export_rows(snapshot: dict, results: list[dict]) -> list[dict]:
    from .compare_statistics import Case
    from .history import _judge_export_fields
    recovery = recover_protocol_identity(snapshot)
    manifest = recovery["manifest"]
    protocol = require_snapshot_protocol(snapshot) if recovery["executable"] else None
    items, rows = snapshot.get("items") or [], []
    for position, result in enumerate(results):
        index = result.get("index", position)
        item = items[index] if type(index) is int and 0 <= index < len(items) else {}
        count = int(item.get("product_count") or result.get("product_count") or 2)
        status = "failed" if result.get("error") or result.get("评估状态") == "评估失败" else (
            "unfinished" if result.get("评估状态") == "待评估" else "done")
        case = Case(index, item, result, status, count, manifest.get("standard_version", ""),
                    manifest.get("bundle_revision", ""), "chitchat_gsb_compare",
                    "；".join(recovery["diagnostics"]))
        row = {
            "数据集序号": index + 1 if type(index) is int else "", "query_id": result.get("item_id") or item.get("id", ""),
            "题目": item.get("query") or item.get("question") or result.get("query", ""),
            "评估状态": result.get("评估状态") or ("评估失败" if result.get("error") else "已完成"),
            "error": result.get("error", ""), "标准ID": "chitchat_gsb_compare",
            "标准版本": manifest.get("standard_version", result.get("standard_version", "")),
            "评测协议": recovery["evaluation_profile"], "协议包修订": manifest.get("bundle_revision"),
            "结果Schema版本": manifest.get("result_schema_version"), "协议恢复诊断": recovery["diagnostics"],
            "产品数量": count, **_judge_export_fields(result),
        }
        for product in range(1, count + 1):
            name, prefix = f"产品{product}", f"answer{product}"
            row[name + "背景"] = item.get(f"context{product}", result.get(f"context{product}", ""))
            row[name + "回答"] = item.get(prefix, result.get(prefix, ""))
            row[name + "输入状态"] = result.get(prefix + "_input_status")
            row[name + "执行状态"] = result.get(prefix + "_execution_status")
        for a, b in combinations(range(1, count + 1), 2):
            label = f"产品{a}对产品{b} "
            state, value = gsb_pair_state(case, a, b, protocol)
            pairs = result.get("gsb_pairs")
            raw = pairs.get(f"{a}_{b}") if isinstance(pairs, dict) else None
            raw = raw if isinstance(raw, dict) else {}
            row[label + "GSB"] = value["gsb"] if state == "evaluated" else None
            row[label + "判定状态"] = STATUS_LABELS.get(state, state)
            row[label + "理由"] = (value or raw).get("reason", "")
            row[label + "严重问题"] = (value or raw).get("serious_issue")
            originals = result.get("model_pair_results")
            original = originals.get(f"pair_{a}_{b}") if isinstance(originals, dict) else None
            if original is not None:
                row[label + "模型原始判定"] = original
        for key, label in (("needs_human_review", "是否需要人工复核"), ("review_reasons", "人工复核原因"),
                           ("prompt_sha256", "Prompt SHA256"), ("input_manifest_sha256", "输入指纹"),
                           ("trusted_collection_metadata", "可信采集元数据"),
                           ("normalization_diagnostics", "状态规范化诊断"), ("structural_diagnostics", "结构诊断")):
            row[label] = result.get(key)
        row["评估运行时间（非产品测试时间）"] = result.get("evaluation_run_at") or result.get("evaluation_datetime")
        if not recovery["executable"]:
            row["原始结果（协议待恢复）"] = result
        rows.append(row)
    return rows
