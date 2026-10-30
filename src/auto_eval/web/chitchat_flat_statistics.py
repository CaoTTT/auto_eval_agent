"""Independent seven-dimension scores, missing evidence and exact mean ratios."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from statistics import mean

from ..judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_DIMENSIONS as DIMENSIONS,
    CHITCHAT_FLAT_DIMENSION_NAMES as DIMENSION_NAMES,
)
from .protocol_identity import recover_protocol_identity, require_snapshot_protocol

ALLOWED_SCORES = {d: (1, 5) if i < 2 else (1, 2, 3, 4, 5)
                  for i, d in enumerate(DIMENSIONS)}

STATUS_LABELS = {
    "scored": "有效评分", "unverifiable": "证据不足",
    "input_failed": "输入失败", "technical_failure": "技术失败",
    "unfinished": "未完成", "invalid_state": "评分状态冲突",
    "identity_error": "协议身份异常", "inactive_product": "非活动产品",
}


def _valid_score(value, dimension: str) -> bool:
    return type(value) is int and value in ALLOWED_SCORES.get(dimension, ())


def chitchat_flat_score_state(case, product: int, dimension: str, protocol=None):
    """Preserve independent scores; response/safety never clear other dimensions."""
    if case.status != "done":
        return ("technical_failure" if case.status == "failed" else "unfinished"), None
    if case.identity_diagnostic or case.standard_id != "chitchat_evidence_first_flat":
        return "identity_error", None
    if product not in range(1, case.product_count + 1):
        return "inactive_product", None
    if dimension not in DIMENSIONS:
        return "invalid_state", None
    row = case.result
    if row.get(f"answer{product}_input_status") == "failed" or row.get(f"answer{product}_execution_status") == "input_failed":
        return "input_failed", None
    if protocol is not None and (protocol.standard_id != "chitchat_evidence_first_flat" or
                                protocol.result_schema_version != "chitchat-flat-result-1"):
        return "identity_error", None
    if row.get("result_schema_version") not in (None, "chitchat-flat-result-1"):
        return "identity_error", None
    prefix = f"answer{product}_{dimension}"
    if prefix + "_score" not in row:
        return "invalid_state", None
    score = row[prefix + "_score"]
    reason = row.get(dimension + "_reason")
    if not isinstance(reason, str) or not reason.strip():
        return "invalid_state", None
    state = row.get(prefix + "_score_status", "unverifiable" if score is None else "scored")
    if state == "scored" and _valid_score(score, dimension):
        return "scored", score
    if state == "unverifiable" and score is None:
        return "unverifiable", None
    return "invalid_state", None


def dimension_metrics(cases, product: int, dimension: str, protocol=None) -> dict:
    present = [case for case in cases if product <= case.product_count]
    states = [chitchat_flat_score_state(case, product, dimension, protocol) for case in present]
    values = [score for _, score in states if score is not None]
    counts = dict(Counter(state for state, _ in states))
    total = len(present)
    # Every submitted dimension applies; missing evidence remains a gap.
    eligible = total
    return {
        "avg": mean(values) if values else None,
        "scored": len(values), "eligible": eligible, "applicable": eligible,
        "applicable_unknown": counts.get("unfinished", 0) + counts.get("input_failed", 0),
        "score_coverage": len(values) / eligible if eligible else None,
        "total": total, "score_status_counts": counts,
        "score_status_rates": {state: counts.get(state, 0) / total if total else None for state in STATUS_LABELS},
        "score_distribution": {str(score): values.count(score) for score in ALLOWED_SCORES[dimension]},
    }


def _tables(cases, snapshot, protocol, title=""):
    from .compare_statistics import StatisticsTable, _name
    count = max((case.product_count for case in cases), default=2)
    overview = StatisticsTable(title + "各产品维度覆盖与评分",
        ("维度", "产品", "样本数", "有效评分", "应评或待核", "评分覆盖率", "均分",
         "证据不足", "输入失败", "技术失败", "未完成", "状态异常", "身份异常"),
        ("text", "text", "count", "count", "count", "percent", "decimal",
         "count", "count", "count", "count", "count", "count"))
    distribution = StatisticsTable(title + "各维度分数分布",
        ("维度", "产品", "分值", "数量", "有效评分分母", "占比"),
        ("text", "text", "decimal", "count", "count", "percent"))
    paired = StatisticsTable(title + "同题同维共同可评分交集",
        ("维度", "产品A", "产品B", "配对有效题数", "A交集均分", "B交集均分", "A减B均分", "A/B交集均分比"),
        ("text", "text", "text", "count", "decimal", "decimal", "decimal", "percent"))
    common = StatisticsTable(title + "全部产品共同可评分交集",
        ("维度", "产品数量", "产品", "共同有效题数", "交集均分"),
        ("text", "count", "text", "count", "decimal"))
    ratios = StatisticsTable(title + "各产品有效均分比",
        ("维度", "产品A", "产品B", "A有效数", "B有效数", "A均分", "B均分", "A/B均分比"),
        ("text", "text", "text", "count", "count", "decimal", "decimal", "percent"))
    for dimension in DIMENSIONS:
        for a, b in combinations(range(1, count + 1), 2):
            ma, mb = (dimension_metrics(cases, p, dimension, protocol) for p in (a, b))
            ratios.rows.append([DIMENSION_NAMES[dimension], _name(snapshot, a), _name(snapshot, b),
                                ma["scored"], mb["scored"], ma["avg"], mb["avg"],
                                ma["avg"] / mb["avg"] if ma["avg"] is not None and mb["avg"] else None])
        for product in range(1, count + 1):
            metrics = dimension_metrics(cases, product, dimension, protocol)
            states = metrics["score_status_counts"]
            overview.rows.append([DIMENSION_NAMES[dimension], _name(snapshot, product), metrics["total"],
                metrics["scored"], metrics["eligible"], metrics["score_coverage"], metrics["avg"],
                *[states.get(state, 0) for state in ("unverifiable",
                    "input_failed", "technical_failure", "unfinished", "invalid_state", "identity_error")]])
            for score in ALLOWED_SCORES[dimension]:
                n = metrics["score_distribution"][str(score)]
                distribution.rows.append([DIMENSION_NAMES[dimension], _name(snapshot, product), score,
                    n, metrics["scored"], n / metrics["scored"] if metrics["scored"] else None])
        for a, b in combinations(range(1, count + 1), 2):
            values = [(chitchat_flat_score_state(case, a, dimension, protocol)[1],
                       chitchat_flat_score_state(case, b, dimension, protocol)[1])
                      for case in cases if case.product_count >= b]
            values = [(x, y) for x, y in values if x is not None and y is not None]
            x, y = (mean(v[0] for v in values), mean(v[1] for v in values)) if values else (None, None)
            paired.rows.append([DIMENSION_NAMES[dimension], _name(snapshot, a), _name(snapshot, b),
                                len(values), x, y, x - y if values else None, x / y if x is not None and y else None])
        for products in sorted({case.product_count for case in cases}):
            values = [[chitchat_flat_score_state(case, p, dimension, protocol)[1] for p in range(1, products + 1)]
                      for case in cases if case.product_count == products]
            values = [scores for scores in values if all(score is not None for score in scores)]
            for product in range(1, products + 1):
                common.rows.append([DIMENSION_NAMES[dimension], products, _name(snapshot, product), len(values),
                                    mean(scores[product - 1] for scores in values) if values else None])
    return [overview, distribution, ratios, paired, common]


def chitchat_flat_statistics(snapshot: dict, aligned_results: list[dict]):
    from .compare_statistics import StatisticsTable, _cases, _subgroups
    recovered = recover_protocol_identity({**snapshot, "results": aligned_results})
    cases = _cases(snapshot, aligned_results)
    info = StatisticsTable("闲聊七维评估统计", ("项目", "内容"), rows=[
        ["评测协议", recovered["evaluation_profile"]],
        ["协议包修订", recovered["manifest"].get("bundle_revision")],
        ["总提交数", len(cases)], ["结构化完成数", sum(case.status == "done" for case in cases)],
        ["技术失败数", sum(case.status == "failed" for case in cases)],
        ["待评估数", sum(case.status not in ("done", "failed") for case in cases)],
        ["评分", "响应体验/安全稳定仅1、5；其余五维1—5整数；空分不补0。安全或响应低分不级联清空其他维度。"],
        ["覆盖率", "有效评分数 / 该产品全部提交数；缺证、失败及未完成保留覆盖缺口。"],
        ["比较", "单维逐产品汇总；配对只取同题同维双方有效交集，全产品交集单列；不计算跨维总分、胜率或排名。"],
        ["理由", "证据位置或短原话、实际表现及影响、分档依据在同一维度理由内呈现，不另造证据数组。"],
    ])
    if not recovered["executable"]:
        return [info, StatisticsTable("评分统计不可用", ("原因",), rows=[[value] for value in recovered["diagnostics"]])]
    protocol = require_snapshot_protocol({**snapshot, "results": aligned_results})
    result = [info, *_tables(cases, snapshot, protocol)]
    for label, group in _subgroups(cases):
        result.extend(_tables(group, snapshot, protocol, label + " / "))
    result.extend(chitchat_flat_conversation_statistics(snapshot, aligned_results))
    return result


def chitchat_flat_conversation_statistics(snapshot: dict, aligned_results: list[dict]):
    """Report per-dimension session means; never mix different dimensions."""
    from .compare_statistics import StatisticsTable, _cases, _name
    cases = _cases(snapshot, aligned_results)
    if not any(case.statistic_unit.startswith("conversation:") or case.conversation_diagnostic for case in cases):
        return []
    recovered = recover_protocol_identity({**snapshot, "results": aligned_results})
    if not recovered["executable"]:
        return []
    protocol = require_snapshot_protocol({**snapshot, "results": aligned_results})
    sessions = defaultdict(list)
    for case in cases:
        sessions[case.statistic_unit].append(case)
    coverage = StatisticsTable("闲聊七维多轮会话覆盖", ("统计单元", "应执行轮数", "完成轮数", "完整", "诊断"))
    for key, turns in sessions.items():
        expected = max(case.item["turn_index"] for case in turns) if key.startswith("conversation:") else 1
        done = sum(case.status == "done" for case in turns)
        coverage.rows.append([turns[0].item.get("session_id") if key.startswith("conversation:") else key,
                              expected, done, done == expected,
                              "；".join(sorted({case.conversation_diagnostic for case in turns if case.conversation_diagnostic}))])
    products = StatisticsTable("闲聊七维会话与独立题等权均值",
        ("维度", "产品数量", "产品", "有效单元数", "有效轮数", "单元等权均分"),
        ("text", "count", "text", "count", "count", "decimal"))
    paired = StatisticsTable("闲聊七维会话同题配对等权均值",
        ("维度", "产品数量", "产品A", "产品B", "有效单元数", "配对轮数", "A单元等权均分", "B单元等权均分"),
        ("text", "count", "text", "text", "count", "count", "decimal", "decimal"))
    for count in sorted({case.product_count for case in cases}):
        cohort = [case for case in cases if case.product_count == count]
        for dimension in DIMENSIONS:
            for product in range(1, count + 1):
                units = defaultdict(list)
                for case in cohort:
                    score = chitchat_flat_score_state(case, product, dimension, protocol)[1]
                    if score is not None:
                        units[case.statistic_unit].append(score)
                products.rows.append([DIMENSION_NAMES[dimension], count, _name(snapshot, product), len(units),
                                      sum(map(len, units.values())), mean(mean(v) for v in units.values()) if units else None])
            for a, b in combinations(range(1, count + 1), 2):
                units = defaultdict(list)
                for case in cohort:
                    x = chitchat_flat_score_state(case, a, dimension, protocol)[1]
                    y = chitchat_flat_score_state(case, b, dimension, protocol)[1]
                    if x is not None and y is not None:
                        units[case.statistic_unit].append((x, y))
                paired.rows.append([DIMENSION_NAMES[dimension], count, _name(snapshot, a), _name(snapshot, b), len(units),
                                    sum(map(len, units.values())),
                                    mean(mean(x for x, _ in v) for v in units.values()) if units else None,
                                    mean(mean(y for _, y in v) for v in units.values()) if units else None])
    return [coverage, products, paired]
