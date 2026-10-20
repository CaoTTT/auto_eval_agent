"""R19 statistics for the independent, three-point timeliness protocol."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from statistics import mean

from .protocol_identity import recover_protocol_identity, require_snapshot_protocol

DIMENSIONS = ("understanding", "authority", "timeliness")
DIMENSION_NAMES = dict(zip(DIMENSIONS, ("理解需求", "权威性", "时效性")))
STATUS_LABELS = {
    "scored": "有效评分", "not_applicable": "维度不适用",
    "insufficient_evidence": "证据不足", "blocked_gate": "Gate阻断",
    "blocked_understanding": "理解需求阻断", "input_failed": "输入失败",
    "technical_failure": "技术失败", "unfinished": "未完成",
    "invalid_state": "评分状态冲突", "identity_error": "协议身份异常",
    "inactive_product": "非活动产品",
}
GATES = ("pass", "fail", "unclear")


def _simplified_score_state(row: dict, product: int, dimension: str) -> tuple[str, int | None]:
    """Validate program-derived states without inventing model verification."""
    prefix = f"answer{product}_{dimension}"
    if any(prefix + suffix not in row for suffix in ("_score", "_applicable", "_score_status")):
        return "invalid_state", None
    score = row.get(prefix + "_score")
    applicable = row.get(prefix + "_applicable")
    recorded = row.get(prefix + "_score_status")
    na_key = prefix + "_na_reason"
    na_reason = row.get(na_key)
    gates = [row.get(f"answer{product}_{gate}_gate") for gate in ("response", "safety")]
    if any(g not in GATES for g in gates):
        return "invalid_state", None
    if "fail" in gates:
        state = "blocked_gate"
        valid = applicable is None and score is None and na_key not in row
    else:
        understanding = (None, None)
        if dimension != "understanding":
            understanding = _simplified_score_state(row, product, "understanding")
            if understanding[0] == "invalid_state":
                return "invalid_state", None
        if understanding == ("scored", 1):
            state = "blocked_understanding"
            valid = applicable is None and score is None and na_key not in row
        elif type(score) is int and 1 <= score <= 3:
            state = "scored"
            valid = applicable is True and na_key not in row
        elif score is None and dimension == "understanding":
            state = "insufficient_evidence"
            valid = applicable is True and na_key not in row
        elif score is None and na_reason == "not_applicable":
            state = "not_applicable"
            valid = applicable is False
        elif score is None and na_reason in ("insufficient_evidence", "applicability_unknown"):
            state = "insufficient_evidence"
            valid = (applicable is True if na_reason == "insufficient_evidence" else applicable is None)
        else:
            return "invalid_state", None
    if not valid or recorded != state:
        return "invalid_state", None
    return state, score if state == "scored" else None


def timeliness_score_state(case, product: int, dimension: str, protocol=None) -> tuple[str, int | None]:
    """Do not upgrade malformed histories or treat a missing score as zero."""
    if case.status != "done":
        return ("technical_failure" if case.status == "failed" else "unfinished"), None
    if case.identity_diagnostic:
        return "identity_error", None
    if product not in range(1, case.product_count + 1):
        return "inactive_product", None
    row = case.result
    prefix = f"answer{product}_{dimension}"
    if row.get(f"answer{product}_input_status") == "failed":
        return "input_failed", None
    schema = protocol.result_schema_version if protocol else row.get("result_schema_version")
    if schema == "timeliness-result-2":
        return _simplified_score_state(row, product, dimension)
    gates = [row.get(f"answer{product}_{gate}_gate") for gate in ("response", "safety")]
    applicable = row.get(prefix + "_applicable")
    verification = row.get(prefix + "_verification_status")
    score = row.get(prefix + "_score")
    recorded = row.get(prefix + "_score_status")
    if any(g not in GATES for g in gates):
        return "invalid_state", None
    if "fail" in gates:
        state = "blocked_gate"
        valid = applicable is None and verification is None and score is None
    elif dimension != "understanding" and row.get(f"answer{product}_understanding_score") == 1:
        state = "blocked_understanding"
        valid = applicable is None and verification is None and score is None
    elif applicable is False:
        state = "not_applicable"
        valid = dimension != "understanding" and verification == "not_required" and score is None
    elif verification == "unverifiable":
        state = "insufficient_evidence"
        valid = score is None and (applicable is True or (dimension != "understanding" and applicable is None))
    else:
        state = "scored"
        valid = applicable is True and type(score) is int and 1 <= score <= 3 and (
            verification == "verified" or
            verification == "partial" and (dimension == "understanding" or score == 1))
    # Every saved result carries the finalizer's status. Missing or conflicting
    # status is not inferred into a formal score on reload.
    if not valid or recorded != state:
        return "invalid_state", None
    return state, score if state == "scored" else None


def gate_observable(case, product: int) -> bool:
    return (case.status == "done" and not case.identity_diagnostic and
            product <= case.product_count and case.result.get(f"answer{product}_input_status") != "failed")


def dimension_metrics(cases, product: int, dimension: str, protocol) -> dict:
    present = [case for case in cases if product <= case.product_count]
    states = [(case, *timeliness_score_state(case, product, dimension, protocol)) for case in present]
    values = [score for _, _, score in states if score is not None]
    eligible = sum(state in ("scored", "insufficient_evidence") and
                   case.result.get(f"answer{product}_{dimension}_applicable") is True
                   for case, state, _ in states)
    unknown = sum(state == "insufficient_evidence" and
                  case.result.get(f"answer{product}_{dimension}_applicable") is None
                  for case, state, _ in states)
    counts = dict(Counter(state for _, state, _ in states))
    total = len(present)
    return {"avg": round(mean(values), 3) if values else None, "scored": len(values),
            "eligible": eligible, "applicable": eligible, "applicable_unknown": unknown,
            "score_coverage": len(values) / eligible if eligible else None,
            "total": total, "score_status_counts": counts,
            "score_status_rates": {state: counts.get(state, 0) / total if total else None for state in STATUS_LABELS}}


def timeliness_statistics(snapshot: dict, aligned_results: list[dict]):
    """No thresholds, weighted totals, win rates or ranks are in this standard."""
    from .compare_statistics import StatisticsTable, _cases, _subgroups
    recovered = recover_protocol_identity({**snapshot, "results": aligned_results})
    cases = _cases(snapshot, aligned_results)
    info = StatisticsTable("时效性评估统计", ("项目", "内容"), rows=[
        ["评测协议", recovered["evaluation_profile"]],
        ["协议包修订", recovered["manifest"].get("bundle_revision")],
        ["总提交数", len(cases)], ["结构化完成数", sum(c.status == "done" for c in cases)],
        ["技术失败数", sum(c.status == "failed" for c in cases)],
        ["待评估数", sum(c.status not in ("done", "failed") for c in cases)],
        ["需人工复核数", sum(bool(c.result.get("needs_human_review")) for c in cases)],
        ["评分", "只汇总1—3整数；NA不计0。均分不证明现实最新或事实已独立核实。"],
        ["覆盖率", "有效评分数 / 适用且未被前置阻断的样本数；缺证计入分母。适用性未知另列。"],
        ["比较", "配对与全产品共同可评分交集分别报告；不提供总分、胜率或排名。"],
        ["时间", "按原输入实际测试时间解释今天；运行时间不替代测试时间。"],
    ])
    if not recovered["executable"]:
        return [info, StatisticsTable("评分统计不可用", ("原因",), rows=[[v] for v in recovered["diagnostics"]])]
    protocol = require_snapshot_protocol({**snapshot, "results": aligned_results})
    names = (snapshot.get("options") or {}).get("product_names") or []
    def name(p):
        return names[p - 1] if p <= len(names) and names[p - 1] else f"产品{p}"
    def tables(group, title=""):
        count = max((c.product_count for c in group), default=2)
        overview = StatisticsTable(title + "各产品维度覆盖与评分",
            ("维度", "产品", "样本数", "有效评分", "适用未阻断", "评分覆盖率", "均分", "适用性未知",
             "不适用", "证据不足", "Gate阻断", "理解阻断", "输入失败", "技术失败", "未完成", "状态异常",
             "不适用占全部样本", "Gate阻断占全部样本", "理解阻断占全部样本"),
            ("text", "text", "count", "count", "count", "percent", "decimal", "count",
             "count", "count", "count", "count", "count", "count", "count", "count",
             "percent", "percent", "percent"))
        pair = StatisticsTable(title + "同题同维共同可评分交集",
            ("维度", "产品A", "产品B", "配对有效题数", "A交集均分", "B交集均分", "A减B均分"),
            ("text", "text", "text", "count", "decimal", "decimal", "decimal"))
        common = StatisticsTable(title + "全部产品共同可评分交集",
            ("维度", "产品数量", "产品", "共同有效题数", "交集均分"),
            ("text", "count", "text", "count", "decimal"))
        gate = StatisticsTable(title + "Gate三态（独立于评分样本）",
            ("产品", "Gate", "可观察数", "通过", "不通过", "不确定", "状态缺失", "理解1分"),
            ("text", "text", "count", "count", "count", "count", "count", "count"))
        for p in range(1, count + 1):
            observed = [c for c in group if gate_observable(c, p)]
            for g in ("response", "safety"):
                counts = Counter(c.result.get(f"answer{p}_{g}_gate") for c in observed)
                gate.rows.append([name(p), "响应体验" if g == "response" else "安全稳定", len(observed),
                                  counts["pass"], counts["fail"], counts["unclear"],
                                  sum(v for k, v in counts.items() if k not in GATES),
                                  sum(timeliness_score_state(c, p, "understanding", protocol)[1] == 1 for c in group)])
        for dim in DIMENSIONS:
            for p in range(1, count + 1):
                m = dimension_metrics(group, p, dim, protocol)
                states = m["score_status_counts"]
                overview.rows.append([DIMENSION_NAMES[dim], name(p), sum(c.product_count >= p for c in group),
                    m["scored"], m["eligible"], m["score_coverage"], m["avg"], m["applicable_unknown"],
                    *[states.get(s, 0) for s in ("not_applicable", "insufficient_evidence", "blocked_gate",
                       "blocked_understanding", "input_failed", "technical_failure", "unfinished", "invalid_state")],
                    *[m["score_status_rates"][s] for s in ("not_applicable", "blocked_gate", "blocked_understanding")]])
            for a, b in combinations(range(1, count + 1), 2):
                values = [(timeliness_score_state(c, a, dim, protocol)[1],
                           timeliness_score_state(c, b, dim, protocol)[1]) for c in group if c.product_count >= b]
                values = [(x, y) for x, y in values if x is not None and y is not None]
                x, y = (mean(v[0] for v in values), mean(v[1] for v in values)) if values else (None, None)
                pair.rows.append([DIMENSION_NAMES[dim], name(a), name(b), len(values), x, y, x - y if values else None])
            for n in sorted({c.product_count for c in group}):
                valid = [[timeliness_score_state(c, p, dim, protocol)[1] for p in range(1, n + 1)]
                         for c in group if c.product_count == n]
                valid = [row for row in valid if all(v is not None for v in row)]
                for p in range(1, n + 1):
                    common.rows.append([DIMENSION_NAMES[dim], n, name(p), len(valid),
                                        mean(row[p - 1] for row in valid) if valid else None])
        return [overview, pair, common, gate]
    result = [info, *tables(cases)]
    for label, group in _subgroups(cases):
        result.extend(tables(group, label + " / "))
    result.extend(timeliness_conversation_statistics(snapshot, aligned_results))
    return result


def timeliness_conversation_statistics(snapshot: dict, aligned_results: list[dict]):
    """Equal session weights use valid paired turns without product rankings."""
    from .compare_statistics import StatisticsTable, _cases
    cases = _cases(snapshot, aligned_results)
    if not any(c.statistic_unit.startswith("conversation:") or c.conversation_diagnostic for c in cases):
        return []
    recovered = recover_protocol_identity({**snapshot, "results": aligned_results})
    if not recovered["executable"]:
        return []
    protocol = require_snapshot_protocol({**snapshot, "results": aligned_results})
    sessions = defaultdict(list)
    for case in cases:
        sessions[case.statistic_unit].append(case)
    coverage = StatisticsTable("时效性多轮会话覆盖", ("统计单元", "应执行轮数", "完成轮数", "完整", "诊断"))
    for key, turns in sessions.items():
        expected = max(c.item["turn_index"] for c in turns) if key.startswith("conversation:") else 1
        done = sum(c.status == "done" for c in turns)
        coverage.rows.append([turns[0].item.get("session_id") if key.startswith("conversation:") else key,
                              expected, done, done == expected,
                              "；".join(sorted({c.conversation_diagnostic for c in turns if c.conversation_diagnostic}))])
    products = StatisticsTable("时效性会话与独立题等权均值",
        ("维度", "产品数量", "产品", "有效单元数", "有效轮数", "单元等权均分"),
        ("text", "count", "text", "count", "count", "decimal"))
    paired = StatisticsTable("时效性会话同题配对等权均值",
        ("维度", "产品数量", "产品A", "产品B", "有效单元数", "配对轮数", "A单元等权均分", "B单元等权均分"),
        ("text", "count", "text", "text", "count", "count", "decimal", "decimal"))
    for count in sorted({c.product_count for c in cases}):
        cohort = [c for c in cases if c.product_count == count]
        for dimension in DIMENSIONS:
            for p in range(1, count + 1):
                units = defaultdict(list)
                for case in cohort:
                    _, score = timeliness_score_state(case, p, dimension, protocol)
                    if score is not None:
                        units[case.statistic_unit].append(score)
                products.rows.append([DIMENSION_NAMES[dimension], count, f"产品{p}", len(units),
                                      sum(map(len, units.values())), mean(mean(v) for v in units.values()) if units else None])
            for a, b in combinations(range(1, count + 1), 2):
                units = defaultdict(list)
                for case in cohort:
                    x = timeliness_score_state(case, a, dimension, protocol)[1]
                    y = timeliness_score_state(case, b, dimension, protocol)[1]
                    if x is not None and y is not None:
                        units[case.statistic_unit].append((x, y))
                paired.rows.append([DIMENSION_NAMES[dimension], count, f"产品{a}", f"产品{b}", len(units),
                                    sum(map(len, units.values())),
                                    mean(mean(x for x, _ in v) for v in units.values()) if units else None,
                                    mean(mean(y for _, y in v) for v in units.values()) if units else None])
    return [coverage, products, paired]
