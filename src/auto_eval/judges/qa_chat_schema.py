"""Frozen, product-local result contract for qa_chat_compare@1.0.

This model deliberately does not inherit the legacy comparison observation:
``personalization`` is a real dimension here, never a legacy winner projection.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, create_model, model_validator

from ..schema import CompareWinner, ConflictVerdict, GateStatus, InputStatus, JudgeConfidence, ProductId, VerificationStatus

QA_CHAT_STANDARD_ID = "qa_chat_compare"
QA_CHAT_COMPARE_PROTOCOL_ID = "qa_chat_compare@1.0"
QA_CHAT_RESULT_SCHEMA_VERSION = "qa-chat-result-1"
QA_CHAT_DIMENSION_NAMES = {
    "understanding": "理解需求",
    "personalization": "个性化适配",
    "emotional_expression": "情感表达恰当",
    "style": "风格恰当",
    "scenario_fulfillment": "场景化满足",
    "guided_recommendation": "引导推荐",
}
QA_CHAT_DIMENSIONS = tuple(QA_CHAT_DIMENSION_NAMES)
QAChatScore = Annotated[int, Field(strict=True, ge=1, le=5)]
QAChatScoreStatus = Literal["scored", "not_applicable", "gate_blocked", "input_failed", "unverifiable"]


class _QAChatObservationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    standard_id: str = QA_CHAT_STANDARD_ID
    standard_version: str = "1.0"
    evaluation_profile: str = QA_CHAT_COMPARE_PROTOCOL_ID
    bundle_revision: str = "1.0.0"
    result_schema_version: str = QA_CHAT_RESULT_SCHEMA_VERSION
    evaluation_datetime: str = ""
    product_count: Annotated[int, Field(strict=True, ge=2, le=3)]
    has_conflict: ConflictVerdict = "unclear"
    conflict_reason: StrictStr = ""
    rationale: StrictStr = ""
    confidence: JudgeConfidence = "low"
    evidence: list[StrictStr] = Field(default_factory=list)
    needs_human_review: bool = False
    review_reasons: list[StrictStr] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""
    needs_review_label: str = "F"
    normalization_diagnostics: list[str] = Field(default_factory=list)
    # Machine compatibility only. The new protocol has no composite score.
    answer1_total_score: None = None
    answer2_total_score: None = None
    answer3_total_score: None = None
    overall_winner: None = None
    overall_ranking: None = None
    judge: str | None = None
    judge_model: str | None = None
    judge_latency_ms: int | None = None
    prompt_sha256: str | None = None
    input_manifest_sha256: str | None = None

    @model_validator(mode="after")
    def validate_product_states(self):
        """Reject omissions; repair only contradictions with a known priority."""
        for product in range(1, self.product_count + 1):
            prefix = f"answer{product}"
            required = [f"{prefix}_input_status"]
            for gate in ("response", "safety"):
                required.extend((f"{prefix}_{gate}_gate", f"{prefix}_{gate}_gate_reason"))
            for dimension in QA_CHAT_DIMENSIONS:
                required.extend(f"{prefix}_{dimension}_{suffix}" for suffix in (
                    "applicable", "verification_status", "score_status", "primary_issue", "reason", "evidence", "score",
                ))
            missing = [name for name in required if name not in self.model_fields_set]
            if missing:
                raise ValueError("活动产品缺少必填字段：" + ", ".join(missing))
            if getattr(self, f"{prefix}_input_status") is None:
                raise ValueError(f"{prefix}_input_status 不能为空")
            for gate in ("response", "safety"):
                if getattr(self, f"{prefix}_{gate}_gate") is None:
                    raise ValueError(f"{prefix}_{gate}_gate 不能为空")
                if not (getattr(self, f"{prefix}_{gate}_gate_reason") or "").strip():
                    raise ValueError(f"{prefix}_{gate}_gate_reason 必须说明证据或原因")
            input_failed = getattr(self, f"{prefix}_input_status") == "failed"
            gate_failed = any(getattr(self, f"{prefix}_{gate}_gate") == "fail" for gate in ("response", "safety"))
            if input_failed:
                for gate in ("response", "safety"):
                    setattr(self, f"{prefix}_{gate}_gate", "unclear")
                    reason = getattr(self, f"{prefix}_{gate}_gate_reason")
                    if not reason.startswith("产品输入失败，无法判断；"):
                        setattr(self, f"{prefix}_{gate}_gate_reason", "产品输入失败，无法判断；" + reason)
            for dimension in QA_CHAT_DIMENSIONS:
                field = f"{prefix}_{dimension}"
                status = getattr(self, f"{field}_score_status")
                score = getattr(self, f"{field}_score")
                applicable = getattr(self, f"{field}_applicable")
                verification = getattr(self, f"{field}_verification_status")
                if status is None:
                    raise ValueError(f"{field}_score_status 不能为空")
                if getattr(self, f"{field}_primary_issue") is None:
                    raise ValueError(f"{field}_primary_issue 必须为字符串，无问题时为空字符串")
                if status == "scored" and score is None:
                    raise ValueError(f"{field} scored 缺少严格整数分数")
                if not (getattr(self, f"{field}_reason") or "").strip():
                    raise ValueError(f"{field}_reason 必须说明可见依据或空分原因")
                if input_failed:
                    target = "input_failed"
                    if verification not in (None, "unverifiable"):
                        setattr(self, f"{field}_verification_status", None)
                elif gate_failed:
                    target = "gate_blocked"
                elif applicable is False:
                    target = "not_applicable"
                    setattr(self, f"{field}_verification_status", "not_required")
                elif verification == "unverifiable":
                    target = "unverifiable"
                else:
                    target = status
                    if status == "scored" and (applicable is not True or verification not in ("verified", "partial", "not_required")):
                        raise ValueError(f"{field} scored 必须适用且有可核验证据")
                    if status == "not_applicable":
                        raise ValueError(f"{field} not_applicable 必须 applicable=false")
                    if status == "unverifiable":
                        raise ValueError(f"{field} unverifiable 必须 verification_status=unverifiable")
                    if status in ("input_failed", "gate_blocked"):
                        raise ValueError(f"{field} 阻断状态与产品输入/Gate不一致")
                setattr(self, f"{field}_score_status", target)
                if target != "scored":
                    setattr(self, f"{field}_score", None)
                if target != status or (target != "scored" and score is not None):
                    diagnostic = f"{field}: {status}→{target}，按输入/Gate/适用性优先级归一化"
                    self.normalization_diagnostics.append(diagnostic)
                    setattr(self, f"{field}_reason", getattr(self, f"{field}_reason") + "；" + diagnostic)
        if self.product_count == 2:
            # Persist a uniform flat shape while excluding an inactive product.
            for name in type(self).model_fields:
                if name.startswith("answer3_"):
                    setattr(self, name, [] if name.endswith("_evidence") else None)
        self.normalization_diagnostics = list(dict.fromkeys(self.normalization_diagnostics))
        return self


_fields: dict = {}
for _product in (1, 2, 3):
    _prefix = f"answer{_product}"
    _fields[f"{_prefix}_input_status"] = (InputStatus | None, None)
    for _gate in ("response", "safety"):
        _fields[f"{_prefix}_{_gate}_gate"] = (GateStatus | None, None)
        _fields[f"{_prefix}_{_gate}_gate_reason"] = (StrictStr | None, None)
    for _dimension in QA_CHAT_DIMENSIONS:
        _field = f"{_prefix}_{_dimension}"
        _fields.update({
            f"{_field}_applicable": (StrictBool | None, None),
            f"{_field}_verification_status": (VerificationStatus | None, None),
            f"{_field}_score_status": (QAChatScoreStatus | None, None),
            f"{_field}_primary_issue": (StrictStr | None, None),
            f"{_field}_reason": (StrictStr | None, None),
            f"{_field}_evidence": (list[StrictStr], Field(default_factory=list)),
            f"{_field}_score": (QAChatScore | None, None),
        })
for _dimension in QA_CHAT_DIMENSIONS:
    _fields.update({
        f"{_dimension}_applicable": (StrictBool | None, None),
        f"{_dimension}_verification_status": (VerificationStatus | None, None),
        f"{_dimension}_primary_issue": (str, ""),
        f"{_dimension}_reason": (str, ""),
        f"{_dimension}_evidence": (list[str], Field(default_factory=list)),
        f"{_dimension}_winner": (CompareWinner | None, None),
        f"{_dimension}_rank_groups": (list[list[ProductId]], Field(default_factory=list)),
    })

QAChatObservation = create_model("QAChatObservation", __base__=_QAChatObservationBase, **_fields)


def finalize_qa_chat_observation(observation: QAChatObservation, protocol) -> None:
    """Derive shared display views without feeding them back into product scores."""
    products = range(1, observation.product_count + 1)
    reviews = [*observation.review_reasons, *observation.normalization_diagnostics]
    all_evidence = list(observation.evidence)
    for product in products:
        if getattr(observation, f"answer{product}_input_status") != "complete":
            reviews.append(f"产品{product}输入不完整或失败")
        for gate in ("response", "safety"):
            if getattr(observation, f"answer{product}_{gate}_gate") == "unclear":
                reviews.append(f"产品{product} {gate}_gate 无法判断")
    for dimension in QA_CHAT_DIMENSIONS:
        values = [getattr(observation, f"answer{p}_{dimension}_applicable") for p in products]
        setattr(observation, f"{dimension}_applicable", True if True in values else False if all(v is False for v in values) else None)
        verification = [getattr(observation, f"answer{p}_{dimension}_verification_status") for p in products]
        shared_verification = verification[0] if len(set(verification)) == 1 else "partial"
        if (f"{dimension}_verification_status" in observation.model_fields_set
                and getattr(observation, f"{dimension}_verification_status") != shared_verification):
            reviews.append(f"{QA_CHAT_DIMENSION_NAMES[dimension]}共享核验摘要与产品字段不一致，已按产品字段派生，保留各产品有效分")
        setattr(observation, f"{dimension}_verification_status", shared_verification)
        reasons, evidence, issues, scores = [], [], [], []
        for product in products:
            field = f"answer{product}_{dimension}"
            reasons.append(f"产品{product}：{getattr(observation, field + '_reason')}")
            evidence.extend(getattr(observation, field + "_evidence"))
            issue = getattr(observation, field + "_primary_issue")
            if issue:
                issues.append(f"产品{product}：{issue}")
            if getattr(observation, field + "_verification_status") in ("partial", "unverifiable"):
                reviews.append(f"产品{product} {QA_CHAT_DIMENSION_NAMES[dimension]}证据未完全核验")
            scores.append(getattr(observation, field + "_score"))
        setattr(observation, f"{dimension}_reason", "；".join(reasons))
        setattr(observation, f"{dimension}_primary_issue", "；".join(issues))
        setattr(observation, f"{dimension}_evidence", list(dict.fromkeys(evidence)))
        all_evidence.extend(evidence)
        winner = None if scores[0] is None or scores[1] is None else "tie" if scores[0] == scores[1] else "answer1" if scores[0] > scores[1] else "answer2"
        setattr(observation, f"{dimension}_winner", winner)
        ranks: dict[int, list[str]] = {}
        if all(score is not None for score in scores):
            for product, score in zip(products, scores):
                ranks.setdefault(score, []).append(f"product{product}")
        setattr(observation, f"{dimension}_rank_groups", [ranks[s] for s in sorted(ranks, reverse=True)])
    observation.standard_id = protocol.standard_id
    observation.standard_version = protocol.standard_version
    observation.evaluation_profile = protocol.id
    observation.bundle_revision = protocol.bundle_revision
    observation.result_schema_version = protocol.result_schema_version
    observation.evidence = list(dict.fromkeys(all_evidence))
    observation.review_reasons = list(dict.fromkeys(reviews))
    observation.needs_human_review = bool(observation.needs_human_review or reviews)
    observation.needs_review = observation.needs_human_review
    observation.review_reason = "；".join(observation.review_reasons)
    observation.needs_review_label = "T" if observation.needs_review else "F"
