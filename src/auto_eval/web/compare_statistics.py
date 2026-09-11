"""Read-only comparison statistics from the exported, latest-per-case snapshot.

Scores are ordinal protocol outputs. Missing scores never become zero/full marks.
Pairwise statistics always use the same cases on both sides; three-way ranks use
the three-way intersection. No model calls, product totals or accuracy rankings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from statistics import mean, median
from typing import Any

import numpy as np

from ..judges.compare_protocols import CompareProtocol, DIMENSIONS, resolve_compare_protocol


SHEET_NAME = "对比评估统计"
STATISTICS_VERSION = "compare-statistics-1.0"
DIMENSION_NAMES = dict(zip(DIMENSIONS, (
    "理解需求", "内容准确性", "服务闭环", "场景化满足", "直观高效", "有理有据", "引导推荐",
)))
SCORE_DIMENSIONS = tuple(d for d in DIMENSIONS if d != "accuracy")
# Reporting thresholds, not a conversion between the two scoring standards.
THRESHOLDS = {"0.2-simplified": (4, 2), "0.3": (2, 1)}
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260911
GATE_STATES = ("pass", "fail", "unclear")
VERIFY_STATES = ("verified", "partial", "unverifiable", "not_required")
STATE_NAMES = {
    "complete": "完整", "partial": "部分完整", "failed": "输入失败",
    "pass": "通过", "fail": "不通过", "unclear": "不确定",
    "verified": "已核验", "unverifiable": "无法核验", "not_required": "无需外部核验",
}


@dataclass
class StatisticsTable:
    title: str
    headers: tuple[str, ...]
    formats: tuple[str, ...] = ()
    rows: list[list[Any]] = field(default_factory=list)


@dataclass
class Case:
    index: int
    item: dict
    result: dict
    status: str
    product_count: int
    standard: str
    revision: str


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _finite_score(value: Any, protocol: CompareProtocol) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and float(value).is_integer()
        and protocol.score_min <= value <= protocol.score_max
    )


def _product_count(item: dict, result: dict) -> int:
    for source in (item, result):
        if source.get("product_count") in (2, 3):
            return int(source["product_count"])
    source = item.get("source_data") or {}
    return 3 if any(item.get(k) or source.get(k) for k in ("video3", "screenshot3", "answer3", "context3")) else 2


def _cases(snapshot: dict, aligned_results: list[dict]) -> list[Case]:
    manifest = snapshot.get("protocol_manifest") or {}
    selected = snapshot.get("evaluation_profile") or manifest.get("id") or ""
    task_standard = manifest.get("standard_version") or (selected.split("@", 1)[-1] if "@" in selected else "")
    cases = []
    for index, item in enumerate(snapshot.get("items") or []):
        row = aligned_results[index] if index < len(aligned_results) else {}
        if row.get("error") or row.get("评估状态") == "评估失败":
            status = "failed"
        elif row.get("评估状态") == "已完成":
            status = "done"
        else:
            status = "unfinished"
        standard = row.get("standard_version") or task_standard or "未记录标准"
        revision = row.get("bundle_revision") or (
            manifest.get("bundle_revision") if standard == task_standard else None
        ) or "未记录实现版本"
        cases.append(Case(index, item, row, status, _product_count(item, row), standard, revision))
    return cases


def _policy(standard: str, revision: str) -> CompareProtocol | None:
    if standard not in THRESHOLDS:
        return None
    try:
        return resolve_compare_protocol(
            f"qa_competitor_compare@{standard}",
            None if revision == "未记录实现版本" else revision,
        )
    except ValueError:
        return None


def score_state(case: Case, product: int, dimension: str, protocol: CompareProtocol) -> tuple[str, float | None]:
    """Mutually exclusive reasons, following the frozen protocol's actual gates."""
    if case.status != "done":
        return ("技术失败" if case.status == "failed" else "未完成"), None
    row = case.result
    applicable = row.get(f"{dimension}_applicable")
    if applicable is False:
        return "维度不适用", None
    if applicable is not True:
        return "适用性状态缺失", None
    verification = row.get(f"{dimension}_verification_status")
    if verification == "unverifiable":
        return "无法核验", None
    if verification not in VERIFY_STATES:
        return "核验状态缺失", None
    input_status = row.get(f"answer{product}_input_status")
    if input_status == "failed":
        return "输入失败", None
    if input_status not in ("complete", "partial"):
        return "输入状态缺失", None
    response = row.get(f"answer{product}_response_gate")
    safety = row.get(f"answer{product}_safety_gate")
    if response not in GATE_STATES or safety not in GATE_STATES:
        return "Gate状态缺失", None
    if response == "fail" or (protocol.require_response_pass and response != "pass"):
        return "响应Gate阻断", None
    if not protocol.require_response_pass and safety == "fail":
        return "安全Gate阻断", None
    score = row.get(f"answer{product}_{dimension}_score")
    if score is None:
        return "应评分但缺失", None
    if not _finite_score(score, protocol):
        return "分数格式或范围异常", None
    return "有效评分", float(score)


