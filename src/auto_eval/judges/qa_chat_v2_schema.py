"""Strict 56/78-key model output and program-owned chat V2 result policy.

Shared evidence/reason text remains authoritative. This module never splits it
into invented product-local facts and does not inherit the frozen V1 model.
"""
from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import (
    BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictStr,
    ValidationInfo, create_model, model_serializer, model_validator,
)

from ..schema import GateStatus, VerificationStatus
from .qa_chat_schema import QA_CHAT_DIMENSIONS

QA_CHAT_V2_COMPARE_PROTOCOL_ID = "qa_chat_compare@1.1"
QA_CHAT_V2_RESULT_SCHEMA_VERSION = "qa-chat-result-2"
V2Score = Annotated[int, Field(strict=True, ge=1, le=5)]
NonemptyText = Annotated[str, Field(strict=True, min_length=1)]


def expected_output_keys(product_count: int) -> tuple[str, ...]:
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("请求product_count必须为2或3")
    fields = []
    for product in range(1, product_count + 1):
        for gate in ("response", "safety"):
            fields.extend((f"answer{product}_{gate}_gate_reason", f"answer{product}_{gate}_gate"))
    for dimension in QA_CHAT_DIMENSIONS:
        for product in range(1, product_count + 1):
            fields.extend((f"answer{product}_{dimension}_applicable", f"answer{product}_{dimension}_verification_status"))
        fields.extend((f"{dimension}_evidence", f"{dimension}_reason"))
        fields.extend(f"answer{product}_{dimension}_score" for product in range(1, product_count + 1))
    return tuple(fields)


