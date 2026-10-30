"""Strict seven-dimension chat output with evidence merged into each reason.

All seven scores are independent. Response and safety are numeric dimensions,
not legacy Gates, so their failure must never clear other dimension scores.
"""
from __future__ import annotations

import json
import math
import re
from typing import Annotated

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, StrictInt,
    ValidationInfo, create_model, model_serializer, model_validator,
)

from .qa_chat_v2_schema import collection_metadata

CHITCHAT_FLAT_STANDARD_ID = "chitchat_evidence_first_flat"
CHITCHAT_FLAT_COMPARE_PROTOCOL_ID = "chitchat_evidence_first_flat@1.1"
CHITCHAT_FLAT_RESULT_SCHEMA_VERSION = "chitchat-flat-result-1"
CHITCHAT_FLAT_DIMENSIONS = (
    "response_experience", "safety_stability", "understanding",
    "emotional_expression", "style", "scenario_fulfillment",
    "guided_recommendation",
)
CHITCHAT_FLAT_DIMENSION_NAMES = {
    "response_experience": "响应体验",
    "safety_stability": "安全稳定",
    "understanding": "理解需求",
    "emotional_expression": "情感表达恰当",
    "style": "风格恰当",
    "scenario_fulfillment": "场景化满足",
    "guided_recommendation": "引导推荐",
}
CHITCHAT_FLAT_ALLOWED_SCORES = {
    dimension: ((1, 5) if index < 2 else (1, 2, 3, 4, 5))
    for index, dimension in enumerate(CHITCHAT_FLAT_DIMENSIONS)
}


def _binary_score(value: int) -> int:
    if value not in (1, 5):
        raise ValueError("响应体验和安全稳定只允许整数1或5")
    return value


def _nonblank_text(value: str) -> str:
    if not value.strip():
        raise ValueError("证据理由不能为空白")
    return value


BinaryScore = Annotated[StrictInt, AfterValidator(_binary_score)]
DimensionScore = Annotated[int, Field(strict=True, ge=1, le=5)]
NonemptyText = Annotated[
    str, Field(strict=True, min_length=1), AfterValidator(_nonblank_text),
]


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    """Return the canonical reason-before-score contract: 21 or 28 fields."""
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("请求product_count必须为严格整数2或3")
    keys = []
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        keys.append(dimension + "_reason")
        keys.extend(
            f"answer{product}_{dimension}_score"
            for product in range(1, product_count + 1)
        )
    return tuple(keys)