def _limited(case: Case, products: tuple[int, ...], dimension: str) -> bool:
    row = case.result
    return row.get(f"{dimension}_verification_status") == "partial" or any(
        row.get(f"answer{p}_input_status") == "partial"
        or any(row.get(f"answer{p}_{gate}_gate") == "unclear" for gate in ("response", "safety"))
        or (case.item.get(f"screenshot_meta{p}") or {}).get("split_status") == "risky"
        for p in products
    )


def _cluster_key(case: Case) -> str:
    item = case.item
    source = item.get("source_data") or {}
    session = item.get("session_group", source.get("session_group"))
    if session not in (None, ""):
        return "session:" + str(session)
    query = item.get("query") or item.get("question")
    if query:
        # Conservative grouping of repeated queries, including text/image identity.
        images = [m.get("original_sha256") for m in item.get("query_image_meta", [])]
        return "query:" + json.dumps([query.strip(), images or item.get("query_images", [])], ensure_ascii=False)
    return f"case:{case.index}"


def _intervals(pairs: list[tuple[Case, float, float]]) -> tuple[Any, Any, Any, Any, str]:
    """Seeded paired cluster percentile bootstrap, bounded memory, no extra deps."""
    grouped: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for case, a, b in pairs:
        group = grouped[_cluster_key(case)]
        group[0] += a - b
        group[1] += (a > b) - (a < b)
        group[2] += 1
    if len(grouped) < 2:
        return None, None, None, None, "不足2个独立题组，未估计区间"
    # Compress identical group contributions; multinomial draws equal resampling
    # the group indices, while avoiding an O(resamples * all cases) matrix.
    counts = Counter(tuple(values) for values in grouped.values())
    values = np.asarray(list(counts), dtype=float)
    probabilities = np.asarray(list(counts.values()), dtype=float) / len(grouped)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    estimates = []
    for start in range(0, BOOTSTRAP_SAMPLES, 128):
        weights = rng.multinomial(len(grouped), probabilities, size=min(128, BOOTSTRAP_SAMPLES - start))
        sums = weights @ values
        estimates.append(sums[:, :2] / sums[:, 2, None])
    bounds = np.quantile(np.concatenate(estimates), [0.025, 0.975], axis=0)
    note = f"按{len(grouped)}个题组配对重采样"
    if np.any(bounds[0] == bounds[1]):
        note += "；区间退化，仅反映当前观测样本"
    return float(bounds[0, 0]), float(bounds[1, 0]), float(bounds[0, 1]), float(bounds[1, 1]), note


def _name(snapshot: dict, product: int) -> str:
    names = (snapshot.get("options") or {}).get("product_names") or snapshot.get("product_names") or []
    name = names[product - 1] if isinstance(names, list) and len(names) >= product else ""
    return f"产品{product}（{name.strip()}）" if isinstance(name, str) and name.strip() else f"产品{product}"


def _counts_table(title: str) -> StatisticsTable:
    return StatisticsTable(title, ("对象", "指标", "数量", "分母", "占比", "口径"),
                           ("text", "text", "count", "count", "percent", "text"))


