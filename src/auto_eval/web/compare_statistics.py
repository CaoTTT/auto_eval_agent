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

from ..conversation import is_conversation
from ..judges.compare_protocols import CompareProtocol, DIMENSIONS, resolve_compare_standard
from .protocol_identity import recover_protocol_identity


SHEET_NAME = "对比评估统计"
STATISTICS_VERSION = "compare-statistics-1.2"
DIMENSION_NAMES = dict(zip(DIMENSIONS, (
    "理解需求", "内容准确性", "服务闭环", "场景化满足", "直观高效", "有理有据", "引导推荐",
)))
SCORE_DIMENSIONS = tuple(d for d in DIMENSIONS if d != "accuracy")
# Reporting thresholds, not a conversion between scoring standards.
THRESHOLDS = {
    "0.2-simplified": (4, 2),
    "0.2-simplified-plus": (4, 2),
    "0.2-simplified-calibrated": (4, 2),
    "0.2-simplified-thinking-exposure": (4, 2),
    "0.3": (2, 1),
    "1.0": (4, 2),
    "1.1": (4, 2),
}
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
    standard_id: str = "qa_competitor_compare"
    identity_diagnostic: str = ""
    statistic_unit: str = ""
    conversation_diagnostic: str = ""


def _chat(protocol: CompareProtocol) -> bool:
    return protocol.standard_id == "qa_chat_compare"


def balanced_chat(protocol: CompareProtocol | None) -> bool:
    """The balanced output contract has program-owned, nullable input status."""
    return bool(protocol and _chat(protocol) and protocol.result_schema_version == "qa-chat-result-2")


def compact_1022(protocol: CompareProtocol | None) -> bool:
    """Use the frozen contract, never the user-facing standard label alone."""
    return bool(protocol and protocol.output_contract == "compare_1022_v2")


def gate_observable(case: Case, product: int, protocol: CompareProtocol | None) -> bool:
    """Whether the product's gate belongs to the observed-input denominator."""
    if case.status != "done" or product not in range(1, case.product_count + 1):
        return False
    status = case.result.get(f"answer{product}_input_status")
    return status in (None, "complete", "partial") if balanced_chat(protocol) or (protocol and protocol.standard_id == "timeliness_compare") else status in ("complete", "partial")


def _score_dimensions(protocol: CompareProtocol) -> tuple[str, ...]:
    return tuple(getattr(protocol, "score_dimensions", SCORE_DIMENSIONS))


def _dimension_name(protocol: CompareProtocol, dimension: str) -> str:
    return getattr(protocol, "dimension_names", DIMENSION_NAMES).get(dimension, dimension)


def _thresholds(protocol: CompareProtocol) -> tuple[int, int]:
    return THRESHOLDS.get(protocol.standard_version, (4, 2))


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _finite_score(value: Any, protocol: CompareProtocol) -> bool:
    if _chat(protocol) or protocol.standard_id == "timeliness_compare":
        return type(value) is int and protocol.score_min <= value <= protocol.score_max
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
    records = [snapshot, manifest, snapshot.get("options") or {}, *aligned_results]
    chat_present = any(record.get("standard_id") in ("qa_chat_compare", "timeliness_compare", "knowledge_qa_compare") or
                       str(record.get("evaluation_profile") or record.get("id") or "").startswith(("qa_chat_compare@", "timeliness_compare@", "knowledge_qa_compare@"))
                       for record in records)
    recovered = recover_protocol_identity({**snapshot, "results": aligned_results}) if chat_present else None
    if recovered is not None:
        manifest = recovered["manifest"]
        selected = recovered["evaluation_profile"]
    task_standard = manifest.get("standard_version") or (selected.split("@", 1)[-1] if "@" in selected else "")
    task_family = manifest.get("standard_id") or (selected.split("@", 1)[0] if "@" in selected else "qa_competitor_compare")
    task_diagnostic = "；".join(recovered["diagnostics"]) if recovered else ""
    if selected and manifest.get("id") and selected != manifest["id"]:
        task_diagnostic = "任务协议与冻结清单冲突"
    if "@" in selected and (task_family, task_standard) != tuple(selected.split("@", 1)):
        task_diagnostic = "任务协议家族或版本与冻结清单冲突"
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
        profile = row.get("evaluation_profile") or ""
        family = row.get("standard_id") or (profile.split("@", 1)[0] if "@" in profile else task_family)
        diagnostic = task_diagnostic
        if "@" in profile:
            profile_family, profile_version = profile.split("@", 1)
            if (family, standard) != (profile_family, profile_version):
                diagnostic = "结果协议家族或版本冲突"
        revision = row.get("bundle_revision") or (
            manifest.get("bundle_revision") if (family, standard) == (task_family, task_standard) else None
        ) or "未记录实现版本"
        cases.append(Case(index, item, row, status, _product_count(item, row), standard, revision, family, diagnostic))
    _assign_statistic_units(cases)
    return cases