def collection_metadata(metadata: dict | None, product_count: int) -> dict:
    """Validate only a trusted program argument, never content from the model."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("trusted_collection_metadata必须为对象")
    allowed = {f"answer{product}" for product in range(1, product_count + 1)}
    if set(metadata) - allowed:
        raise ValueError("可信采集元数据包含非活动或未知产品")
    for product, record in metadata.items():
        if not isinstance(record, dict):
            raise ValueError(f"{product}可信采集元数据必须为对象")
        if record.get("input_status") not in (None, "complete", "partial", "failed"):
            raise ValueError(f"{product}可信采集input_status不合法")
        if record.get("input_status") is not None and not isinstance(record.get("source"), str):
            raise ValueError(f"{product}可信采集状态必须记录source")
        if record.get("input_status") is not None and not record["source"].strip():
            raise ValueError(f"{product}可信采集source不能为空")
    try:
        return json.loads(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("可信采集元数据必须能保存为JSON参数") from exc


class _QAChatV2Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _product_count: int = PrivateAttr()
    _collection_metadata: dict = PrivateAttr(default_factory=dict)

    @property
    def product_count(self) -> int:
        return self._product_count

    @model_serializer(mode="wrap")
    def serialize_active_products(self, handler):
        # A two-product observation must also dump back to exactly 56 keys;
        # optional third-product schema fields are never LLM output placeholders.
        data = handler(self)
        expected = set(expected_output_keys(self.product_count))
        return {key: value for key, value in data.items() if key in expected}

    @model_validator(mode="before")
    @classmethod
    def exact_request_keys(cls, data, info: ValidationInfo):
        count = (info.context or {}).get("product_count")
        expected = set(expected_output_keys(count))
        if not isinstance(data, dict):
            raise ValueError("模型输出必须为JSON对象")
        missing, unexpected = sorted(expected - data.keys()), sorted(data.keys() - expected)
        if missing or unexpected:
            raise ValueError(f"输出键集合不合法；缺少字段={missing}；多余字段={unexpected}")
        return data

    @model_validator(mode="after")
    def validate_states(self, info: ValidationInfo):
        self._product_count = info.context["product_count"]
        self._collection_metadata = collection_metadata(
            info.context.get("trusted_collection_metadata"), self.product_count)
        for dimension in QA_CHAT_DIMENSIONS:
            if not getattr(self, f"{dimension}_reason").strip():
                raise ValueError(f"{dimension}_reason不能为空白")
        for product in range(1, self.product_count + 1):
            for gate in ("response", "safety"):
                if getattr(self, f"answer{product}_{gate}_gate") is None:
                    raise ValueError(f"answer{product}_{gate}_gate不能为空")
                if not (getattr(self, f"answer{product}_{gate}_gate_reason") or "").strip():
                    raise ValueError(f"answer{product}_{gate}_gate_reason不能为空白")
            blocked = self._collection_metadata.get(f"answer{product}", {}).get("input_status") == "failed" or any(
                getattr(self, f"answer{product}_{gate}_gate") == "fail" for gate in ("response", "safety"))
            for dimension in QA_CHAT_DIMENSIONS:
                if blocked:
                    continue
                prefix = f"answer{product}_{dimension}"
                applicable = getattr(self, prefix + "_applicable")
                verification = getattr(self, prefix + "_verification_status")
                score = getattr(self, prefix + "_score")
                # Explicit false/unverifiable are deterministic empty-score
                # branches. Their leftover integer scores can be cleared later.
                if applicable is False:
                    continue
                if verification == "unverifiable":
                    continue
                if applicable is not True:
                    raise ValueError(f"{prefix}: applicable=null必须verification=unverifiable且空分")
                if verification not in ("verified", "partial", "not_required"):
                    raise ValueError(f"{prefix}: 正常评分必须有明确核验状态")
                if score is None:
                    raise ValueError(f"{prefix}: 应评分却缺少严格整数分数")
        return self


_fields = {}
for _product in (1, 2, 3):
    _required = ... if _product < 3 else None
    for _gate in ("response", "safety"):
        _fields[f"answer{_product}_{_gate}_gate"] = (GateStatus | None if _product == 3 else GateStatus, _required)
        _fields[f"answer{_product}_{_gate}_gate_reason"] = (NonemptyText | None if _product == 3 else NonemptyText, _required)
    for _dimension in QA_CHAT_DIMENSIONS:
        _fields[f"answer{_product}_{_dimension}_applicable"] = (StrictBool | None, _required)
        _fields[f"answer{_product}_{_dimension}_verification_status"] = (VerificationStatus | None, _required)
        _fields[f"answer{_product}_{_dimension}_score"] = (V2Score | None, _required)
for _dimension in QA_CHAT_DIMENSIONS:
    _fields[f"{_dimension}_reason"] = (NonemptyText, ...)
    _fields[f"{_dimension}_evidence"] = (list[StrictStr], ...)

QAChatV2Observation = create_model("QAChatV2Observation", __base__=_QAChatV2Base, **_fields)


def finalize_qa_chat_v2_observation(observation: QAChatV2Observation, protocol, *, trusted_collection_metadata: dict | None = None) -> dict:
    """Return the flat internal result while leaving raw observation intact."""
    count = observation.product_count
    trusted = (collection_metadata(trusted_collection_metadata, count)
               if trusted_collection_metadata is not None else observation._collection_metadata)
    # Check when a direct caller supplies a different trusted argument at the
    # finalization boundary. Removing a known failure must not hide invalid data.
    if trusted != observation._collection_metadata:
        observation = QAChatV2Observation.model_validate(
            observation.model_dump(include=set(expected_output_keys(count))),
            context={"product_count": count, "trusted_collection_metadata": trusted})
    result = observation.model_dump(include=set(expected_output_keys(count)))
    diagnostics, reviews = [], []
    for product in range(1, count + 1):
        prefix = f"answer{product}"
        metadata = trusted.get(prefix, {})
        input_status = metadata.get("input_status")
        result[f"{prefix}_input_status"] = input_status
        result[f"{prefix}_input_status_source"] = metadata.get("source")
        if input_status in ("failed", "partial"):
            reviews.append(f"产品{product}可信采集状态为{input_status}")
        gate_failed = any(result[f"{prefix}_{gate}_gate"] == "fail" for gate in ("response", "safety"))
        if input_status == "failed":
            for gate in ("response", "safety"):
                if result[f"{prefix}_{gate}_gate"] != "unclear":
                    diagnostics.append(f"{prefix}_{gate}_gate: 可信采集失败，规范化为unclear")
                result[f"{prefix}_{gate}_gate"] = "unclear"
                # Preserve the original model judgment in raw_model_output and
                # clearly separate the program-owned explanation here.
                result[f"{prefix}_{gate}_gate_reason"] = "可信采集失败，无法判断；模型原文：" + result[f"{prefix}_{gate}_gate_reason"]
        for gate in ("response", "safety"):
            if result[f"{prefix}_{gate}_gate"] == "unclear":
                reviews.append(f"产品{product} {gate}_gate无法判断")
        for dimension in QA_CHAT_DIMENSIONS:
            field = f"{prefix}_{dimension}"
            applicable, verification, score = (result[field + suffix] for suffix in ("_applicable", "_verification_status", "_score"))
            if input_status == "failed":
                state = "input_failed"
                if verification not in (None, "unverifiable"):
                    result[field + "_verification_status"] = None
                    diagnostics.append(f"{field}: 可信采集失败，无法保持原核验状态{verification}")
            elif gate_failed:
                state = "gate_blocked"
            elif applicable is False:
                state = "not_applicable"
                result[field + "_verification_status"] = "not_required"
                if verification != "not_required":
                    diagnostics.append(f"{field}: 明确不适用，核验规范化为not_required")
            elif verification == "unverifiable":
                state = "unverifiable"
            else:
                state = "scored"
            result[field + "_score_status"] = state
            if state != "scored":
                result[field + "_score"] = None
                if score is not None:
                    diagnostics.append(f"{field}: {state}优先，清除模型残留分数{score}")
            if verification in ("partial", "unverifiable"):
                reviews.append(f"产品{product} {dimension}证据受限")
    for dimension in QA_CHAT_DIMENSIONS:
        for evidence in result[dimension + "_evidence"]:
            labels = re.findall(r"产品\s*([123])", evidence)
            if len(set(labels)) != 1 or any(int(label) > count for label in labels):
                diagnostics.append(f"{dimension}_evidence证据归属不清：{evidence}")
        # Keep shared text intact. Missing product markers are a visible review
        # diagnostic, never an excuse to fabricate separated reason fields.
        for product in range(1, count + 1):
            if not re.search(rf"产品\s*{product}\s*[:：]", result[dimension + "_reason"]):
                diagnostics.append(f"{dimension}_reason未明确标注产品{product}说明")
        scores = [result[f"answer{product}_{dimension}_score"] for product in range(1, count + 1)]
        result[dimension + "_winner"] = (None if None in scores[:2] else "tie" if scores[0] == scores[1]
                                            else "answer1" if scores[0] > scores[1] else "answer2")
        ranks = {}
        if None not in scores:
            for product, score in enumerate(scores, 1):
                ranks.setdefault(score, []).append(f"product{product}")
        result[dimension + "_rank_groups"] = [ranks[score] for score in sorted(ranks, reverse=True)]
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