def _count(table: StatisticsTable, subject: str, metric: str, count: int, denominator: int, note: str = "") -> None:
    table.rows.append([subject, metric, count, denominator, _ratio(count, denominator), note or ("无可统计样本" if not denominator else "")])


def _overview(cases: list[Case], snapshot: dict) -> StatisticsTable:
    table = _counts_table("A．评测执行与证据质量")
    completed = [c for c in cases if c.status == "done"]
    failed = [c for c in cases if c.status == "failed"]
    for label, count in (("总题数", len(cases)), ("结构化结果成功／覆盖率", len(completed)),
                         ("技术失败", len(failed)), ("未完成", len(cases) - len(completed) - len(failed))):
        _count(table, "任务", label, count, len(cases), "按导出时最新逐题结果；未完成不计入失败")
    for state in ("high", "medium", "low"):
        _count(table, "裁判", {"high": "高置信度", "medium": "中置信度", "low": "低置信度"}[state],
               sum(c.result.get("confidence") == state for c in completed), len(completed), "模型自报，不能解释为准确率")
    _count(table, "裁判", "置信度缺失", sum(c.result.get("confidence") not in ("high", "medium", "low") for c in completed), len(completed))
    for label, subset in (("成功结果待复核", completed), ("技术失败待复核", failed)):
        _count(table, "裁判", label, sum(bool(c.result.get("needs_human_review") or c.result.get("needs_review")) for c in subset), len(subset))
    errors = Counter(c.result.get("error_type") or "未分类技术错误" for c in failed)
    for reason, count in sorted(errors.items()):
        _count(table, "失败原因", reason, count, len(failed))
    conflicts = Counter(c.result.get("has_conflict") for c in completed)
    known = conflicts["yes"] + conflicts["no"]
    _count(table, "内容冲突", "明确冲突率", conflicts["yes"], known, "yes/(yes+no)；不归责具体产品或产品对")
    _count(table, "内容冲突", "不确定", conflicts["unclear"], len(completed))
    _count(table, "内容冲突", "状态缺失", len(completed) - known - conflicts["unclear"], len(completed))
    for product in range(1, max((c.product_count for c in cases), default=2) + 1):
        present = [c for c in completed if c.product_count >= product]
        label = _name(snapshot, product)
        for state in ("complete", "partial", "failed"):
            _count(table, label, "输入" + STATE_NAMES[state], sum(c.result.get(f"answer{product}_input_status") == state for c in present), len(present))
        _count(table, label, "输入状态缺失", sum(c.result.get(f"answer{product}_input_status") not in ("complete", "partial", "failed") for c in present), len(present))
        observable = [c for c in present if c.result.get(f"answer{product}_input_status") in ("complete", "partial")]
        for gate, gate_label in (("response", "响应Gate"), ("safety", "安全Gate")):
            for state in GATE_STATES:
                _count(table, label, gate_label + STATE_NAMES[state], sum(c.result.get(f"answer{product}_{gate}_gate") == state for c in observable), len(observable), "不包含输入failed造成的强制unclear")
            _count(table, label, gate_label + "状态缺失", sum(c.result.get(f"answer{product}_{gate}_gate") not in GATE_STATES for c in observable), len(observable))
    return table