def _policy(standard: str, revision: str, standard_id: str = "qa_competitor_compare") -> CompareProtocol | None:
    if (standard_id in ("qa_chat_compare", "timeliness_compare", "knowledge_qa_compare") or standard == "0.2-simplified-1022") and revision in ("", "未记录实现版本", None):
        return None
    try:
        return resolve_compare_standard(
            standard_id, standard,
            None if revision == "未记录实现版本" else revision,
        )
    except ValueError:
        return None


def product_applicable(case: Case, product: int, dimension: str, protocol: CompareProtocol) -> bool | None:
    """The stored applicability, independent of score availability and gates."""
    if protocol.standard_id == "knowledge_qa_compare":
        from .knowledge_qa_statistics import knowledge_qa_score_state
        state = knowledge_qa_score_state(case, product, dimension, protocol)[0]
        return False if state == "not_applicable" else True if state in ("scored", "insufficient_evidence", "unverifiable") else None
    key = f"answer{product}_{dimension}_applicable" if _chat(protocol) or protocol.standard_id == "timeliness_compare" else f"{dimension}_applicable"
    value = case.result.get(key)
    return value if type(value) is bool else None


def _chat_score_state(case: Case, product: int, dimension: str, protocol: CompareProtocol) -> tuple[str, float | None]:
    row = case.result
    prefix = f"answer{product}_{dimension}"
    input_status = row.get(f"answer{product}_input_status")
    if input_status == "failed":
        return "输入失败", None
    if input_status not in ("complete", "partial") and not (balanced_chat(protocol) and input_status is None):
        return "输入状态缺失", None
    response, safety = (row.get(f"answer{product}_{gate}_gate") for gate in ("response", "safety"))
    if response == "fail" or safety == "fail":
        return ("响应Gate阻断" if response == "fail" else "安全Gate阻断"), None
    if response not in GATE_STATES or safety not in GATE_STATES:
        return "Gate状态缺失", None
    status = row.get(prefix + "_score_status")
    if status not in ("scored", "not_applicable", "gate_blocked", "input_failed", "unverifiable"):
        return ("评分状态缺失" if status is None else "评分状态异常"), None
    applicable = product_applicable(case, product, dimension, protocol)
    verification = row.get(prefix + "_verification_status")
    if status in ("gate_blocked", "input_failed"):
        return "评分状态冲突", None
    if status == "not_applicable":
        return ("维度不适用" if applicable is False and verification == "not_required" else "评分状态冲突"), None
    if status == "unverifiable":
        return ("无法核验" if applicable is not False and verification == "unverifiable" else "评分状态冲突"), None
    if applicable is not True:
        return ("评分状态冲突" if applicable is False else "适用性状态缺失"), None
    if verification not in ("verified", "partial", "not_required"):
        return ("评分状态冲突" if verification == "unverifiable" else "核验状态缺失"), None
    score = row.get(prefix + "_score")
    if score is None:
        return "应评分但缺失", None
    if not _finite_score(score, protocol):
        return "分数格式或范围异常", None
    return "有效评分", float(score)


def _compact_1022_score_state(case: Case, product: int, dimension: str, protocol: CompareProtocol) -> tuple[str, float | None]:
    row = case.result
    if dimension not in _score_dimensions(protocol):
        return "未采集维度", None
    input_status = row.get(f"answer{product}_input_status")
    if input_status == "failed":
        return "输入失败", None
    if input_status not in ("complete", "partial"):
        return "输入状态缺失", None
    response, safety = (row.get(f"answer{product}_{gate}_gate") for gate in ("response", "safety"))
    # A known failure takes precedence even if downstream fields were omitted.
    if response == "fail" or safety == "fail":
        return ("响应Gate阻断" if response == "fail" else "安全Gate阻断"), None
    if response not in GATE_STATES or safety not in GATE_STATES:
        return "Gate状态缺失", None
    if response != "pass" or safety != "pass":
        return ("响应Gate阻断" if response != "pass" else "安全Gate阻断"), None
    applicable = row.get(f"{dimension}_applicable")
    if applicable is False:
        return "维度不适用", None
    if applicable is not True:
        return "适用性状态缺失", None
    score = row.get(f"answer{product}_{dimension}_score")
    if score is None:
        from ..judges.compare_1022_contract import is_unscored_reason_1022, product_reason_1022
        reason = product_reason_1022(row.get(f"{dimension}_reason"), product)
        if is_unscored_reason_1022(reason):
            return ("证据不足" if reason.startswith("未评分（证据不足）：") else "有说明的未评分"), None
        return "应评分但缺失", None
    if type(score) is not int or not protocol.score_min <= score <= protocol.score_max:
        return "分数格式或范围异常", None
    return "有效评分", float(score)


