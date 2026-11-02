"""Strict per-product original four-field output and independent overall scores."""
from __future__ import annotations

import json
import math
from typing import Annotated

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr,
    ValidationInfo, model_serializer, model_validator,
)

from .qa_chat_v2_schema import collection_metadata

DIALOGUE_EXPERIENCE_STANDARD_ID = "dialogue_experience_compare"
DIALOGUE_EXPERIENCE_COMPARE_PROTOCOL_ID = "dialogue_experience_compare@1.0"
DIALOGUE_EXPERIENCE_RESULT_SCHEMA_VERSION = "dialogue-experience-result-1"
DIALOGUE_EXPERIENCE_DIMENSIONS = ("overall",)
DIALOGUE_EXPERIENCE_DIMENSION_NAMES = {"overall": "综合体验"}


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("理由、建议和垂域必须是非空字符串")
    return value


OverallScore = Annotated[int, Field(strict=True, ge=1, le=5)]
NonblankText = Annotated[str, Field(strict=True), AfterValidator(_nonblank)]


class ProductExperience(BaseModel):
    """Keep the source rubric's exact four fields, without extra Gates or scores."""

    model_config = ConfigDict(extra="forbid")
    score: OverallScore
    reason: NonblankText
    suggestion: NonblankText
    domain: NonblankText


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("product_count必须是严格整数2或3")
    return tuple(f"answer{n}" for n in range(1, product_count + 1))


class DialogueExperienceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer1: ProductExperience
    answer2: ProductExperience
    answer3: ProductExperience | None = None
    _product_count: int = PrivateAttr()
    _collection_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_validator(mode="before")
    @classmethod
    def exact_active_products(cls, data, info: ValidationInfo):
        expected = set(expected_output_keys((info.context or {}).get("product_count")))
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError(f"对话体验输出必须且只能包含当前产品：{sorted(expected)}")
        if any(data[key] is None for key in expected):
            raise ValueError("每个活动产品必须提供完整四字段对象，不能为null")
        return data

    @model_validator(mode="after")
    def retain_request_context(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        self._collection_metadata = collection_metadata(
            info.context.get("trusted_collection_metadata"), self.product_count,
        )
        return self

    @model_serializer(mode="wrap")
    def active_products_only(self, handler):
        data = handler(self)
        return {key: data[key] for key in expected_output_keys(self.product_count)}


def validate_output(data: dict, product_count: int) -> DialogueExperienceObservation:
    return DialogueExperienceObservation.model_validate(data, context={"product_count": product_count})


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"模型JSON包含重复键：{key}")
        data[key] = value
    return data


def _reject_constant(value: str):
    raise ValueError(f"模型JSON包含非法数值：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("模型JSON数值必须有限")
    return number


def parse_output(raw: str, product_count: int) -> DialogueExperienceObservation:
    """Reject duplicates at every nesting level, coercion and semantic repairs."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出必须是非空JSON文本")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是完整合法JSON：{exc.msg}") from exc
    return validate_output(data, product_count)


def finalize_dialogue_experience_observation(
    observation: DialogueExperienceObservation, protocol, *,
    trusted_collection_metadata: dict | None = None,
) -> dict:
    """Project one overall dimension without changing tentative rubric scores.

    Only a trusted collection failure clears a final score. The unmodified
    model product objects and raw output remain available independently.
    """
    count = observation.product_count
    trusted = (collection_metadata(trusted_collection_metadata, count)
               if trusted_collection_metadata is not None else observation._collection_metadata)
    original = observation.model_dump()
    result = {"model_product_results": original}
    reviews, diagnostics = [], []
    reasons = []
    for n in range(1, count + 1):
        prefix = f"answer{n}"
        value = original[prefix]
        metadata = trusted.get(prefix, {})
        status = metadata.get("input_status")
        failed = status == "failed"
        score = None if failed else value["score"]
        reason = value["reason"]
        if failed:
            reason = "程序记录：可信采集失败，最终空分；模型原理由：" + reason
            diagnostics.append(f"{prefix}: 可信采集失败，保留原总分={value['score']}供审计")
        if status in ("failed", "partial"):
            reviews.append(f"产品{n}可信采集状态为{status}")
        if "证据状态：不足" in reason or "暂评" in reason:
            reviews.append(f"产品{n}证据不足，保留模型暂评分供复核")
        result.update({
            prefix + "_overall_score": score,
            prefix + "_overall_reason": reason,
            prefix + "_suggestion": value["suggestion"],
            prefix + "_domain": value["domain"],
            prefix + "_overall_score_status": "unverifiable" if failed else "scored",
            prefix + "_overall_applicable": True,
            prefix + "_overall_verification_status": None,
            prefix + "_overall_verification_status_source": "not_collected",
            prefix + "_input_status": status,
            prefix + "_input_status_source": metadata.get("source"),
            prefix + "_execution_status": "input_failed" if failed else "evaluated",
        })
        reasons.append(f"产品{n}：{reason}")
    scores = [result[f"answer{n}_overall_score"] for n in range(1, count + 1)]
    comparable = all(score is not None for score in scores)
    rank_groups = []
    if comparable:
        for score in sorted(set(scores), reverse=True):
            rank_groups.append([f"product{n}" for n in range(1, count + 1)
                                if result[f"answer{n}_overall_score"] == score])
    score1, score2 = scores[:2]
    winner = None if score1 is None or score2 is None else (
        "tie" if score1 == score2 else "answer1" if score1 > score2 else "answer2")
    reviews = list(dict.fromkeys(reviews))
    result.update(
        standard_id=protocol.standard_id, standard_version=protocol.standard_version,
        evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
        result_schema_version=protocol.result_schema_version, product_count=count,
        overall_reason="\n".join(reasons), overall_applicable=True,
        overall_winner=winner, overall_rank_groups=rank_groups,
        overall_ranking=rank_groups, trusted_collection_metadata=trusted,
        normalization_diagnostics=diagnostics, structural_diagnostics=[],
        needs_human_review=bool(reviews), review_reasons=reviews,
        needs_review=bool(reviews), review_reason="；".join(reviews),
        needs_review_label="T" if reviews else "F",
    )
    return result
