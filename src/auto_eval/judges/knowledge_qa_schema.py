"""Standalone candidate output contract for the knowledge QA rubric.

This module is deliberately not registered as an executable compare protocol.
It retains the existing flat ``answerN`` naming without inheriting legacy
Gate normalization, winner fields, or score aggregation.
"""
from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr,
    StrictFloat, StrictInt, StrictStr, ValidationInfo, create_model,
    model_serializer, model_validator,
)

KNOWLEDGE_QA_STANDARD_ID = "knowledge_qa_compare"
KNOWLEDGE_QA_RESULT_SCHEMA_VERSION = "knowledge-qa-result-1"
KNOWLEDGE_QA_DIMENSIONS = (
    "accuracy", "clarity_and_structure", "content_quality", "multimedia_value",
)
KNOWLEDGE_QA_DIMENSION_NAMES = {
    "accuracy": "准确性",
    "clarity_and_structure": "回复清晰易懂与呈现形式结构化",
    "content_quality": "丰富程度、简洁度与专业性",
    "multimedia_value": "富媒体增益",
}
KNOWLEDGE_QA_ALLOWED_SCORES = {
    "accuracy": (0, 1, 1.5, 2),
    "clarity_and_structure": (0, 1, 2),
    "content_quality": (0, 1, 2),
    "multimedia_value": (0, 1, 2),
}
ScoreStatus = Literal[
    "scored", "not_applicable", "insufficient_evidence", "unverifiable",
]


def _accuracy_score(value):
    # Do not coerce strings, bools, integral floats, or non-finite JSON numbers.
    if type(value) is int and value in (0, 1, 2):
        return value
    if type(value) is float and math.isfinite(value) and value == 1.5:
        return value
    raise ValueError("准确性分数必须为严格整数0、1、2或小数1.5")


def _quality_score(value: int) -> int:
    if value not in (0, 1, 2):
        raise ValueError("该维度分数必须为严格整数0、1、2")
    return value


def _nonblank_text(value: str) -> str:
    if not value.strip():
        raise ValueError("评分理由不能为空白")
    return value


AccuracyScore = Annotated[StrictInt | StrictFloat, BeforeValidator(_accuracy_score)]
QualityScore = Annotated[StrictInt, AfterValidator(_quality_score)]
NonemptyText = Annotated[
    str, Field(strict=True, min_length=1), AfterValidator(_nonblank_text),
]


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    """Return exactly 24 keys for two products or 32 for three products."""
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("请求product_count必须为严格整数2或3")
    keys = []
    for dimension in KNOWLEDGE_QA_DIMENSIONS:
        keys.extend((dimension + "_evidence", dimension + "_reason"))
        for product in range(1, product_count + 1):
            prefix = f"answer{product}_{dimension}"
            keys.extend((prefix + "_status", prefix + "_score"))
    return tuple(keys)


class _KnowledgeQABase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _product_count: int = PrivateAttr()

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_validator(mode="before")
    @classmethod
    def exact_request_keys(cls, data, info: ValidationInfo):
        count = (info.context or {}).get("product_count")
        expected = set(expected_output_keys(count))
        if not isinstance(data, dict):
            raise ValueError("知识问答模型输出必须为JSON对象")
        missing = sorted(expected - data.keys())
        unexpected = sorted(str(key) for key in data.keys() - expected)
        if missing or unexpected:
            raise ValueError(f"输出键集合不合法；缺少字段={missing}；多余字段={unexpected}")
        return data

    @model_validator(mode="after")
    def validate_states(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        for dimension in KNOWLEDGE_QA_DIMENSIONS:
            for product in range(1, self.product_count + 1):
                prefix = f"answer{product}_{dimension}"
                status = getattr(self, prefix + "_status")
                score = getattr(self, prefix + "_score")
                if status is None:
                    raise ValueError(f"{prefix}: 活动产品的评分状态不能为空")
                if (status == "scored") != (score is not None):
                    raise ValueError(f"{prefix}: 仅scored状态必须且允许包含数值分")
                if status == "not_applicable" and dimension != "multimedia_value":
                    raise ValueError(f"{prefix}: 仅富媒体增益允许not_applicable；缺证不能当不适用")
        return self

    @model_serializer(mode="wrap")
    def serialize_active_products(self, handler):
        data = handler(self)
        return {
            key: data[key] for key in expected_output_keys(self.product_count)
            if key in data
        }


_fields = {}
for _dimension in KNOWLEDGE_QA_DIMENSIONS:
    _fields[_dimension + "_evidence"] = (list[StrictStr], ...)
    _fields[_dimension + "_reason"] = (NonemptyText, ...)
    for _product in (1, 2, 3):
        _required = ... if _product < 3 else None
        _prefix = f"answer{_product}_{_dimension}"
        _score_type = AccuracyScore if _dimension == "accuracy" else QualityScore
        _fields[_prefix + "_status"] = (
            ScoreStatus | None if _product == 3 else ScoreStatus, _required,
        )
        _fields[_prefix + "_score"] = (_score_type | None, _required)

KnowledgeQAObservation = create_model(
    "KnowledgeQAObservation", __base__=_KnowledgeQABase, **_fields,
)


def validate_output(data: dict, product_count: int) -> KnowledgeQAObservation:
    """Validate a complete candidate response without modifying any judgment."""
    return KnowledgeQAObservation.model_validate(
        data, context={"product_count": product_count},
    )


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


def parse_output(raw: str, product_count: int) -> KnowledgeQAObservation:
    """Parse one strict JSON object; never repair or overwrite model judgments.

    Duplicate keys are rejected at every object depth before constructing a
    dictionary. Markdown fences, commentary, empty output and non-JSON numeric
    constants are invalid rather than recoverable formatting variants.
    """
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