def score_state(case: Case, product: int, dimension: str, protocol: CompareProtocol) -> tuple[str, float | None]:
    """Mutually exclusive reasons, following the frozen protocol's actual gates."""
    if protocol.standard_id == "knowledge_qa_compare":
        from .knowledge_qa_statistics import STATUS_LABELS, knowledge_qa_score_state
        state, value = knowledge_qa_score_state(case, product, dimension, protocol)
        return STATUS_LABELS.get(state, state), value
    if protocol.standard_id == "timeliness_compare":
        from .timeliness_statistics import STATUS_LABELS, timeliness_score_state
        state, value = timeliness_score_state(case, product, dimension, protocol)
        return STATUS_LABELS.get(state, state), value
    if case.status != "done":
        return ("技术失败" if case.status == "failed" else "未完成"), None
    if case.identity_diagnostic:
        return "协议身份异常", None
    if product not in range(1, case.product_count + 1):
        return "非活动产品", None
    if compact_1022(protocol):
        return _compact_1022_score_state(case, product, dimension, protocol)
    if _chat(protocol):
        return _chat_score_state(case, product, dimension, protocol)
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
    return (case.standard_id != "qa_chat_compare" and row.get(f"{dimension}_verification_status") == "partial") or any(
        (case.standard_id == "qa_chat_compare" and row.get(f"answer{p}_{dimension}_verification_status") == "partial")
        or
        row.get(f"answer{p}_input_status") == "partial"
        or any(row.get(f"answer{p}_{gate}_gate") == "unclear" for gate in ("response", "safety"))
        or (case.item.get(f"screenshot_meta{p}") or {}).get("split_status") == "risky"
        for p in products
    )


def _assign_statistic_units(cases: list[Case]) -> None:
    """Reliable sessions are units; malformed or independent rows stay isolated."""
    groups = defaultdict(list)
    for case in cases:
        case.statistic_unit = f"case:{case.index}"
        item = case.item
        sid, turn = item.get("session_id"), item.get("turn_index")
        if not is_conversation(item):
            continue
        if not isinstance(sid, str) or not sid.strip() or type(turn) is not int or turn < 1:
            case.conversation_diagnostic = "多轮结构异常：需要非空session_id和严格正整数turn_index；按独立条目隔离"
            continue
        groups[(case.standard_id, case.standard, case.revision, sid.strip())].append(case)
    for identity, turns in groups.items():
        indices = [c.item["turn_index"] for c in turns]
        mappings = {json.dumps(c.item.get("product_mapping"), sort_keys=True, ensure_ascii=False) for c in turns}
        if len(set(indices)) != len(indices) or len(mappings) != 1 or len({c.product_count for c in turns}) != 1:
            reason = "轮次重复" if len(set(indices)) != len(indices) else "产品数量或映射不一致"
            for case in turns:
                case.conversation_diagnostic = f"多轮结构异常：{reason}；按独立条目隔离"
            continue
        missing = []
        previous = 0
        for index in sorted(indices):
            if index > previous + 1:
                missing.append(str(previous + 1) if index == previous + 2 else f"{previous + 1}-{index - 1}")
            previous = index
        key = "conversation:" + json.dumps(identity, ensure_ascii=False)
        for case in turns:
            case.statistic_unit = key
            if missing:
                case.conversation_diagnostic = "存量会话缺失已知轮次：" + ",".join(missing)


def _cluster_key(case: Case) -> str:
    item = case.item
    source = item.get("source_data") or {}
    if case.statistic_unit.startswith("conversation:"):
        return case.statistic_unit
    if case.conversation_diagnostic:
        return f"case:{case.index}"
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


