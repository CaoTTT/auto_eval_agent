"""Strict single-shot timeliness output and program-owned blocking policy.

This protocol deliberately does not inherit either legacy scoring normalizer.
Raw observations retain the model's judgments; only explicit prerequisite
blocking and trusted input failure can remove scores in finalized results.
"""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import (
    BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictStr,
    ValidationInfo, create_model, model_serializer, model_validator,
)

from ..schema import GateStatus, VerificationStatus
from .qa_chat_v2_schema import collection_metadata

TIMELINESS_STANDARD_ID = "timeliness_compare"
TIMELINESS_COMPARE_PROTOCOL_ID = "timeliness_compare@1.0"
TIMELINESS_RESULT_SCHEMA_VERSION = "timeliness-result-1"
TIMELINESS_STANDARD_SHA256 = "9f3d663e213bfb7f6ba66cc2eacc316b6f7eeaba85726957e79e6bf6dad1a9a9"
TIMELINESS_DIMENSIONS = ("understanding", "authority", "timeliness")
TIMELINESS_DIMENSION_NAMES = {
    "understanding": "理解需求",
    "authority": "权威性",
    "timeliness": "时效性",
}
TimelinessScore = Annotated[int, Field(strict=True, ge=1, le=3)]
NonemptyText = Annotated[str, Field(strict=True, min_length=1)]


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    """Return the complete ordered model-output contract (32 or 45 keys)."""
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("请求product_count必须为2或3")
    keys = []
    for product in range(1, product_count + 1):
        for gate in ("response", "safety"):
            keys.extend((f"answer{product}_{gate}_gate_reason", f"answer{product}_{gate}_gate"))
    for dimension in TIMELINESS_DIMENSIONS:
        for product in range(1, product_count + 1):
            prefix = f"answer{product}_{dimension}"
            keys.extend((prefix + "_applicable", prefix + "_verification_status"))
        keys.extend((dimension + "_evidence", dimension + "_reason"))
        keys.extend(f"answer{product}_{dimension}_score" for product in range(1, product_count + 1))
    return tuple(keys)


