"""Strict relative GSB judgments, with collection state owned by the program."""
from __future__ import annotations

import json
import math
from itertools import combinations
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr,
    ValidationInfo, model_serializer, model_validator,
)

from .qa_chat_v2_schema import collection_metadata

CHITCHAT_GSB_STANDARD_ID = "chitchat_gsb_compare"
CHITCHAT_GSB_COMPARE_PROTOCOL_ID = "chitchat_gsb_compare@1.0"
CHITCHAT_GSB_RESULT_SCHEMA_VERSION = "chitchat-gsb-result-1"
CHITCHAT_GSB_DIMENSIONS = ("overall",)
CHITCHAT_GSB_DIMENSION_NAMES = {"overall": "整体GSB"}

GSBStatus = Literal["evaluated", "insufficient_evidence", "needs_review"]
GSBVerdict = Literal["G", "S", "B"]
SeriousIssue = Literal["none", "A", "B", "both"]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("GSB理由必须为非空字符串")
    return value


NonblankText = Annotated[str, Field(strict=True), AfterValidator(_nonblank)]


class GSBPairJudgment(BaseModel):
    """A/B refer to the first/second product in this particular pair."""

    model_config = ConfigDict(extra="forbid", strict=True)
    status: GSBStatus
    gsb: GSBVerdict | None
    reason: NonblankText
    serious_issue: SeriousIssue

    @model_validator(mode="after")
    def consistent_verdict(self):
        if (self.status == "evaluated") != (self.gsb is not None):
            raise ValueError("只有evaluated必须填写G/S/B；待补证据或待复核必须gsb=null")
        if self.status == "evaluated" and (
            (self.serious_issue == "A" and self.gsb != "B")
            or (self.serious_issue == "B" and self.gsb != "G")
        ):
            raise ValueError("仅单方有严重问题时必须判该方更差，不能判其获胜或相近")
        return self


def active_product_pairs(product_count: int) -> tuple[tuple[int, int], ...]:
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("product_count必须是严格整数2或3")
    return tuple(combinations(range(1, product_count + 1), 2))


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    return tuple(f"pair_{a}_{b}" for a, b in active_product_pairs(product_count))


class ChitchatGSBObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pair_1_2: GSBPairJudgment
    pair_1_3: GSBPairJudgment | None = None
    pair_2_3: GSBPairJudgment | None = None
    _product_count: int = PrivateAttr()
    _collection_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_validator(mode="before")
    @classmethod
    def exact_active_pairs(cls, data, info: ValidationInfo):
        expected = set(expected_output_keys((info.context or {}).get("product_count")))
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError(f"GSB输出必须且只能包含当前产品对：{sorted(expected)}")
        if any(data[key] is None for key in expected):
            raise ValueError("每个活动产品对必须提供完整四字段对象，不能为null")
        return data

    @model_validator(mode="after")
    def retain_request_context(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        self._collection_metadata = collection_metadata(
            info.context.get("trusted_collection_metadata"), self.product_count,
        )
        return self

    @model_serializer(mode="wrap")
    def active_pairs_only(self, handler):
        data = handler(self)
        return {key: data[key] for key in expected_output_keys(self.product_count)}


def validate_output(data: dict, product_count: int) -> ChitchatGSBObservation:
    return ChitchatGSBObservation.model_validate(data, context={"product_count": product_count})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型JSON包含重复键：{key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"模型JSON包含非法数值：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("模型JSON数值必须有限")
    return number


def parse_output(raw: str, product_count: int) -> ChitchatGSBObservation:
    """Reject duplicate keys, invalid JSON and coercion without semantic repair."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型输出必须是非空JSON文本")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出不是完整合法JSON：{exc.msg}") from exc
    return validate_output(data, product_count)


def _supported_serious_issue(value: SeriousIssue, failed_sides: set[str]) -> SeriousIssue:
    sides = {"A", "B"} if value == "both" else set() if value == "none" else {value}
    sides -= failed_sides
    if sides == {"A", "B"}:
        return "both"
    return next(iter(sides)) if sides else "none"


def finalize_chitchat_gsb_observation(
    observation: ChitchatGSBObservation, protocol, *,
    trusted_collection_metadata: dict | None = None,
) -> dict:
    """Preserve model judgments, excluding only pairs with trusted input failure."""
    count = observation.product_count
    trusted = (collection_metadata(trusted_collection_metadata, count)
               if trusted_collection_metadata is not None else observation._collection_metadata)
    original = observation.model_dump()
    pairs, reviews, diagnostics = {}, [], []
    for a, b in active_product_pairs(count):
        key = f"{a}_{b}"
        value = dict(original[f"pair_{key}"])
        failed = {
            side for side, product in (("A", a), ("B", b))
            if trusted.get(f"answer{product}", {}).get("input_status") == "failed"
        }
        if failed:
            products = "、".join(str(product) for side, product in (("A", a), ("B", b))
                                 if side in failed)
            value.update(
                status="insufficient_evidence", gsb=None,
                reason=f"程序记录：产品{products}可信采集失败，本产品对待补证据；模型原理由：" + value["reason"],
                serious_issue=_supported_serious_issue(value["serious_issue"], failed),
            )
            diagnostics.append(f"pair_{key}: 可信采集失败，最终GSB留空；模型原判断保留在model_pair_results")
        value.update(product_a=a, product_b=b)
        pairs[key] = value
        if value["status"] != "evaluated":
            label = "待补证据" if value["status"] == "insufficient_evidence" else "待复核"
            reviews.append(f"产品{a}对产品{b}{label}：{value['reason']}")
    return {
        "standard_id": protocol.standard_id,
        "standard_version": protocol.standard_version,
        "evaluation_profile": protocol.id,
        "bundle_revision": protocol.bundle_revision,
        "result_schema_version": protocol.result_schema_version,
        "product_count": count,
        "gsb_pairs": pairs,
        "model_pair_results": original,
        "trusted_collection_metadata": trusted,
        "normalization_diagnostics": diagnostics,
        "structural_diagnostics": [],
        "needs_human_review": bool(reviews),
        "review_reasons": reviews,
        "needs_review": bool(reviews),
        "review_reason": "；".join(reviews),
        "needs_review_label": "T" if reviews else "F",
    }