def _overview(cases: list[Case], snapshot: dict, protocol: CompareProtocol | None = None) -> StatisticsTable:
    table = _counts_table("A．评测执行与证据质量")
    completed = [c for c in cases if c.status == "done"]
    failed = [c for c in cases if c.status == "failed"]
    for label, count in (("总题数", len(cases)), ("结构化结果成功／覆盖率", len(completed)),
                         ("技术失败", len(failed)), ("未完成", len(cases) - len(completed) - len(failed))):
        _count(table, "任务", label, count, len(cases), "按导出时最新逐题结果；未完成不计入失败")
    manifest = snapshot.get("protocol_manifest") or {}
    balanced = (balanced_chat(protocol) if protocol is not None else
                manifest.get("standard_id") == "qa_chat_compare" and manifest.get("result_schema_version") == "qa-chat-result-2")
    compact = compact_1022(protocol) if protocol is not None else manifest.get("output_contract") == "compare_1022_v2"
    if not balanced and not compact:
        for state in ("high", "medium", "low"):
            _count(table, "裁判", {"high": "高置信度", "medium": "中置信度", "low": "低置信度"}[state],
                   sum(c.result.get("confidence") == state for c in completed), len(completed), "模型自报，不能解释为准确率")
        _count(table, "裁判", "置信度缺失", sum(c.result.get("confidence") not in ("high", "medium", "low") for c in completed), len(completed))
    for label, subset in (() if compact else (("成功结果待复核", completed), ("技术失败待复核", failed))):
        _count(table, "裁判", label, sum(bool(c.result.get("needs_human_review") or c.result.get("needs_review")) for c in subset), len(subset))
    errors = Counter(c.result.get("error_type") or "未分类技术错误" for c in failed)
    for reason, count in sorted(errors.items()):
        _count(table, "失败原因", reason, count, len(failed))
    if not balanced:
        conflicts = Counter(c.result.get("has_conflict") for c in completed)
        known = conflicts["yes"] + conflicts["no"]
        _count(table, "内容冲突", "明确冲突率", conflicts["yes"], known, "yes/(yes+no)；不归责具体产品或产品对")
        _count(table, "内容冲突", "不确定", conflicts["unclear"], len(completed))
        _count(table, "内容冲突", "状态缺失", len(completed) - known - conflicts["unclear"], len(completed))
    for product in range(1, max((c.product_count for c in cases), default=2) + 1):
        present = [c for c in completed if c.product_count >= product]
        label = _name(snapshot, product)
        for state in ("complete", "partial", "failed"):
            _count(table, label, ("采集" if balanced else "输入") + STATE_NAMES[state], sum(c.result.get(f"answer{product}_input_status") == state for c in present), len(present))
        if balanced:
            _count(table, label, "采集状态未知", sum(c.result.get(f"answer{product}_input_status") is None for c in present), len(present), "程序没有可信采集结论；不等于产品失败，也不自动剔除可见证据充分的合法分")
            _count(table, label, "采集状态异常", sum(c.result.get(f"answer{product}_input_status") not in (None, "complete", "partial", "failed") for c in present), len(present))
        else:
            _count(table, label, "输入状态缺失", sum(c.result.get(f"answer{product}_input_status") not in ("complete", "partial", "failed") for c in present), len(present))
        observable = [c for c in present if gate_observable(c, product, protocol)]
        for gate, gate_label in (("response", "响应Gate"), ("safety", "安全Gate")):
            known = [c for c in observable if c.result.get(f"answer{product}_{gate}_gate") in GATE_STATES]
            note = "pass/(pass+fail+unclear)；程序采集结论未知仍纳入可见Gate，排除可信采集failed" if balanced else "pass/(pass+fail+unclear)；仅计对应产品输入complete/partial且Gate有效的结构化结果"
            _count(table, label, gate_label + "通过率", sum(c.result.get(f"answer{product}_{gate}_gate") == "pass" for c in known), len(known), note)
            _count(table, label, gate_label + "可观察覆盖率", len(known), len(present), "有效Gate/对应产品结构化完成条目；输入failed单列")
            for state in GATE_STATES:
                _count(table, label, gate_label + STATE_NAMES[state], sum(c.result.get(f"answer{product}_{gate}_gate") == state for c in observable), len(observable), "不包含输入failed造成的强制unclear")
            _count(table, label, gate_label + "状态缺失", sum(c.result.get(f"answer{product}_{gate}_gate") not in GATE_STATES for c in observable), len(observable))
    return table