def _dimension_tables(cases: list[Case], protocol: CompareProtocol, snapshot: dict) -> list[StatisticsTable]:
    diagnostics = _counts_table("B1．维度适用性、核验与评分覆盖")
    performance = StatisticsTable("B2．各产品维度表现（各自有效样本）",
        ("维度", "产品", "有效题数", "均分", "中位数", "达标题数", "达标率", "低质题数", "低质率", "满档率", "最低档率", "受限证据题数"),
        ("text", "text", "count", "decimal", "decimal", "count", "percent", "count", "percent", "percent", "percent", "count"))
    distribution = StatisticsTable("B3．完整分档分布", ("维度", "产品", "分值", "题数", "有效题数", "占比"),
                                   ("text", "text", "count", "count", "count", "percent"))
    missing = _counts_table("B4．评分状态分布（互斥原因）")
    done = [c for c in cases if c.status == "done"]
    count = max((c.product_count for c in cases), default=2)
    pass_score, low_score = THRESHOLDS[protocol.standard_version]
    for dimension in DIMENSIONS:
        label = DIMENSION_NAMES[dimension]
        known = [c for c in done if isinstance(c.result.get(f"{dimension}_applicable"), bool)]
        applicable = [c for c in known if c.result[f"{dimension}_applicable"]]
        _count(diagnostics, label, "维度适用率", len(applicable), len(known), "适用性仅描述需求分布；不以越高越好评价")
        _count(diagnostics, label, "适用性状态缺失", len(done) - len(known), len(done))
        for state in VERIFY_STATES:
            _count(diagnostics, label, "核验：" + ("部分核验" if state == "partial" else STATE_NAMES[state]), sum(c.result.get(f"{dimension}_verification_status") == state for c in applicable), len(applicable))
        _count(diagnostics, label, "核验状态缺失", sum(c.result.get(f"{dimension}_verification_status") not in VERIFY_STATES for c in applicable), len(applicable))
        if dimension == "accuracy":
            continue
        for p in range(1, count + 1):
            states = [score_state(c, p, dimension, protocol) for c in cases]
            valid = [(c, score) for c, (state, score) in zip(cases, states) if state == "有效评分"]
            scores = [s for _, s in valid]
            expected = sum(state in ("有效评分", "应评分但缺失", "分数格式或范围异常") for state, _ in states)
            _count(diagnostics, f"{label} / {_name(snapshot, p)}", "有效评分覆盖率", len(valid), len(applicable), "有效分/该维度适用题数；Gate阻断等原因见评分状态")
            _count(diagnostics, f"{label} / {_name(snapshot, p)}", "应评分输出完整率", len(valid), expected, "有效分/按所选协议应评分数量")
            frequencies = Counter(state for state, _ in states)
            for reason, frequency in sorted(frequencies.items()):
                _count(missing, f"{label} / {_name(snapshot, p)}", reason, frequency, len(cases))
            passed = sum(s >= pass_score for s in scores)
            low = sum(s <= low_score for s in scores)
            n = len(scores)
            performance.rows.append([label, _name(snapshot, p), n, mean(scores) if n else None, median(scores) if n else None,
                passed, _ratio(passed, n), low, _ratio(low, n), _ratio(sum(s == protocol.score_max for s in scores), n),
                _ratio(sum(s == protocol.score_min for s in scores), n), sum(_limited(c, (p,), dimension) for c, _ in valid)])
            for score in range(protocol.score_min, protocol.score_max + 1):
                frequency = sum(s == score for s in scores)
                distribution.rows.append([label, _name(snapshot, p), score, frequency, n, _ratio(frequency, n)])
    return [diagnostics, performance, distribution, missing]