class _ChitchatFlatBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _product_count: int = PrivateAttr()
    _collection_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_validator(mode="before")
    @classmethod
    def exact_request_keys(cls, data, info: ValidationInfo):
        expected = set(expected_output_keys((info.context or {}).get("product_count")))
        if not isinstance(data, dict):
            raise ValueError("闲聊七维模型输出必须为JSON对象")
        missing = sorted(expected - data.keys())
        unexpected = sorted(str(key) for key in data.keys() - expected)
        if missing or unexpected:
            raise ValueError(f"输出键集合不合法；缺少字段={missing}；多余字段={unexpected}")
        return data

    @model_validator(mode="after")
    def store_request_context(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        self._collection_metadata = collection_metadata(
            info.context.get("trusted_collection_metadata"), self.product_count,
        )
        return self

    @model_serializer(mode="wrap")
    def serialize_active_products(self, handler):
        data = handler(self)
        # JSON object member order is not a semantic scoring error. Preserve
        # model judgments and canonicalize display without a second model call.
        return {
            key: data[key] for key in expected_output_keys(self.product_count)
            if key in data
        }


_fields = {}
for _dimension in CHITCHAT_FLAT_DIMENSIONS:
    _fields[_dimension + "_reason"] = (NonemptyText, ...)
    _score_type = BinaryScore if _dimension in CHITCHAT_FLAT_DIMENSIONS[:2] else DimensionScore
    for _product in (1, 2, 3):
        _fields[f"answer{_product}_{_dimension}_score"] = (
            _score_type | None, ... if _product < 3 else None,
        )

ChitchatFlatObservation = create_model(
    "ChitchatFlatObservation", __base__=_ChitchatFlatBase, **_fields,
)


def validate_output(data: dict, product_count: int) -> ChitchatFlatObservation:
    """Validate a complete candidate response without changing its judgments."""
    return ChitchatFlatObservation.model_validate(
        data, context={"product_count": product_count},
    )


def finalize_chitchat_flat_observation(
    observation: ChitchatFlatObservation, protocol, *,
    trusted_collection_metadata: dict | None = None,
) -> dict:
    """Add program-owned storage metadata without legacy Gate cascading.

    A trusted collection failure invalidates only the affected product's final
    scores. Raw observations and shared reasons remain available for audit.
    Null means insufficient decisive evidence, never non-applicability or zero.
    """
    count = observation.product_count
    trusted = (collection_metadata(trusted_collection_metadata, count)
               if trusted_collection_metadata is not None else observation._collection_metadata)
    result = observation.model_dump()
    diagnostics, reviews, failed_products = [], [], []
    for product in range(1, count + 1):
        prefix = f"answer{product}"
        metadata = trusted.get(prefix, {})
        input_status = metadata.get("input_status")
        input_failed = input_status == "failed"
        if input_failed:
            failed_products.append(product)
        result[prefix + "_input_status"] = input_status
        result[prefix + "_input_status_source"] = metadata.get("source")
        result[prefix + "_execution_status"] = "input_failed" if input_failed else "evaluated"
        if input_status in ("failed", "partial"):
            reviews.append(f"产品{product}可信采集状态为{input_status}")
        for dimension in CHITCHAT_FLAT_DIMENSIONS:
            field = f"{prefix}_{dimension}"
            raw_score = result[field + "_score"]
            score = None if input_failed else raw_score
            result[field + "_score"] = score
            result[field + "_score_status"] = "scored" if score is not None else "unverifiable"
            # All seven dimensions apply to chat; absent emotion or a needless
            # follow-up is not non-applicability. Verification was not requested.
            result[field + "_applicable"] = True
            result[field + "_verification_status"] = None
            result[field + "_verification_status_source"] = "not_collected"
            if input_failed:
                diagnostics.append(
                    f"{field}: 可信采集失败，最终空分；保留模型原分数={raw_score!r}供审计"
                )
            if score is None:
                reviews.append(f"产品{product} {dimension}证据受限")
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        raw_reason = result[dimension + "_reason"]
        if failed_products:
            labels = "、".join(f"产品{product}" for product in failed_products)
            result[dimension + "_reason"] = (
                f"程序记录：{labels}可信采集失败，最终空分；仅这些产品的原判断供审计，"
                "不影响其他产品的有效评分。原模型逐产品理由：" + raw_reason
            )
        for product in range(1, count + 1):
            # Explicit shared labels such as 产品1、产品2 are valid too. This
            # diagnostic checks attribution only, never semantic score quality.
            if not re.search(rf"产品\s*{product}(?!\d)", raw_reason):
                diagnostics.append(f"{dimension}_reason未明确标注产品{product}说明")
    diagnostics = list(dict.fromkeys(diagnostics))
    reviews = list(dict.fromkeys([*reviews, *diagnostics]))
    result.update(
        standard_id=protocol.standard_id, standard_version=protocol.standard_version,
        evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
        result_schema_version=protocol.result_schema_version, product_count=count,
        trusted_collection_metadata=trusted, normalization_diagnostics=diagnostics,
        structural_diagnostics=[], needs_human_review=bool(reviews), review_reasons=reviews,
        needs_review=bool(reviews), review_reason="；".join(reviews),
        needs_review_label="T" if reviews else "F",
    )
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型输出存在重复键：{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ValueError(f"模型输出包含非法JSON常量：{value}")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("模型输出的JSON数值必须有限")
    return number


def parse_output(raw: str, product_count: int) -> ChitchatFlatObservation:
    """Parse strict JSON; reject duplicate keys, NaN and format repair."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出必须为非空JSON文本")
    try:
        data = json.loads(
            raw, object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant, parse_float=_finite_json_float,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是完整合法JSON文本：{exc.msg}") from exc
    return validate_output(data, product_count)
