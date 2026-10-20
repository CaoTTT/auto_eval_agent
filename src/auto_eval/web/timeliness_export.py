"""Flat, auditable timeliness exports; reasons precede scores."""
from __future__ import annotations

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol
from .timeliness_statistics import CONFLICT_LABELS, DIMENSION_NAMES, STATUS_LABELS, conflict_state, timeliness_score_state


def timeliness_export_rows(snapshot: dict, results: list[dict]) -> list[dict]:
    from .compare_statistics import Case
    from .history import _judge_export_fields
    recovered = recover_protocol_identity(snapshot)
    manifest = recovered["manifest"]
    protocol = require_snapshot_protocol(snapshot) if recovered["executable"] else None
    items = snapshot.get("items") or []
    rows = []
    for position, result in enumerate(results):
        index = result.get("index", position)
        item = items[index] if type(index) is int and 0 <= index < len(items) else {}
        count = int(result.get("product_count") or item.get("product_count") or 2)
        status = "failed" if result.get("error") or result.get("评估状态") == "评估失败" else (
            "unfinished" if result.get("评估状态") == "待评估" else "done")
        case = Case(index, item, result, status, count, manifest.get("standard_version", ""),
                    manifest.get("bundle_revision", ""), "timeliness_compare")
        row = {"数据集序号": index + 1 if type(index) is int else "",
               "query_id": result.get("item_id") or item.get("id", ""),
               "题目": item.get("query") or item.get("question") or result.get("query", ""),
               "背景": item.get("context", result.get("context", "")),
               "评估状态": result.get("评估状态") or ("评估失败" if result.get("error") else "已完成"),
               "error": result.get("error", ""), "标准ID": "timeliness_compare",
               "标准版本": manifest.get("standard_version", result.get("standard_version", "")),
               "评测协议": recovered["evaluation_profile"], "协议包修订": manifest.get("bundle_revision"),
               "结果Schema版本": manifest.get("result_schema_version"), "协议恢复诊断": recovered["diagnostics"],
               "产品数量": count, **_judge_export_fields(result)}
        for p in range(1, count + 1):
            prefix, name = f"answer{p}", f"产品{p}"
            row[name + "背景"] = item.get(f"context{p}", result.get(f"context{p}", ""))
            row[name + "回答"] = item.get(prefix, result.get(prefix, ""))
            row[name + "输入状态"] = result.get(prefix + "_input_status")
            row[name + "执行状态"] = result.get(prefix + "_execution_status")
            for gate, label in (("response", "响应体验"), ("safety", "安全稳定")):
                row[name + label + "理由"] = result.get(f"{prefix}_{gate}_gate_reason", "")
                row[name + label + "Gate"] = result.get(f"{prefix}_{gate}_gate")
        for dim, label in DIMENSION_NAMES.items():
            row[label + "证据（维度共享）"] = result.get(dim + "_evidence", [])
            row[label + "理由（维度共享）"] = result.get(dim + "_reason", "")
            for p in range(1, count + 1):
                prefix, name = f"answer{p}_{dim}", f"产品{p}{label}"
                if manifest.get("result_schema_version") in ("timeliness-result-2", "timeliness-result-3"):
                    if dim != "understanding":
                        row[name + "NA原因"] = result.get(prefix + "_na_reason")
                else:
                    row[name + "是否适用"] = result.get(prefix + "_applicable")
                    row[name + "核验状态"] = result.get(prefix + "_verification_status")
                state, score = timeliness_score_state(case, p, dim, protocol) if protocol else ("identity_error", None)
                row[name + "评分状态"] = STATUS_LABELS.get(state, state)
                row[name + "分"] = score
                if score is None and result.get(prefix + "_score") is not None:
                    row[name + "原始异常分"] = result[prefix + "_score"]
        state = conflict_state(case, protocol) if recovered["executable"] else "identity_error"
        if manifest.get("result_schema_version") == "timeliness-result-3":
            row["内容冲突理由"] = result.get("conflict_reason", "")
            verdict = result.get("has_conflict")
            label = CONFLICT_LABELS.get(verdict, verdict) if isinstance(verdict, str) else verdict
            row["内容冲突"] = f"采集失败，待核验（模型：{label}）" if state == "input_failed" else label
            row["模型内容冲突原始值"] = verdict
        row["内容冲突采集状态"] = CONFLICT_LABELS.get(state, state)
        for key, label in (("needs_human_review", "是否需要人工复核"), ("review_reasons", "人工复核原因"),
                           ("standard_source_sha256", "标准源文件SHA256"), ("prompt_sha256", "Prompt SHA256"),
                           ("input_manifest_sha256", "输入指纹"), ("trusted_collection_metadata", "可信采集元数据"),
                           ("evaluation_run_at", "评估运行时间（非产品测试时间）"),
                           ("normalization_diagnostics", "状态规范化诊断"),
                           ("structural_diagnostics", "结构诊断")):
            row[label] = result.get(key)
        if not recovered["executable"]:
            row["原始结果（协议待恢复）"] = result
        rows.append(row)
    return rows
