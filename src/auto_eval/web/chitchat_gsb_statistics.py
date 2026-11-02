"""Direct categorical GSB statistics without invented scores or total ranks."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations

from .protocol_identity import recover_protocol_identity


STANDARD_ID = "chitchat_gsb_compare"
SCHEMA_VERSION = "chitchat-gsb-result-1"
STATUS_LABELS = {
    "evaluated": "已评", "insufficient_evidence": "待补证据", "needs_review": "待复核",
    "pending": "未完成", "failed": "评估失败", "invalid_state": "状态异常",
    "identity_error": "协议身份异常", "inactive_pair": "非活动产品对",
}


def gsb_pair_state(case, a: int, b: int, protocol=None) -> tuple[str, dict | None]:
    """Only a valid, frozen pair judgment enters a G/S/B denominator."""
    if not 1 <= a < b <= case.product_count:
        return "inactive_pair", None
    if case.status != "done":
        return ("failed" if case.status == "failed" else "pending"), None
    if case.identity_diagnostic or case.standard_id != STANDARD_ID:
        return "identity_error", None
    if protocol is not None and (protocol.standard_id != STANDARD_ID or
                                 protocol.result_schema_version != SCHEMA_VERSION):
        return "identity_error", None
    row = case.result
    if row.get("result_schema_version") not in (None, SCHEMA_VERSION):
        return "identity_error", None
    if row.get("product_count") not in (None, case.product_count):
        return "invalid_state", None
    pairs = row.get("gsb_pairs")
    value = pairs.get(f"{a}_{b}") if isinstance(pairs, dict) else None
    if not isinstance(value, dict):
        return "invalid_state", None
    if any(type(value.get(key)) is not int or value[key] != expected
           for key, expected in (("product_a", a), ("product_b", b))):
        return "invalid_state", None
    if (not isinstance(value.get("reason"), str) or not value["reason"].strip() or
            value.get("serious_issue") not in ("none", "A", "B", "both")):
        return "invalid_state", None
    state, gsb = value.get("status"), value.get("gsb")
    if state == "evaluated" and gsb not in ("G", "S", "B"):
        return "invalid_state", None
    if state == "evaluated" and ((value["serious_issue"] == "A" and gsb != "B") or
                                 (value["serious_issue"] == "B" and gsb != "G")):
        return "invalid_state", None
    if state not in ("evaluated", "insufficient_evidence", "needs_review"):
        return "invalid_state", None
    if state != "evaluated" and gsb is not None:
        return "invalid_state", None
    trusted = row.get("trusted_collection_metadata") or {}
    if not isinstance(trusted, dict) or any(
            trusted.get(f"answer{p}") is not None and not isinstance(trusted[f"answer{p}"], dict)
            for p in (a, b)):
        return "invalid_state", None
    if any(row.get(f"answer{p}_input_status") == "failed" or
           (trusted.get(f"answer{p}") or {}).get("input_status") == "failed" for p in (a, b)):
        return "insufficient_evidence", {**value, "status": "insufficient_evidence", "gsb": None}
    return state, value


def pair_metrics(cases, a: int, b: int, protocol=None) -> dict:
    present = [case for case in cases if case.product_count >= b]
    states = [gsb_pair_state(case, a, b, protocol) for case in present]
    counts = Counter(state for state, _ in states)
    valid = [value for state, value in states if state == "evaluated"]
    decisions = Counter(value["gsb"] for value in valid)
    n, total = len(valid), len(present)
    observed = [value for state, value in states if value is not None]
    issues = Counter(value["serious_issue"] for value in observed)
    result = {
        "product_a": a, "product_b": b, "total": total, "eligible": total,
        "valid_count": n, "evaluated": n,
        "coverage": n / total if total else None,
        "G": decisions["G"], "S": decisions["S"], "B": decisions["B"],
        "g_rate": decisions["G"] / n if n else None,
        "s_rate": decisions["S"] / n if n else None,
        "b_rate": decisions["B"] / n if n else None,
        "net_win_rate": (decisions["G"] - decisions["B"]) / n if n else None,
        "serious_issue_counts": {key: issues[key] for key in ("none", "A", "B", "both")},
    }
    result.update({state: counts[state] for state in STATUS_LABELS if state not in ("evaluated", "inactive_pair")})
    return result


def all_pair_metrics(cases, protocol=None) -> dict:
    count = max((case.product_count for case in cases), default=2)
    return {f"{a}_{b}": pair_metrics(cases, a, b, protocol)
            for a, b in combinations(range(1, count + 1), 2)}


def _pair_tables(cases, snapshot: dict, protocol, *, title: str = "") -> list:
    from .compare_statistics import StatisticsTable, _name
    paired = StatisticsTable(title + "整体GSB产品对比较",
        ("产品A", "产品B", "有效数", "G", "S", "B", "G占比", "S占比", "B占比", "净胜率"),
        ("text", "text", "count", "count", "count", "count", "percent", "percent", "percent", "percent"))
    coverage = StatisticsTable(title + "整体GSB覆盖与排除原因",
        ("产品A", "产品B", "全部条目", "有效数", "覆盖率", "待补证据", "待复核", "未完成", "评估失败", "状态异常", "身份异常"),
        ("text", "text", "count", "count", "percent", "count", "count", "count", "count", "count", "count"))
    for metrics in all_pair_metrics(cases, protocol).values():
        a, b = metrics["product_a"], metrics["product_b"]
        names = [_name(snapshot, a), _name(snapshot, b)]
        paired.rows.append([*names, *[metrics[key] for key in
            ("valid_count", "G", "S", "B", "g_rate", "s_rate", "b_rate", "net_win_rate")]])
        coverage.rows.append([*names, *[metrics[key] for key in
            ("total", "valid_count", "coverage", "insufficient_evidence", "needs_review", "pending", "failed", "invalid_state", "identity_error")]])
    return [paired, coverage]


def chitchat_gsb_statistics(snapshot: dict, aligned_results: list[dict]) -> list:
    from .compare_statistics import StatisticsTable, _cases, _subgroups
    from .protocol_identity import require_snapshot_protocol
    source = {**snapshot, "results": aligned_results}
    recovery = recover_protocol_identity(source)
    info = StatisticsTable("闲聊整体GSB统计", ("项目", "内容"), rows=[
        ["评测协议", recovery["evaluation_profile"]],
        ["协议包修订", recovery["manifest"].get("bundle_revision")],
        ["结果Schema版本", recovery["manifest"].get("result_schema_version")],
        ["GSB口径", "模型直接给整体相对结论，不由绝对分数推导。A相对B：G为胜，S为整体相近，B为负。净胜率=(G-B)/(G+S+B)。"],
        ["缺失与异常", "仅已评且结论有效的产品对进入分母；待补证据、待复核、未完成、技术失败、身份或状态异常不计S，不补分。零有效样本时比例为空。"],
        ["去重", "原数据集每个条目与产品对只计最新结果一次，不重复计重跑或评审票数。"],
        ["多轮", "汇总单位为目标轮次；同会话内多轮相关，不视为独立会话。按轮次和会话分组展示逐轮分布，不合成会话GSB。"],
        ["三产品", "分别统计1对2、1对3、2对3；不据两两偏好编造总分、传递排名或三方胜者。"],
    ])
    if not recovery["executable"]:
        return [info, StatisticsTable("GSB统计不可用", ("原因",), rows=[[d] for d in recovery["diagnostics"]])]
    protocol = require_snapshot_protocol(source)
    cases = _cases(snapshot, aligned_results)
    tables = [info, *_pair_tables(cases, snapshot, protocol)]
    for label, group in _subgroups(cases):
        tables.extend(_pair_tables(group, snapshot, protocol, title=label + " / "))
    tables.extend(chitchat_gsb_conversation_statistics(snapshot, aligned_results))
    return tables


def chitchat_gsb_conversation_statistics(snapshot: dict, aligned_results: list[dict]) -> list:
    """Describe target-turn decisions within each session, never a session vote."""
    from .compare_statistics import StatisticsTable, _cases, _name
    from .protocol_identity import require_snapshot_protocol
    source = {**snapshot, "results": aligned_results}
    recovery = recover_protocol_identity(source)
    if not recovery["executable"]:
        return []
    cases = _cases(snapshot, aligned_results)
    sessions = defaultdict(list)
    turns = defaultdict(list)
    diagnostics = []
    for case in cases:
        if case.statistic_unit.startswith("conversation:"):
            sessions[str(case.item["session_id"])].append(case)
            turns[case.item["turn_index"]].append(case)
        elif case.conversation_diagnostic:
            diagnostics.append([case.item.get("id") or case.index, case.conversation_diagnostic])
    if not sessions and not diagnostics:
        return []
    protocol = require_snapshot_protocol(source)
    coverage = StatisticsTable("GSB多轮会话覆盖", ("会话ID", "输入轮数", "已完成轮数", "待评估轮数", "评估失败轮数", "说明"))
    grouped = StatisticsTable("GSB会话内逐轮分布（不合成为会话胜负）",
        ("会话ID", "产品A", "产品B", "输入轮数", "有效轮数", "G", "S", "B", "待补证据", "待复核", "逐轮净胜率"),
        ("text", "text", "text", "count", "count", "count", "count", "count", "count", "count", "percent"))
    indexed = StatisticsTable("GSB各目标轮次分布",
        ("目标轮次", "产品A", "产品B", "有效轮数", "G", "S", "B", "净胜率"),
        ("count", "text", "text", "count", "count", "count", "count", "percent"))
    for session, cohort in sorted(sessions.items()):
        coverage.rows.append([session, len(cohort), sum(c.status == "done" for c in cohort),
            sum(c.status not in ("done", "failed") for c in cohort), sum(c.status == "failed" for c in cohort),
            "；".join(sorted({c.conversation_diagnostic for c in cohort if c.conversation_diagnostic})) or
            "只统计已导入目标轮次，不推断未导入轮次，不生成会话GSB"])
        for value in all_pair_metrics(cohort, protocol).values():
            grouped.rows.append([session, _name(snapshot, value["product_a"]), _name(snapshot, value["product_b"]),
                *[value[k] for k in ("total", "valid_count", "G", "S", "B", "insufficient_evidence", "needs_review", "net_win_rate")]])
    for turn, cohort in sorted(turns.items()):
        for value in all_pair_metrics(cohort, protocol).values():
            indexed.rows.append([turn, _name(snapshot, value["product_a"]), _name(snapshot, value["product_b"]),
                *[value[k] for k in ("valid_count", "G", "S", "B", "net_win_rate")]])
    result = [coverage, grouped, indexed]
    if diagnostics:
        result.append(StatisticsTable("GSB会话分组诊断", ("条目", "原因"), rows=diagnostics))
    return result