def _dimension_tables(cases: list[Case], protocol: CompareProtocol, snapshot: dict) -> list[StatisticsTable]:
    diagnostics = _counts_table("B1．维度适用性与评分覆盖" if compact_1022(protocol) else "B1．维度适用性、核验与评分覆盖")
    performance = StatisticsTable("B2．各产品维度表现（各自有效样本）",
        ("维度", "产品", "有效题数", "均分", "中位数", "达标题数", "达标率", "低质题数", "低质率", "满档率", "最低档率", "受限证据题数"),
        ("text", "text", "count", "decimal", "decimal", "count", "percent", "count", "percent", "percent", "percent", "count"))
    distribution = StatisticsTable("B3．完整分档分布", ("维度", "产品", "分值", "题数", "有效题数", "占比"),
                                   ("text", "text", "count", "count", "count", "percent"))
    missing = _counts_table("B4．评分状态分布（互斥原因）")
    done = [c for c in cases if c.status == "done"]
    count = max((c.product_count for c in cases), default=2)
    pass_score, low_score = _thresholds(protocol)
    for dimension in getattr(protocol, "dimensions", DIMENSIONS):
        label = _dimension_name(protocol, dimension)
        known = [c for c in done if isinstance(c.result.get(f"{dimension}_applicable"), bool)]
        applicable = [c for c in known if c.result[f"{dimension}_applicable"]]
        if _chat(protocol):
            inconsistent = 0
            for case in done:
                product_values = [product_applicable(case, p, dimension, protocol) for p in range(1, case.product_count + 1)]
                expected_shared = True if True in product_values else (False if all(v is False for v in product_values) else None)
                key = f"{dimension}_applicable"
                has_valid = any(score_state(case, p, dimension, protocol)[1] is not None for p in range(1, case.product_count + 1))
                if (key in case.result and case.result[key] is not expected_shared) or (case.result.get(f"{dimension}_verification_status") == "unverifiable" and has_valid):
                    inconsistent += 1
            _count(diagnostics, label, "共享摘要不一致", inconsistent, len(done), "共享摘要仅诊断；评分和适用分母始终读取产品级权威字段")
        if not _chat(protocol):
            _count(diagnostics, label, "维度适用率", len(applicable), len(known), "适用性仅描述需求分布；不以越高越好评价")
            unknown = [c for c in done if not isinstance(c.result.get(f"{dimension}_applicable"), bool)]
            if compact_1022(protocol):
                blocked = [c for c in unknown if all(score_state(c, p, dimension, protocol)[0] in
                           ("输入失败", "响应Gate阻断", "安全Gate阻断") for p in range(1, c.product_count + 1))]
                _count(diagnostics, label, "全部产品阻断，适用性未评估", len(blocked), len(done))
                blocked_indices = {c.index for c in blocked}
                unknown = [c for c in unknown if c.index not in blocked_indices]
            _count(diagnostics, label, "适用性状态缺失", len(unknown), len(done))
            if not compact_1022(protocol):
                for state in VERIFY_STATES:
                    _count(diagnostics, label, "核验：" + ("部分核验" if state == "partial" else STATE_NAMES[state]), sum(c.result.get(f"{dimension}_verification_status") == state for c in applicable), len(applicable))
                _count(diagnostics, label, "核验状态缺失", sum(c.result.get(f"{dimension}_verification_status") not in VERIFY_STATES for c in applicable), len(applicable))
        if dimension not in _score_dimensions(protocol):
            continue
        for p in range(1, count + 1):
            product_cases = [c for c in cases if c.product_count >= p]
            subject = f"{label} / {_name(snapshot, p)}"
            if _chat(protocol):
                known = [c for c in product_cases if product_applicable(c, p, dimension, protocol) is not None]
                applicable = [c for c in known if product_applicable(c, p, dimension, protocol) is True]
                _count(diagnostics, subject, "确认适用样本数", len(applicable), len(product_cases), "确认适用/U；U为该产品可对应输入条目，含技术失败与未完成")
                _count(diagnostics, subject, "维度适用率", len(applicable), len(known), "确认适用/适用性已知；未知不进分母")
                _count(diagnostics, subject, "适用性未知数", len(product_cases) - len(known), len(product_cases), "显式null或缺失；不从Gate、空分或技术失败反推")
                for state in VERIFY_STATES:
                    _count(diagnostics, subject, "核验：" + ("部分核验" if state == "partial" else STATE_NAMES[state]), sum(c.result.get(f"answer{p}_{dimension}_verification_status") == state for c in applicable), len(applicable))
                _count(diagnostics, subject, "核验状态缺失", sum(c.result.get(f"answer{p}_{dimension}_verification_status") not in VERIFY_STATES for c in applicable), len(applicable))
            states = [score_state(c, p, dimension, protocol) for c in product_cases]
            valid = [(c, score) for c, (state, score) in zip(product_cases, states) if state == "有效评分"]
            scores = [s for _, s in valid]
            expected = sum(state in ("有效评分", "应评分但缺失", "分数格式或范围异常") for state, _ in states)
            _count(diagnostics, subject, "有效评分覆盖率", len(valid), len(applicable), "有效分/该产品该维度确认适用数；Gate阻断等原因见评分状态")
            completeness_note = ("有效分/按所选协议应评分数量；不适用、有说明的未评分、输入及Gate阻断不视为缺失" if compact_1022(protocol)
                                 else "有效分/按所选协议应评分数量；NA、无法核验、输入及Gate阻断不视为缺失")
            _count(diagnostics, subject, "应评分输出完整率", len(valid), expected, completeness_note)
            _count(diagnostics, subject, "有效评分数", len(valid), len(product_cases), "有效分/U；受限证据合法分仍纳入")
            frequencies = Counter(state for state, _ in states)
            reasons = sorted(set(frequencies) | ({"有效评分", "维度不适用", "输入失败", "响应Gate阻断", "安全Gate阻断", "无法核验", "技术失败", "未完成"} if _chat(protocol) else set()))
            for reason in reasons:
                _count(missing, subject, reason, frequencies[reason], len(product_cases), "该互斥状态数量/U；原始score_status保留在逐题结果")
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
        ("维度", "方向 A/B", "适用题数", "配对有效题数", "可比覆盖率", "A均分", "B均分", "分位值 A/B", "均分差 A-B", "分差95%下限", "分差95%上限", "达标率差", "低质率差", "受限证据题数", "适用未知题数", "说明"),
        ("text", "text", "count", "count", "percent", "decimal", "decimal", "percent", "decimal", "decimal", "decimal", "percent", "percent", "count", "count", "text"))
    gsb = StatisticsTable(f"C2．双向 GSB（{scope}）",
        ("维度", "方向 A/B", "配对有效题数", "GSB数量", "G", "S", "B", "胜率", "平率", "负率", "净胜率", "净胜95%下限", "净胜95%上限"),
        ("text", "text", "count", "text", "count", "count", "count", "percent", "percent", "percent", "percent", "percent", "percent"))
    differences = StatisticsTable(f"C3．双向分差分布（{scope}）", ("维度", "方向 A/B", "分差 A-B", "题数", "配对有效题数", "占比"),
                                  ("text", "text", "count", "count", "count", "percent"))
    count = max((c.product_count for c in cases), default=2)
    pass_score, low_score = _thresholds(protocol)
    for dimension in _score_dimensions(protocol):
        scores = {c.index: [score_state(c, p, dimension, protocol)[1] for p in range(1, count + 1)] for c in cases}
        for first, second in combinations(range(1, count + 1), 2):
            products = tuple(range(1, count + 1)) if common_three else (first, second)
            applicable = [c for c in cases if (c.status == "done" or _chat(protocol)) and all(product_applicable(c, p, dimension, protocol) is True for p in products)]
            unknown = sum(any(product_applicable(c, p, dimension, protocol) is None for p in products) for c in cases)
            pairs = []
            for case in cases:
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
                gaps.rows.append([_dimension_name(protocol, dimension), direction, len(applicable), n, _ratio(n, len(applicable)), ma, mb, ratio,
                                  mean(delta) if n else None, lo, hi, pass_delta, low_delta,
                                  sum(_limited(c, (p, q), dimension) for c, _, _ in pairs), unknown, note])
                gsb.rows.append([_dimension_name(protocol, dimension), direction, n, f"{g}/{s}/{b}", g, s, b,
                                 _ratio(g, n), _ratio(s, n), _ratio(b, n), _ratio(g - b, n), nlo, nhi])
                if not compact:
                    frequencies = Counter(delta)
                    span = protocol.score_max - protocol.score_min
                    for difference in range(-span, span + 1):
                        differences.rows.append([_dimension_name(protocol, dimension), direction, difference, frequencies[difference], n, _ratio(frequencies[difference], n)])
    return [gaps, gsb] if compact or common_three else [gaps, gsb, differences]


