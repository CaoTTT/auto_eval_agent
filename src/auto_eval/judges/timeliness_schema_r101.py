"""Strict single-shot timeliness output and program-owned blocking policy.

This protocol deliberately does not inherit either legacy scoring normalizer.
Raw observations retain the model's judgments; only explicit prerequisite
blocking and trusted input failure can remove scores in finalized results.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, PrivateAttr, StrictStr,
    ValidationInfo, create_model, model_serializer, model_validator,
)

from ..schema import GateStatus
from .qa_chat_v2_schema import collection_metadata

TIMELINESS_STANDARD_ID = "timeliness_compare"
TIMELINESS_COMPARE_PROTOCOL_ID = "timeliness_compare@1.0"
TIMELINESS_RESULT_SCHEMA_VERSION = "timeliness-result-2"
TIMELINESS_STANDARD_SHA256 = "9f3d663e213bfb7f6ba66cc2eacc316b6f7eeaba85726957e79e6bf6dad1a9a9"
TIMELINESS_DIMENSIONS = ("understanding", "authority", "timeliness")
TIMELINESS_DIMENSION_NAMES = {
    "understanding": "理解需求",
    "authority": "权威性",
    "timeliness": "时效性",
}
TimelinessScore = Annotated[int, Field(strict=True, ge=1, le=3)]
NonemptyText = Annotated[str, Field(strict=True, min_length=1)]
NaReason = Literal["not_applicable", "insufficient_evidence", "applicability_unknown"]


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    """Return the required ordered base contract (20 or 27 keys).

    Unblocked authority/timeliness null scores additionally require a
    per-product ``na_reason``. Use output_key_errors for the full contract.
    """
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("请求product_count必须为2或3")
    keys = []
    for product in range(1, product_count + 1):
        for gate in ("response", "safety"):
            keys.extend((f"answer{product}_{gate}_gate_reason", f"answer{product}_{gate}_gate"))
    for dimension in TIMELINESS_DIMENSIONS:
        keys.extend((dimension + "_evidence", dimension + "_reason"))
        keys.extend(f"answer{product}_{dimension}_score" for product in range(1, product_count + 1))
    return tuple(keys)


def allowed_output_keys(product_count: int) -> tuple[str, ...]:
    """Return all possible keys; NA keys are conditional, never blanket optional."""
    keys = []
    for key in expected_output_keys(product_count):
        keys.append(key)
        if key.endswith(("_authority_score", "_timeliness_score")):
            keys.append(key.removesuffix("_score") + "_na_reason")
    return tuple(keys)


def output_key_errors(
    data: dict, product_count: int, trusted_collection_metadata: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Report missing/unexpected keys, including conditional NA requirements.

    This helper performs structure checks only. Pydantic still validates every
    value, including residual scores under a prerequisite block.
    """
    expected = set(expected_output_keys(product_count))
    if not isinstance(data, dict):
        raise ValueError("时效性模型输出必须为JSON对象")
    # Collection failures are program-owned outcomes, not model-output fields.
    # A later trusted failure must still be able to normalize a valid raw NA.
    # Raw conditional keys therefore depend only on the visible prerequisites.
    for product in range(1, product_count + 1):
        prefix = f"answer{product}"
        blocked = (
            any(data.get(f"{prefix}_{gate}_gate") == "fail" for gate in ("response", "safety"))
            or data.get(prefix + "_understanding_score") == 1
        )
        for dimension in ("authority", "timeliness"):
            score_key = f"{prefix}_{dimension}_score"
            if not blocked and score_key in data and data[score_key] is None:
                expected.add(f"{prefix}_{dimension}_na_reason")
    return sorted(expected - data.keys()), sorted(data.keys() - expected)


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
        return {
            key: data[key] for key in allowed_output_keys(self.product_count)
            if key in self.model_fields_set
        }

    @model_validator(mode="before")
    @classmethod
    def exact_request_keys(cls, data, info: ValidationInfo):
        context = info.context or {}
        missing, unexpected = output_key_errors(
            data, context.get("product_count"), context.get("trusted_collection_metadata"),
        )
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
        _fields[f"answer{_product}_{_dimension}_score"] = (TimelinessScore | None, _required)
        if _dimension != "understanding":
            # A missing optional field defaults internally to None; an explicit
            # JSON null is invalid. Presence is enforced by output_key_errors.
            _fields[f"answer{_product}_{_dimension}_na_reason"] = (NaReason, None)
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
        # Validate the raw observation when replacing collection metadata; it
        # remains untouched while the finalized execution state is derived.
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
            score = result[field + "_score"]
            na_reason = result.get(field + "_na_reason")
            if input_failed:
                state = "insufficient_evidence"
                applicable, final_score = True if dimension == "understanding" else None, None
            elif gate_failed:
                state = "blocked_gate"
                applicable, final_score = None, None
            elif understanding_failed and dimension != "understanding":
                state = "blocked_understanding"
                applicable, final_score = None, None
            else:
                state = ("scored" if score is not None else
                         "not_applicable" if na_reason == "not_applicable" else
                         "insufficient_evidence")
                applicable = (False if na_reason == "not_applicable" else
                              None if na_reason == "applicability_unknown" else True)
                final_score = score
            result[field + "_score_status"] = state
            result[field + "_applicable"] = applicable
            # Result-2 never asks the judge for verification status. Leave the
            # legacy storage field empty instead of claiming an unobserved check.
            result[field + "_verification_status"] = None
            result[field + "_verification_status_source"] = "not_collected"
            if input_failed and field + "_na_reason" in result:
                diagnostics.append(
                    f"{field}_na_reason: 可信采集失败优先，原值={na_reason!r}，结果省略")
                result.pop(field + "_na_reason")
            if score != final_score:
                diagnostics.append(f"{field}_score: {state}优先，原值={score!r}，结果={final_score!r}")
            result[field + "_score"] = final_score
            if not input_failed and state not in ("blocked_gate", "blocked_understanding"):
                if applicable is None:
                    reviews.append(f"产品{product} {dimension}适用性待核验")
                if state == "insufficient_evidence":
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
