"""Flat knowledge QA exports with merged evidence/reason and fractional scores."""
from __future__ import annotations

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol
from .knowledge_qa_statistics import DIMENSION_NAMES, STATUS_LABELS, knowledge_qa_score_state


def knowledge_qa_export_rows(snapshot: dict, results: list[dict]) -> list[dict]:
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
                    manifest.get("bundle_revision", ""), "knowledge_qa_compare")
        row = {"数据集序号": index + 1 if type(index) is int else "",
               "query_id": result.get("item_id") or item.get("id", ""),
               "题目": item.get("query") or item.get("question") or result.get("query", ""),
               "评估状态": result.get("评估状态") or ("评估失败" if result.get("error") else "已完成"),
               "error": result.get("error", ""), "标准ID": "knowledge_qa_compare",
               "标准版本": manifest.get("standard_version", result.get("standard_version", "")),
               "评测协议": recovered["evaluation_profile"], "协议包修订": manifest.get("bundle_revision"),
               "结果Schema版本": manifest.get("result_schema_version"), "协议恢复诊断": recovered["diagnostics"],
               "产品数量": count, **_judge_export_fields(result)}
        for product in range(1, count + 1):
            prefix, name = f"answer{product}", f"产品{product}"
            row[name + "背景"] = item.get(f"context{product}", result.get(f"context{product}", ""))
            row[name + "回答"] = item.get(prefix, result.get(prefix, ""))
            row[name + "输入状态"] = result.get(prefix + "_input_status")
            row[name + "执行状态"] = result.get(prefix + "_execution_status")
        for dimension, label in DIMENSION_NAMES.items():
            row[label + "理由（含证据与分档依据）"] = result.get(dimension + "_reason", "")
            for product in range(1, count + 1):
                prefix, name = f"answer{product}_{dimension}", f"产品{product}{label}"
                state, score = knowledge_qa_score_state(case, product, dimension, protocol) if protocol else ("identity_error", None)
                row[name + "评分状态"] = STATUS_LABELS.get(state, state)
                row[name + "分"] = score
                if score is None and result.get(prefix + "_score") is not None:
                    row[name + "原始异常分"] = result[prefix + "_score"]
        for key, label in (("needs_human_review", "是否需要人工复核"), ("review_reasons", "人工复核原因"),
                           ("prompt_sha256", "Prompt SHA256"), ("input_manifest_sha256", "输入指纹"),
                           ("trusted_collection_metadata", "可信采集元数据"),
                           ("evaluation_run_at", "评估运行时间（非产品测试时间）"),
                           ("normalization_diagnostics", "状态规范化诊断"), ("structural_diagnostics", "结构诊断")):
            row[label] = result.get(key)
        if not recovered["executable"]:
            row["原始结果（协议待恢复）"] = result
        rows.append(row)
    return rows