def _gate_pairs(cases: list[Case], snapshot: dict, protocol: CompareProtocol | None = None) -> StatisticsTable:
    table = _counts_table("C4．产品门槛对比（独立于评分样本）")
    count = max((c.product_count for c in cases), default=2)
    for a, b in combinations(range(1, count + 1), 2):
        present = [c for c in cases if all(gate_observable(c, p, protocol) for p in (a, b))]
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
    for dimension in _score_dimensions(protocol):
        applicable = [c for c in cases if (c.status == "done" or _chat(protocol)) and all(product_applicable(c, p, dimension, protocol) is True for p in (1, 2, 3))]
        scores = [[score_state(c, p, dimension, protocol)[1] for p in (1, 2, 3)] for c in cases]
        scored = [s for s in scores if all(v is not None for v in s)]
        n = len(scored)
        for p in (1, 2, 3):
            first = sum(s[p - 1] == max(s) and s.count(max(s)) == 1 for s in scored)
            tied = sum(s[p - 1] == max(s) and s.count(max(s)) == 2 for s in scored)
            same = sum(len(set(s)) == 1 for s in scored)
            last = sum(s[p - 1] == min(s) and s.count(min(s)) == 1 for s in scored)
            table.rows.append([_dimension_name(protocol, dimension), _name(snapshot, p), len(applicable), n, _ratio(n, len(applicable)),
                               first, _ratio(first, n), tied, _ratio(tied, n), same, _ratio(same, n), last, _ratio(last, n)])
    return table


def _subgroups(cases: list[Case]) -> list[tuple[str, list[Case]]]:
    groups: dict[tuple[str, str], list[Case]] = defaultdict(list)
    for case in cases:
        item = case.item
        source = item.get("source_data") or {}
        if case.statistic_unit.startswith("conversation:"):
            groups[("轮次", str(item["turn_index"]))].append(case)
            groups[("有效用户输入", item.get("effective_input_modality", "未检查"))].append(case)
        modality = item.get("effective_input_modality") or case.result.get("effective_input_modality")
        groups[("题型", "图文题" if item.get("query_images") or (case.statistic_unit.startswith("conversation:") and modality == "text_image") else "文字题")].append(case)
        evidence = item.get("evidence_mode") or case.result.get("evidence_mode") or (
            "long_screenshot" if item.get("screenshot1") or source.get("screenshot1") else "video_frames")
        groups[("证据", "长截图" if evidence == "long_screenshot" else "录屏")].append(case)
        category = case.result.get("category_display") or item.get("category") or source.get("category") or "未分类"
        groups[("场景", str(category))].append(case)
    return [(f"{kind}：{label}", rows) for (kind, label), rows in sorted(groups.items())]


