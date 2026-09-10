"""Versioned protocol registry for the visual comparison evaluator.

An evaluation protocol is more than a prompt: it freezes the prompt templates,
output schema, score range, gate semantics and user-facing lifecycle state.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, TypeAlias

from jinja2 import Template
from pydantic import BaseModel, create_model, model_validator

from ..schema import VisualCompareObservation
from .visual_compare_prompt_v02_simplified import (
    VISUAL_COMPARE_SYSTEM as V02_SYSTEM,
    VISUAL_COMPARE_USER as V02_USER,
)
from .visual_compare_prompt_v03 import (
    VISUAL_COMPARE_SYSTEM as V03_SYSTEM,
    VISUAL_COMPARE_USER as V03_USER,
)


STANDARD_ID = "qa_competitor_compare"
DEFAULT_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.2-simplified"
V03_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.3"
DIMENSIONS = (
    "understanding",
    "accuracy",
    "service_closure",
    "scenario_fulfillment",
    "intuitive_efficiency",
    "evidence_quality",
    "guided_recommendation",
)

V02QualityScore: TypeAlias = Literal[1, 2, 3, 4, 5]


class _VisualCompareObservationV02Base(VisualCompareObservation):
    """V0.2 state rules; score fields are overridden below to accept 1..5."""

    standard_version: str = "0.2-simplified"

    @model_validator(mode="after")
    def normalize_compare_states(self):
        product_nos = (1, 2, 3) if self.product_count == 3 else (1, 2)
        if self.product_count == 2:
            self.answer3_input_status = None
            self.answer3_response_gate = None
            self.answer3_safety_gate = None
            for dimension in DIMENSIONS:
                setattr(self, f"answer3_{dimension}_score", None)

        for dimension in DIMENSIONS:
            if not getattr(self, f"{dimension}_applicable"):
                for answer_no in (1, 2, 3):
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)
                setattr(self, f"{dimension}_winner", None)
                setattr(self, f"{dimension}_rank_groups", [])
            if getattr(self, f"{dimension}_verification_status") == "unverifiable":
                for answer_no in product_nos:
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)
            for answer_no in product_nos:
                input_failed = getattr(self, f"answer{answer_no}_input_status") == "failed"
                response_pass = getattr(self, f"answer{answer_no}_response_gate") == "pass"
                if input_failed or not response_pass:
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)
        return self


_v02_score_fields = {
    f"answer{answer_no}_{dimension}_score": (V02QualityScore | None, None)
    for answer_no in (1, 2, 3)
    for dimension in DIMENSIONS
}
VisualCompareObservationV02 = create_model(
    "VisualCompareObservationV02",
    __base__=_VisualCompareObservationV02Base,
    **_v02_score_fields,
)


@dataclass(frozen=True)
class CompareProtocol:
    id: str
    standard_id: str
    standard_version: str
    bundle_revision: str
    display: str
    status: Literal["stable", "experimental", "deprecated"]
    system_template: Template
    user_template: Template
    observation_model: type[BaseModel]
    require_response_pass: bool
    score_min: int
    score_max: int

    def public_metadata(self) -> dict:
        return {
            "id": self.id,
            "standard_id": self.standard_id,
            "standard_version": self.standard_version,
            "bundle_revision": self.bundle_revision,
            "display": self.display,
            "status": self.status,
            "score_range": [self.score_min, self.score_max],
            "modes": ["compare"],
            "input_modalities": ["text"] if self.bundle_revision in {"0.2.0", "0.3.0"} else ["text", "text_image"],
        }


_PROTOCOLS = {
    DEFAULT_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=DEFAULT_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version="0.2-simplified",
        bundle_revision="0.2.1",
        display="V0.2 简化版（稳定）",
        status="stable",
        system_template=V02_SYSTEM,
        user_template=V02_USER,
        observation_model=VisualCompareObservationV02,
        require_response_pass=True,
        score_min=1,
        score_max=5,
    ),
    V03_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=V03_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version="0.3",
        bundle_revision="0.3.1",
        display="V0.3（实验）",
        status="experimental",
        system_template=V03_SYSTEM,
        user_template=V03_USER,
        observation_model=VisualCompareObservation,
        require_response_pass=False,
        score_min=0,
        score_max=3,
    ),
}


def list_compare_protocols() -> list[CompareProtocol]:
    return list(_PROTOCOLS.values())


def resolve_compare_protocol(protocol_id: str | None, bundle_revision: str | None = None) -> CompareProtocol:
    selected = protocol_id or DEFAULT_COMPARE_PROTOCOL_ID
    try:
        protocol = _PROTOCOLS[selected]
        if bundle_revision and bundle_revision != protocol.bundle_revision:
            legacy = "0.2.0" if selected == DEFAULT_COMPARE_PROTOCOL_ID else "0.3.0"
            if bundle_revision != legacy:
                raise ValueError("无法恢复任务冻结的实现版本，请新建任务")
            return replace(protocol, bundle_revision=legacy)
        return protocol
    except KeyError as exc:
        supported = ", ".join(_PROTOCOLS)
        raise ValueError(f"未知评测协议 {selected!r}；可选值：{supported}") from exc