class _TimelinessBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _product_count: int = PrivateAttr()
    _collection_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_serializer(mode="wrap")
    def serialize_active_products(self, handler):
        data = handler(self)
        # Canonical display order is evidence/reason before score. Input JSON
        # ordering is not semantic and never causes a second evaluation.
        return {key: data[key] for key in expected_output_keys(self.product_count) if key in data}

    @model_validator(mode="before")
    @classmethod
    def exact_request_keys(cls, data, info: ValidationInfo):
        expected = set(expected_output_keys((info.context or {}).get("product_count")))
        if not isinstance(data, dict):
            raise ValueError("时效性模型输出必须为JSON对象")
        missing = sorted(expected - data.keys())
        unexpected = sorted(data.keys() - expected)
        if missing or unexpected:
            raise ValueError(f"输出键集合不合法；缺少字段={missing}；多余字段={unexpected}")
        return data

    @model_validator(mode="after")
    def validate_states(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        self._collection_metadata = collection_metadata(
            info.context.get("trusted_collection_metadata"), self.product_count)
        for dimension in TIMELINESS_DIMENSIONS:
            if not getattr(self, dimension + "_reason").strip():
                raise ValueError(f"{dimension}_reason不能为空白")
        for product in range(1, self.product_count + 1):
            prefix = f"answer{product}"
            for gate in ("response", "safety"):
                if getattr(self, f"{prefix}_{gate}_gate") is None:
                    raise ValueError(f"{prefix}_{gate}_gate不能为空")
                if not (getattr(self, f"{prefix}_{gate}_gate_reason") or "").strip():
                    raise ValueError(f"{prefix}_{gate}_gate_reason不能为空白")
            input_failed = self._collection_metadata.get(prefix, {}).get("input_status") == "failed"
            gate_failed = any(getattr(self, f"{prefix}_{gate}_gate") == "fail" for gate in ("response", "safety"))
            understanding_failed = getattr(self, prefix + "_understanding_score") == 1
            for dimension in TIMELINESS_DIMENSIONS:
                # Blocked residuals remain raw observations for audit. The
                # finalizer clears all three fields before any statistics.
                if input_failed or gate_failed or (understanding_failed and dimension != "understanding"):
                    continue
                field = prefix + "_" + dimension
                applicable = getattr(self, field + "_applicable")
                verification = getattr(self, field + "_verification_status")
                score = getattr(self, field + "_score")
                if dimension == "understanding":
                    valid = applicable is True and (
                        (verification in ("verified", "partial") and score is not None)
                        or (verification == "unverifiable" and score is None)
                    )
                else:
                    valid = (
                        (applicable is False and verification == "not_required" and score is None)
                        or (applicable is None and verification == "unverifiable" and score is None)
                        or (applicable is True and (
                            (verification == "verified" and score is not None)
                            or (verification == "partial" and score == 1)
                            or (verification == "unverifiable" and score is None)
                        ))
                    )
                if not valid:
                    raise ValueError(
                        f"{field}: applicable/verification_status/score组合不合法；"
                        "理解始终适用；核心缺证为空分；权威/时效partial仅可配决定性1分"
                    )
        return self


_fields = {}
for _product in (1, 2, 3):
    _required = ... if _product < 3 else None
    for _gate in ("response", "safety"):
        _fields[f"answer{_product}_{_gate}_gate_reason"] = (
            NonemptyText | None if _product == 3 else NonemptyText, _required)
        _fields[f"answer{_product}_{_gate}_gate"] = (
            GateStatus | None if _product == 3 else GateStatus, _required)
    for _dimension in TIMELINESS_DIMENSIONS:
        _fields[f"answer{_product}_{_dimension}_applicable"] = (StrictBool | None, _required)
        _fields[f"answer{_product}_{_dimension}_verification_status"] = (VerificationStatus | None, _required)
        _fields[f"answer{_product}_{_dimension}_score"] = (TimelinessScore | None, _required)
for _dimension in TIMELINESS_DIMENSIONS:
    _fields[f"{_dimension}_evidence"] = (list[StrictStr], ...)
    _fields[f"{_dimension}_reason"] = (NonemptyText, ...)

TimelinessObservation = create_model("TimelinessObservation", __base__=_TimelinessBase, **_fields)


def finalize_timeliness_observation(
    observation: TimelinessObservation, protocol, *, trusted_collection_metadata: dict | None = None,
) -> dict:
    """Derive score states and audit metadata without inventing semantic scores."""
    count = observation.product_count
    trusted = (collection_metadata(trusted_collection_metadata, count)
               if trusted_collection_metadata is not None else observation._collection_metadata)
    if trusted != observation._collection_metadata:
        # A caller cannot clear a known input failure to reveal invalid scores.
        observation = TimelinessObservation.model_validate(
            observation.model_dump(),
            context={"product_count": count, "trusted_collection_metadata": trusted},
        )
    result = observation.model_dump()
    diagnostics, reviews = [], []
    for product in range(1, count + 1):
        prefix = f"answer{product}"
        metadata = trusted.get(prefix, {})
        input_status = metadata.get("input_status")
        input_failed = input_status == "failed"
        result[prefix + "_input_status"] = input_status
        result[prefix + "_input_status_source"] = metadata.get("source")
        # Input failure is an execution/collection outcome, not a product Gate.
        result[prefix + "_execution_status"] = "input_failed" if input_failed else "evaluated"
        if input_status in ("failed", "partial"):
            reviews.append(f"产品{product}可信采集状态为{input_status}")
        gate_failed = any(result[f"{prefix}_{gate}_gate"] == "fail" for gate in ("response", "safety"))
        understanding_failed = result[prefix + "_understanding_score"] == 1
        if input_failed:
            for gate in ("response", "safety"):
                key = f"{prefix}_{gate}_gate"
                diagnostics.append(f"{key}: 可信采集失败，无法归因产品，结果置unclear；原值={result[key]}")
                result[key] = "unclear"
                result[key + "_reason"] = "程序记录：可信采集失败，无法判断；模型原文：" + result[key + "_reason"]
        for gate in ("response", "safety"):
            if result[f"{prefix}_{gate}_gate"] == "unclear":
                reviews.append(f"产品{product} {gate}_gate无法判断")
        for dimension in TIMELINESS_DIMENSIONS:
            field = prefix + "_" + dimension
            applicable, verification, score = (
                result[field + suffix] for suffix in ("_applicable", "_verification_status", "_score"))
            if input_failed:
                state = "insufficient_evidence"
                new_values = (True if dimension == "understanding" else None, "unverifiable", None)
            elif gate_failed:
                state = "blocked_gate"
                new_values = (None, None, None)
            elif understanding_failed and dimension != "understanding":
                state = "blocked_understanding"
                new_values = (None, None, None)
            else:
                state = ("not_applicable" if applicable is False
                         else "insufficient_evidence" if verification == "unverifiable"
                         else "scored")
                new_values = (applicable, verification, score)
            result[field + "_score_status"] = state
            for suffix, old, new in zip(
                ("_applicable", "_verification_status", "_score"),
                (applicable, verification, score), new_values,
            ):
                if old != new:
                    diagnostics.append(f"{field}{suffix}: {state}优先，原值={old!r}，结果={new!r}")
                result[field + suffix] = new
            if not input_failed and state not in ("blocked_gate", "blocked_understanding"):
                if applicable is None:
                    reviews.append(f"产品{product} {dimension}适用性待核验")
                if verification in ("partial", "unverifiable"):
                    reviews.append(f"产品{product} {dimension}证据受限")
    for dimension in TIMELINESS_DIMENSIONS:
        for evidence in result[dimension + "_evidence"]:
            labels = re.findall(r"产品\s*([123])", evidence)
            if len(set(labels)) != 1 or any(int(label) > count for label in labels):
                diagnostics.append(f"{dimension}_evidence证据归属不清：{evidence}")
        for product in range(1, count + 1):
            if not re.search(rf"产品\s*{product}\s*[:：]", result[dimension + "_reason"]):
                diagnostics.append(f"{dimension}_reason未明确标注产品{product}说明")
    diagnostics = list(dict.fromkeys(diagnostics))
    reviews = list(dict.fromkeys([*reviews, *diagnostics]))
    result.update(
        standard_id=protocol.standard_id, standard_version=protocol.standard_version,
        evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
        result_schema_version=protocol.result_schema_version, product_count=count,
        standard_source_sha256=TIMELINESS_STANDARD_SHA256,
        trusted_collection_metadata=trusted, normalization_diagnostics=diagnostics,
        structural_diagnostics=[], needs_human_review=bool(reviews), review_reasons=reviews,
        needs_review=bool(reviews), review_reason="；".join(reviews),
        needs_review_label="T" if reviews else "F",
    )
    return result