def build_compare_statistics(snapshot: dict, aligned_results: list[dict]) -> list[StatisticsTable]:
    """One worksheet, several compact tables; never consume cached summary values."""
    if recover_protocol_identity({**snapshot, "results": aligned_results}).get("is_knowledge_qa"):
        from .knowledge_qa_statistics import knowledge_qa_statistics
        return knowledge_qa_statistics(snapshot, aligned_results)
    if recover_protocol_identity({**snapshot, "results": aligned_results})["is_timeliness"]:
        from .timeliness_statistics import timeliness_statistics
        return timeliness_statistics(snapshot, aligned_results)
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
        ["总分", "当前各标准没有正式维度权重，不计算加权总分、综合胜负或总体排名。"],
        ["阈值", "问答闲聊V1、V0.2简化版及其评分校准版：达标≥4、低质≤2；V0.3：达标≥2、低质≤1。各标准分别统计，阈值不代表跨版本等价。"],
        ["受限证据", "有效评分中，相关产品输入partial、Gate unclear、维度部分核验或长截图风险切片的题数。"],
        ["置信区间", f"配对题组百分位Bootstrap，{BOOTSTRAP_SAMPLES}次，固定seed={BOOTSTRAP_SEED}；按会话或重复Query分组。仅反映本样本抽样不确定性，不代表裁判准确性。"],
        ["解释", "各产品均分使用各自有效样本；比较只用共同有效样本。结合Gate失败和覆盖率判断差距；无现网权重时结论仅适用于本测评集。"],
        ["分差与比例", "均分差单位为当前标准分；达标率差、低质率差、净胜率及其区间均按百分比显示（解读为百分点）。"],
    ]
    if cases and all(c.standard_id == "qa_chat_compare" or compact_1022(_policy(c.standard, c.revision, c.standard_id)) for c in cases):
        info.rows = [row for row in info.rows if row[0] != "准确性"]
    tables = [info]
    if not cases:
        tables.append(_overview([], snapshot))
    cohorts: dict[tuple[str, str, str, int], list[Case]] = defaultdict(list)
    for case in cases:
        cohorts[(case.standard_id, case.standard, case.revision, case.product_count)].append(case)
    for (standard_id, standard, revision, count), cohort in sorted(cohorts.items()):
        identity = standard if standard_id == "qa_competitor_compare" else f"{standard_id}@{standard}"
        label = f"标准 {identity} / 实现 {revision} / {count}产品"
        tables.append(StatisticsTable(label, ("说明",), rows=[["下列各区块仅统计本组；分位值与GSB均输出全部方向。"]]))
        protocol = _policy(standard, revision, standard_id)
        tables.append(_overview(cohort, snapshot, protocol))
        if protocol is None or any(c.identity_diagnostic for c in cohort):
            diagnoses = sorted({c.identity_diagnostic for c in cohort if c.identity_diagnostic})
            tables.append(StatisticsTable("评分统计不可用", ("原因",), rows=[["；".join(diagnoses) or "标准或实现版本缺失/不支持，未猜测评分范围或混合计算。"]]))
            continue
        tables.extend(_dimension_tables(cohort, protocol, snapshot))
        tables.extend(_pair_tables(cohort, protocol, snapshot))
        tables.append(_gate_pairs(cohort, snapshot, protocol))
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
    tables.extend(conversation_statistics(snapshot, aligned_results))
    return tables