def _pair_tables(cases: list[Case], protocol: CompareProtocol, snapshot: dict, *, common_three: bool = False,
                 compact: bool = False) -> list[StatisticsTable]:
    scope = "三方共同有效样本" if common_three else "两两有效样本"
    gaps = StatisticsTable(f"C1．双向分位值与配对分差（{scope}）",
        ("维度", "方向 A/B", "适用题数", "配对有效题数", "可比覆盖率", "A均分", "B均分", "分位值 A/B", "均分差 A-B", "分差95%下限", "分差95%上限", "达标率差", "低质率差", "受限证据题数", "说明"),
        ("text", "text", "count", "count", "percent", "decimal", "decimal", "percent", "decimal", "decimal", "decimal", "percent", "percent", "count", "text"))
    gsb = StatisticsTable(f"C2．双向 GSB（{scope}）",
        ("维度", "方向 A/B", "配对有效题数", "GSB数量", "G", "S", "B", "胜率", "平率", "负率", "净胜率", "净胜95%下限", "净胜95%上限"),
        ("text", "text", "count", "text", "count", "count", "count", "percent", "percent", "percent", "percent", "percent", "percent"))
    differences = StatisticsTable(f"C3．双向分差分布（{scope}）", ("维度", "方向 A/B", "分差 A-B", "题数", "配对有效题数", "占比"),
                                  ("text", "text", "count", "count", "count", "percent"))
    count = max((c.product_count for c in cases), default=2)
    pass_score, low_score = THRESHOLDS[protocol.standard_version]
    for dimension in SCORE_DIMENSIONS:
        applicable = [c for c in cases if c.status == "done" and c.result.get(f"{dimension}_applicable") is True]
        scores = {c.index: [score_state(c, p, dimension, protocol)[1] for p in range(1, count + 1)] for c in applicable}
        for first, second in combinations(range(1, count + 1), 2):
            pairs = []
            for case in applicable:
                values = scores[case.index]
                a, b = values[first - 1], values[second - 1]
                if a is not None and b is not None and (not common_three or all(s is not None for s in values)):
                    pairs.append((case, a, b))
            n = len(pairs)
            interval = _intervals(pairs) if not compact else (None, None, None, None, "分组明细仅展示点估计")
            for reverse in (False, True):
                p, q = (second, first) if reverse else (first, second)
                direction = f"{_name(snapshot, p)} / {_name(snapshot, q)}"
                a_scores = [b if reverse else a for _, a, b in pairs]
                b_scores = [a if reverse else b for _, a, b in pairs]
                delta = [a - b for a, b in zip(a_scores, b_scores)]
                g, s, b = sum(d > 0 for d in delta), sum(d == 0 for d in delta), sum(d < 0 for d in delta)
                ma, mb = (mean(a_scores), mean(b_scores)) if n else (None, None)
                ratio = _ratio(ma, mb) if n else None
                lo, hi, nlo, nhi, note = interval
                if reverse:
                    lo, hi, nlo, nhi = tuple(-v if v is not None else None for v in (hi, lo, nhi, nlo))
                if not n:
                    note = "无配对有效样本"
                elif mb == 0:
                    note = "分位值不可计算：对照均分为0；" + note
                pass_delta = _ratio(sum(v >= pass_score for v in a_scores) - sum(v >= pass_score for v in b_scores), n)
                low_delta = _ratio(sum(v <= low_score for v in a_scores) - sum(v <= low_score for v in b_scores), n)
                gaps.rows.append([DIMENSION_NAMES[dimension], direction, len(applicable), n, _ratio(n, len(applicable)), ma, mb, ratio,
                                  mean(delta) if n else None, lo, hi, pass_delta, low_delta,
                                  sum(_limited(c, (p, q), dimension) for c, _, _ in pairs), note])
                gsb.rows.append([DIMENSION_NAMES[dimension], direction, n, f"{g}/{s}/{b}", g, s, b,
                                 _ratio(g, n), _ratio(s, n), _ratio(b, n), _ratio(g - b, n), nlo, nhi])
                if not compact:
                    frequencies = Counter(delta)
                    span = protocol.score_max - protocol.score_min
                    for difference in range(-span, span + 1):
                        differences.rows.append([DIMENSION_NAMES[dimension], direction, difference, frequencies[difference], n, _ratio(frequencies[difference], n)])
    return [gaps, gsb] if compact or common_three else [gaps, gsb, differences]


