"""Chat result columns shared by full exports and the single-item API."""
from __future__ import annotations

from .protocol_identity import recover_protocol_identity

STATUS_LABELS = {"scored": "已评分", "not_applicable": "不适用",
                 "gate_blocked": "门槛未通过", "input_failed": "输入失败",
                 "unverifiable": "无法核验"}


def chat_export_rows(snapshot: dict, results: list[dict]) -> list[dict]:
    from ..judges.qa_chat_schema import QA_CHAT_DIMENSION_NAMES
    from .compare_statistics import Case, score_state
    from .protocol_identity import require_snapshot_protocol
    recovered = recover_protocol_identity(snapshot)
    manifest = recovered["manifest"]
    protocol = require_snapshot_protocol(snapshot) if recovered["executable"] else None
    # Interpret the frozen contract, including unreadable histories; never guess
    # from a particular score or fill missing product reasons from shared text.
    shared_output = (manifest.get("result_schema_version") == "qa-chat-result-2" or
                     recovered["evaluation_profile"] == "qa_chat_compare@1.1")
    dimensions = manifest.get("dimensions") or [
        {"key": key, "label": label} for key, label in QA_CHAT_DIMENSION_NAMES.items()]
    rows = []
    items = snapshot.get("items") or []
    for position, result in enumerate(results):
        index = result.get("index", position)
        item = items[index] if isinstance(index, int) and 0 <= index < len(items) else {}
        count = int(result.get("product_count") or item.get("product_count") or
                    (3 if item.get("screenshot3") or item.get("video3") else 2))
        row = {"数据集序号": index + 1 if isinstance(index, int) else "",
               "query_id": result.get("item_id") or item.get("id", ""),
               "题目": item.get("query") or item.get("question") or result.get("query", ""),
               "背景": item.get("context", result.get("context", "")),
               "评估状态": result.get("评估状态") or ("评估失败" if result.get("error") else "已完成"),
               "error": result.get("error", ""),
               "标准ID": manifest.get("standard_id", result.get("standard_id", "qa_chat_compare")),
               "标准版本": manifest.get("standard_version", result.get("standard_version", "")),
               "评测协议": recovered["evaluation_profile"],
               "协议包修订": manifest.get("bundle_revision", result.get("bundle_revision")),
               "结果Schema版本": manifest.get("result_schema_version", result.get("result_schema_version", "")),
               "协议恢复诊断": recovered["diagnostics"], "产品数量": count}
        case = Case(index, item, result,
                    "failed" if result.get("error") else "unfinished" if result.get("评估状态") == "待评估" else "done",
                    count, manifest.get("standard_version", "1.0"), manifest.get("bundle_revision", ""), "qa_chat_compare")
        from .history import _judge_export_fields
        row.update(_judge_export_fields(result))
        for product in range(1, count + 1):
            prefix, label = f"answer{product}", f"产品{product}"
            row[f"{label}背景"] = item.get(f"context{product}", result.get(f"context{product}", ""))
            row[f"{label}回答"] = item.get(prefix, result.get(prefix, ""))
            row[f"{label}输入状态"] = (result.get(f"{prefix}_input_status") or "未知") if shared_output else result.get(f"{prefix}_input_status")
            for gate, name in (("response", "响应体验"), ("safety", "安全稳定")):
                row[f"{label}{name}Gate"] = result.get(f"{prefix}_{gate}_gate")
                row[f"{label}{name}理由"] = result.get(f"{prefix}_{gate}_gate_reason", "")
        for dimension in dimensions:
            key, name = dimension["key"], dimension["label"]
            if shared_output:
                row[f"{name}理由（维度共享）"] = result.get(f"{key}_reason", "")
                row[f"{name}证据（维度共享）"] = result.get(f"{key}_evidence", [])
            for product in range(1, count + 1):
                prefix, label = f"answer{product}_{key}", f"产品{product}{name}"
                product_fields = [("applicable", "是否适用"), ("verification_status", "核验状态")]
                if not shared_output:
                    product_fields.extend((("primary_issue", "主要问题"), ("reason", "理由"), ("evidence", "证据")))
                for suffix, title in product_fields:
                    row[f"{label}{title}"] = result.get(f"{prefix}_{suffix}")
                state = result.get(f"{prefix}_score_status")
                row[f"{label}评分状态"] = STATUS_LABELS.get(state, state) or ("评估失败" if result.get("error") else "待评估")
                display_state, value = score_state(case, product, key, protocol) if protocol else ("协议身份异常", None)
                row[f"{label}分"] = value
                if state == "scored" and value is None:
                    row[f"{label}评分状态"] = display_state
                    row[f"{label}原始异常分"] = result.get(f"{prefix}_score")
            row[f"{name}排名组"] = result.get(f"{key}_rank_groups", []) if recovered["executable"] else []
        audit_fields = [("needs_human_review", "是否需要人工复核"), ("review_reasons", "人工复核原因"),
                        ("prompt_sha256", "Prompt SHA256"), ("input_manifest_sha256", "输入指纹")]
        if shared_output:
            audit_fields.extend((("trusted_collection_metadata", "可信采集元数据"),
                                 ("normalization_diagnostics", "状态规范化诊断"),
                                 ("structural_diagnostics", "结构诊断")))
        else:
            audit_fields = [("has_conflict", "内容冲突"), ("conflict_reason", "内容冲突理由"),
                            ("needs_human_review", "是否需要人工复核"), ("review_reasons", "人工复核原因"),
                            ("rationale", "理由"), ("confidence", "置信度"), ("evidence", "证据"),
                            ("prompt_sha256", "Prompt SHA256"), ("input_manifest_sha256", "输入指纹")]
        for key, name in audit_fields:
            row[name] = result.get(key)
        if recovered["diagnostics"]:
            row["原始结果（协议待恢复）"] = result
        rows.append(row)
    return rows