def conversation_statistics(snapshot: dict, aligned_results: list[dict]) -> list[StatisticsTable]:
    if recover_protocol_identity({**snapshot, "results": aligned_results}).get("is_knowledge_qa"):
        from .knowledge_qa_statistics import knowledge_qa_conversation_statistics
        return knowledge_qa_conversation_statistics(snapshot, aligned_results)
    if recover_protocol_identity({**snapshot, "results": aligned_results})["is_timeliness"]:
        from .timeliness_statistics import timeliness_conversation_statistics
        return timeliness_conversation_statistics(snapshot, aligned_results)
    cases = _cases(snapshot, aligned_results)
    if not any(c.statistic_unit.startswith("conversation:") or c.conversation_diagnostic or c.standard_id == "qa_chat_compare" for c in cases):
        return []
    sessions = defaultdict(list)
    for case in cases:
        if case.statistic_unit.startswith("conversation:"):
            sessions[case.statistic_unit].append(case)
    coverage = StatisticsTable("多轮会话覆盖", ("会话", "应执行轮数", "结构化完成轮数", "完整", "警示影响轮数", "阻断轮数", "诊断", "标准", "实现", "产品数量"))
    for turns in sessions.values():
        done = sum(c.status == "done" for c in turns)
        # For retained historical gaps the observed maximum establishes only
        # the known prefix; never invent turns beyond that prefix.
        expected = max(c.item["turn_index"] for c in turns)
        diagnoses = "；".join(sorted({c.conversation_diagnostic for c in turns if c.conversation_diagnostic}))
        coverage.rows.append([turns[0].item["session_id"], expected, done, done == expected,
            sum(bool(c.result.get("image_warning_count")) for c in turns),
            sum(c.result.get("input_diagnostic_status") == "blocked" for c in turns), diagnoses,
            f"{turns[0].standard_id}@{turns[0].standard}", turns[0].revision, turns[0].product_count])
    for case in cases:
        if case.conversation_diagnostic and not case.statistic_unit.startswith("conversation:"):
            coverage.rows.append([f"独立条目 {case.item.get('id') or case.index}", 1, int(case.status == "done"), False,
                                  int(bool(case.result.get("image_warning_count"))),
                                  int(case.result.get("input_diagnostic_status") == "blocked"), case.conversation_diagnostic,
                                  f"{case.standard_id}@{case.standard}", case.revision, case.product_count])
    chat_only = all(c.standard_id == "qa_chat_compare" for c in cases)
    a_label, b_label = ("A单元等权均值", "B单元等权均值") if chat_only else ("A会话均值", "B会话均值")
    paired = StatisticsTable("多轮会话等权（含独立题；均值为统计值；逐轮微平均见原统计）",
        ("标准", "实现", "维度", "方向", "有效会话数", "有效配对轮数", a_label, b_label, "会话等权分位值", "有效单元数", "其中独立题数", "其中可靠会话数", "产品数量", "说明"),
        ("text", "text", "text", "text", "count", "count", "decimal", "decimal", "percent", "count", "count", "count", "count", "text"))
    products = StatisticsTable("会话与独立题等权：各产品维度均值（统计值）",
        ("标准", "实现", "维度", "产品", "有效单元数", "其中可靠会话数", "其中独立题数", "有效轮数", "等权均值", "全部单元数", "产品数量", "说明"),
        ("text", "text", "text", "text", "count", "count", "count", "count", "decimal", "count", "count", "text"))
    states = _counts_table("会话等权状态诊断（原始条目数量）")
    cohorts = defaultdict(list)
    for case in cases:
        cohorts[(case.standard_id, case.standard, case.revision, case.product_count)].append(case)
    for (standard_id, standard, revision, count), cohort in sorted(cohorts.items()):
        protocol = _policy(standard, revision, standard_id)
        if protocol is None or any(c.identity_diagnostic for c in cohort):
            continue
        identity = f"{standard_id}@{standard}"
        units = {c.statistic_unit for c in cohort}
        for dimension in _score_dimensions(protocol):
            label = _dimension_name(protocol, dimension)
            for p in range(1, count + 1):
                values = defaultdict(list)
                frequencies = Counter()
                for case in cohort:
                    state, value = score_state(case, p, dimension, protocol)
                    frequencies[state] += 1
                    if value is not None:
                        values[case.statistic_unit].append(value)
                n = len(values)
                reliable = sum(key.startswith("conversation:") for key in values)
                products.rows.append([identity, revision, label, _name(snapshot, p), n, reliable, n - reliable,
                    sum(map(len, values.values())), mean(mean(v) for v in values.values()) if values else None, len(units), count,
                    "每个可靠会话先对有效轮求均值；每道独立题为一个单元；小数为统计值，不是裁判整数分。"])
                for state, frequency in sorted(frequencies.items()):
                    _count(states, f"{identity} / {revision} / {count}产品 / {label} / {_name(snapshot, p)}", state, frequency, len(cohort), "原始条目数量；空分不补0；不把会话均值分入整数评分档")
            for a in range(1, count + 1):
                for b in range(1, count + 1):
                    if a == b:
                        continue
                    values = defaultdict(list)
                    for case in cohort:
                        _, x = score_state(case, a, dimension, protocol)
                        _, y = score_state(case, b, dimension, protocol)
                        if x is not None and y is not None:
                            values[case.statistic_unit].append((x, y))
                    x = mean(mean(v[0] for v in rows) for rows in values.values()) if values else None
                    y = mean(mean(v[1] for v in rows) for rows in values.values()) if values else None
                    reliable = sum(key.startswith("conversation:") for key in values)
                    paired.rows.append([identity, revision, label, f"{a}/{b}", reliable,
                        sum(map(len, values.values())), x, y, _ratio(x, y) if x is not None else None,
                        len(values), len(values) - reliable, reliable, count,
                        "先在每个单元取共同有效轮，再对同一批单元均值等权；分位值为等权均值比。独立题也纳入。"])
    return [coverage, paired, products, states]