def _gate_pairs(cases: list[Case], snapshot: dict) -> StatisticsTable:
    table = _counts_table("C4．产品门槛对比（独立于评分样本）")
    count = max((c.product_count for c in cases), default=2)
    for a, b in combinations(range(1, count + 1), 2):
        present = [c for c in cases if c.status == "done" and all(c.result.get(f"answer{p}_input_status") in ("complete", "partial") for p in (a, b))]
        for gate in ("response", "safety", "both"):
            labels = Counter()
            for case in present:
                states = []
                for p in (a, b):
                    pair = [case.result.get(f"answer{p}_{g}_gate") for g in (("response", "safety") if gate == "both" else (gate,))]
                    state = "缺失" if any(v not in GATE_STATES for v in pair) else (
                        "fail" if "fail" in pair else ("unclear" if "unclear" in pair else "pass"))
                    states.append(state)
                if "缺失" in states:
                    label = "状态缺失"
                elif "unclear" in states:
                    label = "存在不确定"
                elif states == ["pass", "pass"]:
                    label = "双方通过"
                elif states == ["fail", "fail"]:
                    label = "双方不通过"
                else:
                    label = f"仅{_name(snapshot, a if states[0] == 'pass' else b)}通过"
                labels[label] += 1
            gate_label = {"response": "响应Gate", "safety": "安全Gate", "both": "双Gate合并"}[gate]
            for label in ("双方通过", f"仅{_name(snapshot, a)}通过", f"仅{_name(snapshot, b)}通过", "双方不通过", "存在不确定", "状态缺失"):
                _count(table, f"{_name(snapshot, a)} / {_name(snapshot, b)} {gate_label}", label, labels[label], len(present), "双Gate合并：任一fail即失败；否则有unclear为不确定")
    return table


def _three_way(cases: list[Case], protocol: CompareProtocol, snapshot: dict) -> StatisticsTable:
    table = StatisticsTable("D．三产品同题竞争格局",
        ("维度", "产品", "适用题数", "三方有效题数", "三方覆盖率", "独占第一数", "独占第一率", "两方并列第一数", "两方并列第一率", "三方同分数", "三方同分率", "独占末位数", "独占末位率"),
        ("text", "text", "count", "count", "percent", "count", "percent", "count", "percent", "count", "percent", "count", "percent"))
    for dimension in SCORE_DIMENSIONS:
        applicable = [c for c in cases if c.status == "done" and c.result.get(f"{dimension}_applicable") is True]
        scores = [[score_state(c, p, dimension, protocol)[1] for p in (1, 2, 3)] for c in applicable]
        scored = [s for s in scores if all(v is not None for v in s)]
        n = len(scored)
        for p in (1, 2, 3):
            first = sum(s[p - 1] == max(s) and s.count(max(s)) == 1 for s in scored)
            tied = sum(s[p - 1] == max(s) and s.count(max(s)) == 2 for s in scored)
            same = sum(len(set(s)) == 1 for s in scored)
            last = sum(s[p - 1] == min(s) and s.count(min(s)) == 1 for s in scored)
            table.rows.append([DIMENSION_NAMES[dimension], _name(snapshot, p), len(applicable), n, _ratio(n, len(applicable)),
                               first, _ratio(first, n), tied, _ratio(tied, n), same, _ratio(same, n), last, _ratio(last, n)])
    return table


def _subgroups(cases: list[Case]) -> list[tuple[str, list[Case]]]:
    groups: dict[tuple[str, str], list[Case]] = defaultdict(list)
    for case in cases:
        item = case.item
        source = item.get("source_data") or {}
        groups[("题型", "图文题" if item.get("query_images") else "文字题")].append(case)
        evidence = item.get("evidence_mode") or case.result.get("evidence_mode") or (
            "long_screenshot" if item.get("screenshot1") or source.get("screenshot1") else "video_frames")
        groups[("证据", "长截图" if evidence == "long_screenshot" else "录屏")].append(case)
        category = case.result.get("category_display") or item.get("category") or source.get("category") or "未分类"
        groups[("场景", str(category))].append(case)
    return [(f"{kind}：{label}", rows) for (kind, label), rows in sorted(groups.items())]


