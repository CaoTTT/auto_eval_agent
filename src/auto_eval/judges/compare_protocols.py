"""Versioned protocol registry for the visual comparison evaluator.

An evaluation protocol is more than a prompt: it freezes the prompt templates,
output schema, score range, gate semantics and user-facing lifecycle state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, TypeAlias

from jinja2 import Template
from pydantic import BaseModel, create_model, model_validator

from ..schema import VisualCompareObservation
from .compare_1022_contract import (
    OUTPUT_CONTRACT_1022, STANDARD_VERSION_1022, DIMENSIONS_1022, Compare1022DualOutput,
)
from .visual_compare_prompt_v02_1022 import (
    VISUAL_COMPARE_SYSTEM as V1022_SYSTEM,
    VISUAL_COMPARE_USER as V1022_USER,
)
from .visual_compare_prompt_v02_simplified import (
    VISUAL_COMPARE_SYSTEM as V02_SYSTEM,
    VISUAL_COMPARE_USER as V02_USER,
)
from .visual_compare_prompt_v02_simplified_plus import (
    VISUAL_COMPARE_SYSTEM as V02_PLUS_SYSTEM,
    VISUAL_COMPARE_USER as V02_PLUS_USER,
)
from .visual_compare_prompt_v03 import (
    VISUAL_COMPARE_SYSTEM as V03_SYSTEM,
    VISUAL_COMPARE_USER as V03_USER,
)
from .visual_compare_prompt_v02_calibrated import (
    VISUAL_COMPARE_SYSTEM as V02_CALIBRATED_SYSTEM,
    VISUAL_COMPARE_USER as V02_CALIBRATED_USER,
)
from .visual_compare_prompt_v02_thinking_exposure_r023 import (
    VISUAL_COMPARE_SYSTEM as V02_THINKING_EXPOSURE_SYSTEM,
    VISUAL_COMPARE_USER as V02_THINKING_EXPOSURE_USER,
)
from .qa_chat_schema import (
    QA_CHAT_COMPARE_PROTOCOL_ID, QA_CHAT_STANDARD_ID, QA_CHAT_RESULT_SCHEMA_VERSION,
    QA_CHAT_DIMENSIONS, QA_CHAT_DIMENSION_NAMES, QAChatObservation,
)
from .visual_compare_prompt_qa_chat import (
    VISUAL_COMPARE_SYSTEM as QA_CHAT_SYSTEM,
    VISUAL_COMPARE_USER as QA_CHAT_USER,
)
from .qa_chat_v2_schema import (
    QA_CHAT_V2_COMPARE_PROTOCOL_ID, QA_CHAT_V2_RESULT_SCHEMA_VERSION, QAChatV2Observation,
)
from .visual_compare_prompt_qa_chat_v2 import (
    VISUAL_COMPARE_SYSTEM as QA_CHAT_V2_SYSTEM,
    VISUAL_COMPARE_USER as QA_CHAT_V2_USER,
)
from .timeliness_schema import (
    TIMELINESS_STANDARD_ID, TIMELINESS_COMPARE_PROTOCOL_ID, TIMELINESS_RESULT_SCHEMA_VERSION,
    TIMELINESS_DIMENSIONS, TIMELINESS_DIMENSION_NAMES, TimelinessObservation,
)
from .visual_compare_prompt_timeliness import (
    VISUAL_COMPARE_SYSTEM as TIMELINESS_SYSTEM,
    VISUAL_COMPARE_USER as TIMELINESS_USER,
)


STANDARD_ID = "qa_competitor_compare"
DEFAULT_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.2-simplified"
V02_PLUS_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.2-simplified-plus"
V03_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.3"
V02_CALIBRATED_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.2-simplified-calibrated"
V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@0.2-simplified-thinking-exposure"
V1022_COMPARE_PROTOCOL_ID = f"{STANDARD_ID}@{STANDARD_VERSION_1022}"
DIMENSIONS = (
    "understanding",
    "accuracy",
    "service_closure",
    "scenario_fulfillment",
    "intuitive_efficiency",
    "evidence_quality",
    "guided_recommendation",
)
DIMENSION_NAMES = dict(zip(DIMENSIONS, (
    "理解需求", "内容准确性", "服务闭环", "场景化满足", "直观高效", "有理有据", "引导推荐",
)))
ALL_DIMENSIONS = tuple(dict.fromkeys((*DIMENSIONS, *QA_CHAT_DIMENSIONS, *TIMELINESS_DIMENSIONS)))
DIMENSION_LABELS = {**DIMENSION_NAMES, **QA_CHAT_DIMENSION_NAMES, **TIMELINESS_DIMENSION_NAMES}

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


class _VisualCompareObservation1022Base(VisualCompareObservation):
    """Storage projection only; the independent wire contract validates first."""

    standard_version: str = STANDARD_VERSION_1022

    @model_validator(mode="after")
    def normalize_compare_states(self):
        # Unlike legacy protocols, never hide contradictory judgments by clearing
        # scores here. compare_1022_contract rejects them before this projection.
        return self


VisualCompareObservation1022 = create_model(
    "VisualCompareObservation1022",
    __base__=_VisualCompareObservation1022Base,
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
    dimensions: tuple[str, ...] = DIMENSIONS
    dimension_names: dict[str, str] = field(default_factory=lambda: dict(DIMENSION_NAMES))
    score_dimensions: tuple[str, ...] = tuple(d for d in DIMENSIONS if d != "accuracy")
    result_schema_version: str | None = None
    output_contract: str = "legacy"

    def public_metadata(self) -> dict:
        metadata = {
            "id": self.id,
            "standard_id": self.standard_id,
            "standard_version": self.standard_version,
            "bundle_revision": self.bundle_revision,
            "display": self.display,
            "status": self.status,
            "score_range": [self.score_min, self.score_max],
            "modes": ["compare"],
            "input_modalities": ["text"] if self.bundle_revision in {"0.2.0", "0.3.0"} else ["text", "text_image"],
            "conversation_adapter_version": None if self.bundle_revision in {"0.2.0", "0.3.0"} else "compare-conversation-1",
            "dimensions": [{"key": key, "label": self.dimension_names[key]} for key in self.dimensions],
            "dimension_names": dict(self.dimension_names),
            "score_dimensions": list(self.score_dimensions),
            "result_schema_version": self.result_schema_version,
        }
        if self.output_contract != "legacy":
            metadata["output_contract"] = self.output_contract
        return metadata

    def request_metadata(self) -> dict:
        """Keep legacy request fingerprints frozen when adding UI metadata.

        The conversation adapter hashed public_metadata before dimensions were
        exposed. Old bundles must keep that exact identity field set even while
        newly created manifests can expose the richer display contract.
        """
        metadata = self.public_metadata()
        if self.standard_id != STANDARD_ID or self.output_contract != "legacy":
            return metadata
        legacy_keys = (
            "id", "standard_id", "standard_version", "bundle_revision", "display",
            "status", "score_range", "modes", "input_modalities", "conversation_adapter_version",
        )
        return {key: metadata[key] for key in legacy_keys}


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
    V02_PLUS_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=V02_PLUS_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version="0.2-simplified-plus",
        bundle_revision="0.2.2",
        display="V0.2 简化版（稳定plus）·1-5分",
        status="stable",
        system_template=V02_PLUS_SYSTEM,
        user_template=V02_PLUS_USER,
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
    V02_CALIBRATED_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=V02_CALIBRATED_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version="0.2-simplified-calibrated",
        bundle_revision="0.2.3",
        display="V0.2 简化版·评分校准（实验）",
        status="experimental",
        system_template=V02_CALIBRATED_SYSTEM,
        user_template=V02_CALIBRATED_USER,
        observation_model=VisualCompareObservationV02,
        require_response_pass=True,
        score_min=1,
        score_max=5,
    ),
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version="0.2-simplified-thinking-exposure",
        bundle_revision="0.2.3",
        display="V0.2 简化版·思考暴露优化（可用）",
        status="experimental",
        system_template=V02_THINKING_EXPOSURE_SYSTEM,
        user_template=V02_THINKING_EXPOSURE_USER,
        observation_model=VisualCompareObservationV02,
        require_response_pass=True,
        score_min=1,
        score_max=5,
    ),
    QA_CHAT_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=QA_CHAT_COMPARE_PROTOCOL_ID,
        standard_id=QA_CHAT_STANDARD_ID,
        standard_version="1.0",
        bundle_revision="1.0.0",
        display="问答闲聊V1·1-5分（候选）",
        status="experimental",
        system_template=QA_CHAT_SYSTEM,
        user_template=QA_CHAT_USER,
        observation_model=QAChatObservation,
        require_response_pass=False,
        score_min=1,
        score_max=5,
        dimensions=QA_CHAT_DIMENSIONS,
        dimension_names=dict(QA_CHAT_DIMENSION_NAMES),
        score_dimensions=QA_CHAT_DIMENSIONS,
        result_schema_version=QA_CHAT_RESULT_SCHEMA_VERSION,
    ),
    QA_CHAT_V2_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=QA_CHAT_V2_COMPARE_PROTOCOL_ID,
        standard_id=QA_CHAT_STANDARD_ID,
        standard_version="1.1",
        bundle_revision="1.1.0",
        display="问答闲聊V2·1-5分（候选）",
        status="experimental",
        system_template=QA_CHAT_V2_SYSTEM,
        user_template=QA_CHAT_V2_USER,
        observation_model=QAChatV2Observation,
        require_response_pass=False,
        score_min=1,
        score_max=5,
        dimensions=QA_CHAT_DIMENSIONS,
        dimension_names=dict(QA_CHAT_DIMENSION_NAMES),
        score_dimensions=QA_CHAT_DIMENSIONS,
        result_schema_version=QA_CHAT_V2_RESULT_SCHEMA_VERSION,
    ),
    TIMELINESS_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=TIMELINESS_COMPARE_PROTOCOL_ID,
        standard_id=TIMELINESS_STANDARD_ID,
        standard_version="1.0",
        bundle_revision="1.0.2",
        display="时效性评估V1·1-3分（候选）",
        status="experimental",
        system_template=TIMELINESS_SYSTEM,
        user_template=TIMELINESS_USER,
        observation_model=TimelinessObservation,
        require_response_pass=False,
        score_min=1,
        score_max=3,
        dimensions=TIMELINESS_DIMENSIONS,
        dimension_names=dict(TIMELINESS_DIMENSION_NAMES),
        score_dimensions=TIMELINESS_DIMENSIONS,
        result_schema_version=TIMELINESS_RESULT_SCHEMA_VERSION,
    ),
    V1022_COMPARE_PROTOCOL_ID: CompareProtocol(
        id=V1022_COMPARE_PROTOCOL_ID,
        standard_id=STANDARD_ID,
        standard_version=STANDARD_VERSION_1022,
        bundle_revision="0.2.2",
        display="V0.2简化版（1022优化）",
        status="experimental",
        system_template=V1022_SYSTEM,
        user_template=V1022_USER,
        observation_model=Compare1022DualOutput,
        require_response_pass=True,
        score_min=1,
        score_max=5,
        dimensions=DIMENSIONS_1022,
        dimension_names={d: DIMENSION_NAMES[d] for d in DIMENSIONS_1022},
        score_dimensions=DIMENSIONS_1022,
        output_contract=OUTPUT_CONTRACT_1022,
    ),
}


def list_compare_protocols() -> list[CompareProtocol]:
    return list(_PROTOCOLS.values())


def resolve_compare_protocol(protocol_id: str | None, bundle_revision: str | None = None) -> CompareProtocol:
    selected = protocol_id or DEFAULT_COMPARE_PROTOCOL_ID
    try:
        protocol = _PROTOCOLS[selected]
        if bundle_revision and bundle_revision != protocol.bundle_revision:
            if selected == V1022_COMPARE_PROTOCOL_ID and bundle_revision == "0.2.1":
                from .compare_1022_contract_r021 import OUTPUT_CONTRACT_1022 as legacy_contract
                from .visual_compare_prompt_v02_1022_r021 import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol, bundle_revision=bundle_revision,
                    observation_model=VisualCompareObservation1022,
                    system_template=legacy_system, user_template=legacy_user,
                    dimensions=DIMENSIONS, dimension_names=dict(DIMENSION_NAMES),
                    output_contract=legacy_contract,
                )
            if selected == TIMELINESS_COMPARE_PROTOCOL_ID and bundle_revision == "1.0.1":
                from .timeliness_schema_r101 import TimelinessObservation as legacy_observation
                from .visual_compare_prompt_timeliness_r101 import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol,
                    bundle_revision=bundle_revision,
                    result_schema_version="timeliness-result-2",
                    observation_model=legacy_observation,
                    system_template=legacy_system,
                    user_template=legacy_user,
                )
            if selected == TIMELINESS_COMPARE_PROTOCOL_ID and bundle_revision == "1.0.0":
                # Historical observations retain their complete original JSON
                # contract and rubric; only new tasks use the compact contract.
                from .timeliness_schema_r100 import TimelinessObservation as legacy_observation
                from .visual_compare_prompt_timeliness_r100 import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol,
                    bundle_revision=bundle_revision,
                    result_schema_version="timeliness-result-1",
                    observation_model=legacy_observation,
                    system_template=legacy_system,
                    user_template=legacy_user,
                )
            if selected == V02_PLUS_COMPARE_PROTOCOL_ID and bundle_revision == "0.2.1":
                # Preserve the frozen rubric for historical stable-plus tasks.
                from .visual_compare_prompt_v02_simplified_plus_r021 import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol,
                    bundle_revision=bundle_revision,
                    system_template=legacy_system,
                    user_template=legacy_user,
                )
            if selected == V02_CALIBRATED_COMPARE_PROTOCOL_ID and bundle_revision == "0.2.2":
                # Keep pre-fusion calibrated tasks bound to their original templates.
                from .visual_compare_prompt_v02_calibrated_r022 import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol,
                    bundle_revision=bundle_revision,
                    system_template=legacy_system,
                    user_template=legacy_user,
                )
            if selected == V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID and bundle_revision == "0.2.4":
                # 0.2.4 is frozen for historical tasks only; new/UI tasks use 0.2.3.
                from .visual_compare_prompt_v02_thinking_exposure import (
                    VISUAL_COMPARE_SYSTEM as legacy_system,
                    VISUAL_COMPARE_USER as legacy_user,
                )
                return replace(
                    protocol,
                    bundle_revision=bundle_revision,
                    system_template=legacy_system,
                    user_template=legacy_user,
                )
            legacy = {
                DEFAULT_COMPARE_PROTOCOL_ID: "0.2.0",
                V03_COMPARE_PROTOCOL_ID: "0.3.0",
            }.get(selected)
            if bundle_revision != legacy:
                raise ValueError("无法恢复任务冻结的实现版本，请新建任务")
            return replace(protocol, bundle_revision=legacy)
        return protocol
    except KeyError as exc:
        supported = ", ".join(_PROTOCOLS)
        raise ValueError(f"未知评测协议 {selected!r}；可选值：{supported}") from exc


def resolve_compare_standard(
    standard_id: str | None,
    standard_version: str | None,
    bundle_revision: str | None = None,
) -> CompareProtocol:
    """Resolve an explicit family/version, retaining bare legacy versions.

    Missing chat revisions must be rejected by snapshot recovery, where it is
    possible to distinguish a historical task from a newly selected protocol.
    """
    family = standard_id or STANDARD_ID
    if standard_version and "@" in standard_version:
        selected_family, version = standard_version.split("@", 1)
        if standard_id and selected_family != standard_id:
            raise ValueError("评测标准家族与完整协议ID冲突")
        family, standard_version = selected_family, version
    if "@" in family:
        selected_family, version = family.split("@", 1)
        if standard_version and standard_version != version:
            raise ValueError("评测标准版本与完整协议ID冲突")
        family, standard_version = selected_family, version
    if not standard_version:
        if family != STANDARD_ID:
            raise ValueError("评测标准缺少版本")
        return resolve_compare_protocol(None, bundle_revision)
    return resolve_compare_protocol(f"{family}@{standard_version}", bundle_revision)