def build_compare_statistics(snapshot: dict, aligned_results: list[dict]) -> list[StatisticsTable]:
    """One worksheet, several compact tables; never consume cached summary values."""
    cases = _cases(snapshot, aligned_results)
    info = StatisticsTable("对比评估统计", ("项目", "内容"))
    info.rows = [
        ["数据集", snapshot.get("dataset_name") or "未记录"], ["任务", snapshot.get("task_id") or "未记录"],
        ["总题数", len(cases)],
        ["统计口径版本", STATISTICS_VERSION], ["导出时间（UTC）", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["样本", "按当前数据集顺序与最新结果对齐；重跑不重复计数；按标准、实现版本、产品数量分别统计。"],
        ["分位值", "A均分/B均分；两均分来自同一配对样本；百分比格式。对照均分为0时不可计算。"],
        ["GSB", "从方向A/B的A看：高于B为G，同分为S，低于B为B；净胜率=(G-B)/(G+S+B)。"],
        ["缺失值", "NA不补0或满分；不适用、无法核验、输入失败、Gate阻断、技术失败分别统计。—表示不可计算。"],
        ["准确性", "仅汇总适用性和核验状态；不汇总内容准确性分数、GSB或排名。"],
        ["总分", "当前两个标准没有正式维度权重，不计算加权总分、综合胜负或总体排名。"],
        ["阈值", "V0.2简化版：达标≥4、低质≤2；V0.3：达标≥2、低质≤1。统计阈值不代表跨版本等价。"],
        ["受限证据", "有效评分中，相关产品输入partial、Gate unclear、维度部分核验或长截图风险切片的题数。"],
        ["置信区间", f"配对题组百分位Bootstrap，{BOOTSTRAP_SAMPLES}次，固定seed={BOOTSTRAP_SEED}；按会话或重复Query分组。仅反映本样本抽样不确定性，不代表裁判准确性。"],
        ["解释", "各产品均分使用各自有效样本；比较只用共同有效样本。结合Gate失败和覆盖率判断差距；无现网权重时结论仅适用于本测评集。"],
        ["分差与比例", "均分差单位为当前标准分；达标率差、低质率差、净胜率及其区间均按百分比显示（解读为百分点）。"],
    ]
    tables = [info]
    if not cases:
        tables.append(_overview([], snapshot))
    cohorts: dict[tuple[str, str, int], list[Case]] = defaultdict(list)
    for case in cases:
        cohorts[(case.standard, case.revision, case.product_count)].append(case)
    for (standard, revision, count), cohort in sorted(cohorts.items()):
        label = f"标准 {standard} / 实现 {revision} / {count}产品"
        tables.append(StatisticsTable(label, ("说明",), rows=[["下列各区块仅统计本组；分位值与GSB均输出全部方向。"]]))
        tables.append(_overview(cohort, snapshot))
        protocol = _policy(standard, revision)
        if protocol is None:
            tables.append(StatisticsTable("评分统计不可用", ("原因",), rows=[["标准或实现版本缺失/不支持，未猜测评分范围或混合计算。"]]))
            continue
        tables.extend(_dimension_tables(cohort, protocol, snapshot))
        tables.extend(_pair_tables(cohort, protocol, snapshot))
        tables.append(_gate_pairs(cohort, snapshot))
        if count == 3:
            tables.append(_three_way(cohort, protocol, snapshot))
            tables.extend(_pair_tables(cohort, protocol, snapshot, common_three=True))
        for group_label, group in _subgroups(cohort):
            # Compact strata preserve six directions and denominators without
            # repeating hundreds of diagnostic rows or expensive CI estimates.
            subgroup_tables = _pair_tables(group, protocol, snapshot, compact=True)
            for table in subgroup_tables:
                table.title = f"E．{group_label}（{len(group)}题） {table.title[3:]}"
            tables.extend(subgroup_tables)
    reliability = StatisticsTable("F．裁判可靠性验证", ("验证内容", "指标", "当前状态"))
    reliability.rows = [
        ["评分准确性", "完全一致率、MAE、线性加权Kappa", "未开展：需同标准人工评分及明确配对关系"],
        ["胜负判断", "胜/平/负一致率与混淆矩阵", "未开展：需人工对比结果"],
        ["适用性与Gate", "适用性一致率、Gate失败精确率/召回率", "未开展：需人工状态标签"],
        ["重复稳定性", "分数一致率、波动、胜负一致率", "未开展：需独立重复实验；失败补跑不视为重复实验"],
        ["位置偏差", "换位一致率、胜负反转率、胜平转换率", "未开展：需位置互换实验及真实产品身份映射"],
    ]
    tables.append(reliability)
    return tables
