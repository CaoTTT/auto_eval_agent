"""可按任务冻结 V0.2 简化版或 V0.3 的垂域视觉对比裁判。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ..config import VisualModeProfile
from ..compare_input import normalize_product_contexts
from ..query_images import encode_query_image, check_request_budget, input_manifest
from ..preparation import run_preparation, check_preparation
from ..media import encode_frame
from ..long_screenshot import (
    RISKY_REVIEW_REASON, check_context_budget, encode_original_image,
)
from ..paths import resolve_project_path
from ..schema import VisualCompareObservation
from .base import JudgeClient, JudgeOutputEmptyError, JudgeOutputParseError
from .prompts import parse_json_loose, parse_json_preserving_pairs, scan_recoverable_json_object
from .query_image_prompt import QUERY_IMAGE_INSTRUCTIONS
from .qa_chat_schema import QA_CHAT_STANDARD_ID, QAChatObservation, finalize_qa_chat_observation
from .chitchat_flat_schema import (
    CHITCHAT_FLAT_RESULT_SCHEMA_VERSION, ChitchatFlatObservation,
    expected_output_keys as chitchat_flat_output_keys,
    parse_output as parse_chitchat_flat_output,
    finalize_chitchat_flat_observation,
)
from .qa_chat_v2_schema import (
    QA_CHAT_V2_RESULT_SCHEMA_VERSION, QAChatV2Observation,
    collection_metadata, expected_output_keys, finalize_qa_chat_v2_observation,
)
from .knowledge_qa_schema import (
    KNOWLEDGE_QA_RESULT_SCHEMA_VERSION, KnowledgeQAObservation,
    expected_output_keys as knowledge_qa_output_keys,
    parse_output as parse_knowledge_qa_output,
    finalize_knowledge_qa_observation,
)
from .timeliness_schema import (
    TIMELINESS_RESULT_SCHEMA_VERSION, TimelinessObservation,
    expected_output_keys as timeliness_output_keys, allowed_output_keys as timeliness_allowed_keys,
    finalize_timeliness_observation,
)
from .timeliness_schema_r100 import (
    TIMELINESS_RESULT_SCHEMA_VERSION as TIMELINESS_V1_RESULT_SCHEMA_VERSION,
    TimelinessObservation as TimelinessV1Observation,
    expected_output_keys as timeliness_v1_output_keys,
    finalize_timeliness_observation as finalize_timeliness_v1_observation,
)
from .timeliness_schema_r101 import (
    TIMELINESS_RESULT_SCHEMA_VERSION as TIMELINESS_V2_RESULT_SCHEMA_VERSION,
    TimelinessObservation as TimelinessV2Observation,
    expected_output_keys as timeliness_v2_output_keys,
    allowed_output_keys as timeliness_v2_allowed_keys,
    finalize_timeliness_observation as finalize_timeliness_v2_observation,
)
from .compare_1022_contract import (
    OUTPUT_CONTRACT_1022, OutputContractError, parse_1022_output,
    response_format_1022, validate_1022_output,
)
from .compare_1022_contract_r021 import (
    OUTPUT_CONTRACT_1022 as OUTPUT_CONTRACT_1022_R021,
    parse_1022_output as parse_1022_output_r021,
    response_format_1022 as response_format_1022_r021,
)
from .compare_1022_contract_r022 import (
    parse_1022_output as parse_1022_output_r022,
    response_format_1022 as response_format_1022_r022,
    validate_1022_output as validate_1022_output_r022,
)
from .compare_protocols import (
    CompareProtocol,
    DIMENSIONS,
    resolve_compare_protocol,
)


# 按当前实验约定：准确性保留模型结果，但暂不参与任何汇总或总排名。
AGGREGATION_DIMENSIONS = tuple(d for d in DIMENSIONS if d != "accuracy")
_CONTEXT_NOT_PROVIDED = object()


def _product_nos(observation: VisualCompareObservation) -> tuple[int, ...]:
    return (1, 2, 3) if observation.product_count == 3 else (1, 2)


def _screenshot_prompt_manifest(meta: dict) -> str:
    """模型只需要空间关系和边界说明；文件名、哈希、耗时留在 trace。"""
    return json.dumps({
        "split_status": meta["split_status"], "split_count": meta["split_count"],
        "original_width": meta["original_width"], "original_height": meta["original_height"],
        "boundaries": meta["boundaries"],
        "slices": [{key: part[key] for key in ("start_y", "end_y", "width", "height")} for part in meta["slices"]],
    }, ensure_ascii=False)


def _score_winner(score1: int | None, score2: int | None) -> str | None:
    """仅用于旧页面的产品1/产品2兼容投影。"""
    if score1 is None or score2 is None:
        return None
    if score1 == score2:
        return "tie"
    return "answer1" if score1 > score2 else "answer2"


def _gate_winner(status1: str | None, status2: str | None) -> str | None:
    if status1 is None or status2 is None or "unclear" in (status1, status2):
        return None
    if status1 == status2:
        return "tie"
    return "answer1" if status1 == "pass" else "answer2"


def _rank_groups(
    observation: VisualCompareObservation,
    dimension: str,
) -> list[list[str]]:
    """根据非空绝对分生成并列排名；null 产品自然排除。"""
    scored: dict[int, list[str]] = {}
    for answer_no in _product_nos(observation):
        score = getattr(observation, f"answer{answer_no}_{dimension}_score")
        if score is not None:
            scored.setdefault(score, []).append(f"product{answer_no}")
    return [scored[score] for score in sorted(scored, reverse=True)]


def _normalize_observation_state(
    observation: VisualCompareObservation,
    protocol: CompareProtocol,
) -> None:
    """消除 N/A、无法核验、输入失败和 Gate 状态与分数的矛盾。"""
    product_nos = _product_nos(observation)
    for answer_no in product_nos:
        if getattr(observation, f"answer{answer_no}_input_status") == "failed":
            setattr(observation, f"answer{answer_no}_response_gate", "unclear")
            setattr(observation, f"answer{answer_no}_safety_gate", "unclear")
    for dimension in DIMENSIONS:
        applicable = getattr(observation, f"{dimension}_applicable")
        unverifiable = (
            getattr(observation, f"{dimension}_verification_status") == "unverifiable"
        )
        for answer_no in (1, 2, 3):
            if answer_no not in product_nos:
                setattr(observation, f"answer{answer_no}_{dimension}_score", None)
                continue
            input_failed = getattr(observation, f"answer{answer_no}_input_status") == "failed"
            response_gate = getattr(observation, f"answer{answer_no}_response_gate")
            response_blocked = (
                response_gate != "pass"
                if protocol.require_response_pass
                else response_gate == "fail"
            )
            safety_failed = getattr(observation, f"answer{answer_no}_safety_gate") == "fail"
            if (
                not applicable
                or unverifiable
                or input_failed
                or response_blocked
                or (not protocol.require_response_pass and safety_failed)
            ):
                setattr(observation, f"answer{answer_no}_{dimension}_score", None)


def _score_is_required(
    observation: VisualCompareObservation,
    answer_no: int,
    dimension: str,
    protocol: CompareProtocol,
) -> bool:
    if not getattr(observation, f"{dimension}_applicable"):
        return False
    if getattr(observation, f"{dimension}_verification_status") == "unverifiable":
        return False
    if getattr(observation, f"answer{answer_no}_input_status") == "failed":
        return False
    response_gate = getattr(observation, f"answer{answer_no}_response_gate")
    if protocol.require_response_pass:
        return response_gate == "pass"
    return response_gate != "fail" and (
        getattr(observation, f"answer{answer_no}_safety_gate") != "fail"
    )


def _finalize_observation(
    observation: VisualCompareObservation,
    protocol: CompareProtocol,
) -> None:
    """只确定性计算派生字段，不让模型自行创造总分权重。"""
    strict_1022 = protocol.output_contract == OUTPUT_CONTRACT_1022_R021
    if not strict_1022:
        _normalize_observation_state(observation, protocol)
    observation.standard_id = protocol.standard_id
    observation.standard_version = protocol.standard_version

    for dimension in DIMENSIONS:
        setattr(observation, f"{dimension}_rank_groups", _rank_groups(observation, dimension))
        setattr(
            observation,
            f"{dimension}_winner",
            _score_winner(
                getattr(observation, f"answer1_{dimension}_score"),
                getattr(observation, f"answer2_{dimension}_score"),
            ) if getattr(observation, f"{dimension}_applicable") else None,
        )

    # 当前两个协议都尚无正式权重；禁止擅自计算总分和整体第一名。
    observation.answer1_total_score = None
    observation.answer2_total_score = None
    observation.answer3_total_score = None
    observation.overall_winner = None
    observation.overall_ranking = None

    # 旧字段只保留产品1/产品2投影，保证旧页面和历史接口不报错。
    observation.relevance = observation.understanding_winner
    observation.relevance_reason = observation.understanding_reason
    observation.safety = _gate_winner(
        observation.answer1_safety_gate,
        observation.answer2_safety_gate,
    )
    observation.safety_reason = "；".join(filter(None, [
        observation.answer1_safety_gate_reason,
        observation.answer2_safety_gate_reason,
    ]))
    observation.content_quality = None
    observation.content_quality_reason = (
        f"{protocol.standard_version}未定义正式维度权重，不生成综合内容质量胜方"
    )
    observation.need_closure = observation.service_closure_winner
    observation.need_closure_reason = observation.service_closure_reason
    observation.personalization = observation.scenario_fulfillment_winner
    observation.personalization_reason = observation.scenario_fulfillment_reason

    if strict_1022:
        # Wire validation has already checked mandatory review states. Keep the
        # supplied judgments intact; legacy missing-field defaults/cleanup must
        # not turn malformed 1022 output into a successful evaluation.
        observation.needs_review = observation.needs_human_review
        observation.review_reason = "；".join(observation.review_reasons)
        return

    missing: list[str] = []
    for answer_no in _product_nos(observation):
        input_status = getattr(observation, f"answer{answer_no}_input_status")
        if input_status == "failed":
            missing.append(f"产品{answer_no}输入失败，本条结果不可比较")
        elif input_status == "partial":
            missing.append(f"产品{answer_no}输入不完整")
        for gate in ("response", "safety"):
            if getattr(observation, f"answer{answer_no}_{gate}_gate") == "unclear":
                missing.append(f"产品{answer_no} {gate}_gate 无法判断")

    for dimension in DIMENSIONS:
        # accuracy 当前只保留，不因无法核验单独触发复核或进入聚合。
        if dimension != "accuracy" and (
            getattr(observation, f"{dimension}_verification_status") in {"partial", "unverifiable"}
        ):
            missing.append(f"{dimension} 证据未完全核验")
        missing_answers = [
            str(answer_no)
            for answer_no in _product_nos(observation)
            if _score_is_required(observation, answer_no, dimension, protocol)
            and getattr(observation, f"answer{answer_no}_{dimension}_score") is None
        ]
        if missing_answers:
            missing.append(f"{dimension} 缺少绝对分（产品{'/'.join(missing_answers)}）")

    merged_evidence: list[str] = []
    for dimension in DIMENSIONS:
        merged_evidence.extend(getattr(observation, f"{dimension}_evidence"))
    observation.evidence = list(dict.fromkeys([*observation.evidence, *merged_evidence]))
    observation.review_reasons = list(dict.fromkeys([*observation.review_reasons, *missing]))
    observation.needs_human_review = bool(
        observation.needs_human_review or observation.review_reasons
    )
    observation.needs_review = observation.needs_human_review
    observation.review_reason = "；".join(observation.review_reasons)


def _compact_1022_result(observation: BaseModel | dict, protocol: CompareProtocol) -> dict:
    """Project the compact contract without resurrecting legacy model fields."""
    data = observation.model_dump() if isinstance(observation, BaseModel) else dict(observation)
    count = 3 if "answer3_input_status" in data else 2
    validate = validate_1022_output_r022 if protocol.bundle_revision == "0.2.2" else validate_1022_output
    result = validate(data, count)
    result.update(
        standard_id=protocol.standard_id, standard_version=protocol.standard_version,
        product_count=count, evaluation_profile=protocol.id,
        bundle_revision=protocol.bundle_revision, output_contract=protocol.output_contract,
        has_conflict=result["content_conflict"],
        overall_winner=None, overall_ranking=None,
    )
    for dimension in protocol.dimensions:
        scores = {n: result[f"answer{n}_{dimension}_score"] for n in range(1, count + 1)}
        groups: dict[int, list[str]] = {}
        for n, score in scores.items():
            if score is not None:
                groups.setdefault(score, []).append(f"product{n}")
        result[f"{dimension}_rank_groups"] = [groups[s] for s in sorted(groups, reverse=True)]
        result[f"{dimension}_winner"] = _score_winner(scores[1], scores[2])
    for n in range(1, count + 1):
        result[f"answer{n}_total_score"] = None
    return result


def visual_compare_result_fields(
    observation: BaseModel | dict,
    protocol: CompareProtocol | str | None = None,
    *,
    trusted_collection_metadata: dict | None = None,
) -> dict[str, Any]:
    """返回扁平结果；新增字段可直接被 Excel 导出，旧字段继续存在。"""
    selected_protocol = (
        resolve_compare_protocol(protocol) if isinstance(protocol, str)
        else protocol or resolve_compare_protocol(None)
    )
    if selected_protocol.output_contract == OUTPUT_CONTRACT_1022:
        return _compact_1022_result(observation, selected_protocol)
    if selected_protocol.result_schema_version == CHITCHAT_FLAT_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, ChitchatFlatObservation):
            raise ValueError("闲聊七维协议需要独立ChitchatFlatObservation结果模型")
        return finalize_chitchat_flat_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.result_schema_version == KNOWLEDGE_QA_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, KnowledgeQAObservation):
            raise ValueError("知识问答协议需要独立KnowledgeQAObservation结果模型")
        return finalize_knowledge_qa_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.result_schema_version == TIMELINESS_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, TimelinessObservation):
            raise ValueError("时效性协议需要独立TimelinessObservation结果模型")
        return finalize_timeliness_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.result_schema_version == TIMELINESS_V2_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, TimelinessV2Observation):
            raise ValueError("旧时效性协议需要冻结的TimelinessV2Observation结果模型")
        return finalize_timeliness_v2_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.result_schema_version == TIMELINESS_V1_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, TimelinessV1Observation):
            raise ValueError("旧时效性协议需要冻结的TimelinessV1Observation结果模型")
        return finalize_timeliness_v1_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.result_schema_version == QA_CHAT_V2_RESULT_SCHEMA_VERSION:
        if not isinstance(observation, QAChatV2Observation):
            raise ValueError("问答闲聊V2需要独立QAChatV2Observation结果模型")
        return finalize_qa_chat_v2_observation(
            observation, selected_protocol, trusted_collection_metadata=trusted_collection_metadata)
    if selected_protocol.standard_id == QA_CHAT_STANDARD_ID:
        if not isinstance(observation, QAChatObservation):
            raise ValueError("问答闲聊协议需要独立QAChatObservation结果模型")
        finalize_qa_chat_observation(observation, selected_protocol)
    else:
        _finalize_observation(observation, selected_protocol)
    result = observation.model_dump()
    result["evaluation_profile"] = selected_protocol.id
    result["bundle_revision"] = selected_protocol.bundle_revision
    if selected_protocol.output_contract == OUTPUT_CONTRACT_1022_R021:
        result["output_contract"] = selected_protocol.output_contract
        result["content_conflict"] = observation.has_conflict
    result["needs_review_label"] = "T" if observation.needs_review else "F"
    return result


class VisualCompareJudge:
    """同一调用兼容2个产品，并通过可选参数增量支持第3个产品。"""

    def __init__(
        self,
        client: JudgeClient,
        profile: VisualModeProfile,
        protocol: CompareProtocol | str | None = None,
    ):
        self.client = client
        self.profile = profile
        self.protocol = (
            resolve_compare_protocol(protocol) if isinstance(protocol, str)
            else protocol or resolve_compare_protocol(None)
        )

    async def evaluate(
        self,
        *,
        question: str,
        context: str = "",  # Legacy product-1 alias for 1022; never shared with other products.
        context1: str | object = _CONTEXT_NOT_PROVIDED,
        answer1: str = "",
        frames1: list[str] | None = None,
        context2: str = "",
        answer2: str = "",
        frames2: list[str] | None = None,
        context3: str = "",
        answer3: str = "",
        frames3: list[str] | None = None,
        product_count: Literal[2, 3] | None = None,
        evidence_mode: str = "video_frames",
        include_prior_knowledge: bool = False,
        screenshot_metas: list[dict] | None = None,
        query_image_meta: list[dict] | None = None,
        conversation_input: dict | None = None,
        evidence_callback=None,
        evaluation_time: datetime | None = None,
        stream_callback=None,
        trusted_collection_metadata: dict | None = None,
    ) -> dict[str, Any]:
        extraction = self.profile.extraction
        now = evaluation_time or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        evaluation_datetime = now.isoformat(timespec="seconds")
        actual_product_count: Literal[2, 3] = product_count or (
            3 if (context3 or answer3 or frames3 is not None) else 2
        )
        is_chat_v2 = self.protocol.result_schema_version == QA_CHAT_V2_RESULT_SCHEMA_VERSION
        is_timeliness = self.protocol.standard_id == "timeliness_compare"
        is_knowledge_qa = self.protocol.result_schema_version == KNOWLEDGE_QA_RESULT_SCHEMA_VERSION
        is_flat_chat = self.protocol.result_schema_version == CHITCHAT_FLAT_RESULT_SCHEMA_VERSION
        is_strict_output = is_chat_v2 or is_timeliness or is_knowledge_qa or is_flat_chat
        timeliness_contracts = {
            TIMELINESS_RESULT_SCHEMA_VERSION: (timeliness_output_keys, timeliness_allowed_keys),
            TIMELINESS_V2_RESULT_SCHEMA_VERSION: (timeliness_v2_output_keys, timeliness_v2_allowed_keys),
            TIMELINESS_V1_RESULT_SCHEMA_VERSION: (timeliness_v1_output_keys, timeliness_v1_output_keys),
            KNOWLEDGE_QA_RESULT_SCHEMA_VERSION: (knowledge_qa_output_keys, knowledge_qa_output_keys),
            CHITCHAT_FLAT_RESULT_SCHEMA_VERSION: (chitchat_flat_output_keys, chitchat_flat_output_keys),
        }
        strict_output_keys, allowed_output_keys = timeliness_contracts.get(
            self.protocol.result_schema_version, (expected_output_keys, expected_output_keys))
        output_label = "闲聊七维" if is_flat_chat else "知识问答V1" if is_knowledge_qa else "时效性评估V1" if is_timeliness else "问答闲聊V2"
        trusted_collection = collection_metadata(trusted_collection_metadata, actual_product_count) if is_strict_output else None
        compact_1022 = self.protocol.output_contract == OUTPUT_CONTRACT_1022
        product_contexts_only = compact_1022 and self.protocol.bundle_revision in {"0.2.4", "0.2.5", "0.2.6"}
        if product_contexts_only:
            supplied_contexts = {"context": context, "context2": context2, "context3": context3}
            if context1 is not _CONTEXT_NOT_PROVIDED:
                supplied_contexts["context1"] = context1
            contexts = normalize_product_contexts(supplied_contexts, actual_product_count)
            context1, context2 = contexts["context1"], contexts["context2"]
            context3 = contexts.get("context3", "")
        elif context1 is _CONTEXT_NOT_PROVIDED:
            context1 = ""
        strict_1022 = self.protocol.output_contract in {OUTPUT_CONTRACT_1022, OUTPUT_CONTRACT_1022_R021}
        compact_r022 = compact_1022 and self.protocol.bundle_revision == "0.2.2"
        parse_compact = parse_1022_output_r022 if compact_r022 else parse_1022_output
        format_compact = response_format_1022_r022 if compact_r022 else response_format_1022
        output_options = {}
        if strict_1022:
            output_options = {
                "require_complete": True,
                "response_format": (format_compact if compact_1022 else response_format_1022_r021)(
                    getattr(self.client.cfg, "compare_structured_output", "off"),
                    actual_product_count,
                ),
            }
        elif is_knowledge_qa or is_flat_chat:
            output_options = {"require_complete": True}
        if evidence_mode not in ("video_frames", "long_screenshot"):
            raise ValueError("未知视觉证据模式")
        if type(include_prior_knowledge) is not bool:
            raise ValueError("include_prior_knowledge 必须是布尔值")
        is_screenshot = evidence_mode == "long_screenshot"
        metas = screenshot_metas or []
        if is_screenshot and len(metas) != actual_product_count:
            raise ValueError("长截图缺少逐产品预处理元数据")

        system_template, user_template = self.protocol.prompt_templates(actual_product_count)
        system = system_template.render(
            persona=self.client.persona,
            product_count=actual_product_count,
            evidence_mode=evidence_mode,
            include_prior_knowledge=include_prior_knowledge,
        )
        query_metas = query_image_meta or []
        if len(query_metas) > 1 and conversation_input is None:
            raise ValueError("首版每题仅支持一张提问图片")
        if query_metas and conversation_input is None:
            if self.protocol.bundle_revision in {"0.2.0", "0.3.0"}:
                raise ValueError("旧实现版本不支持提问图片，请新建任务")
            system += QUERY_IMAGE_INSTRUCTIONS
        user = user_template.render(
            **({} if product_contexts_only else {
                "evaluation_datetime": evaluation_datetime,
            }),
            question=question,
            product_count=actual_product_count,
            context1=context1,
            answer1=answer1,
            context2=context2,
            answer2=answer2,
            context3=context3,
            answer3=answer3,
            frame_count1=len(frames1) if frames1 else 0,
            frame_count2=len(frames2) if frames2 else 0,
            frame_count3=len(frames3) if frames3 else 0,
            evidence_mode=evidence_mode,
            image_count1=len(frames1 or []),
            image_count2=len(frames2 or []),
            image_count3=len(frames3 or []),
            split_manifest1=_screenshot_prompt_manifest(metas[0]) if metas else "",
            split_manifest2=_screenshot_prompt_manifest(metas[1]) if metas else "",
            split_manifest3=_screenshot_prompt_manifest(metas[2]) if len(metas) > 2 else "",
        )

        def prepare_images():
            user_images: list[str] = []
            user_image_refs: list[str] = []
            frame_groups = (frames1, frames2, frames3) if actual_product_count == 3 else (frames1, frames2)
            content_parts = [{"type": "text", "text": user}]
            image_metadata = []
            for meta in query_metas:
                content_parts.extend([
                    {"type": "text", "text": f"提问图片 {meta['query_image_id']} 开始：共享用户题目，不是产品回答"},
                    {"type": "image_url", "image_url": {"url": encode_query_image(meta, self.profile.query_images)}},
                    {"type": "text", "text": f"提问图片 {meta['query_image_id']} 结束"},
                ])
                image_metadata.append({**meta, "ref_path": meta["path"]})
                user_image_refs.append(meta["path"])
            for product_no, frames in enumerate(frame_groups, 1):
                check_preparation()
                if not frames:
                    if is_screenshot:
                        raise ValueError(f"产品{product_no}缺少长截图证据")
                    continue
                if is_screenshot:
                    meta = metas[product_no - 1]
                    if frames != [part["path"] for part in meta["slices"]]:
                        raise ValueError("长截图图片顺序与预处理元数据不一致")
                    count = len(frames)
                    intro = f"产品{product_no}最终回答长截图开始，共{count}块；从上到下连续、无重叠。切片边界不是产品缺陷。"
                    content_parts.append({"type": "text", "text": intro})
                    for part_no, path in enumerate(frames, 1):
                        position = "whole" if count == 1 else ("top" if part_no == 1 else "bottom" if part_no == count else "middle")
                        label = f"产品{product_no} 第{part_no}/{count}块（{position}），切片状态：{meta['split_status']}"
                        content_parts.extend([
                            {"type": "text", "text": label},
                            {"type": "image_url", "image_url": {"url": encode_original_image(resolve_project_path(path), self.profile.long_screenshot, meta["slices"][part_no - 1].get("sha256"))}},
                            {"type": "text", "text": f"产品{product_no} 第{part_no}/{count}块结束"},
                        ])
                        image_metadata.append({
                            **meta["slices"][part_no - 1], "image_role": "product_answer",
                            "product_no": product_no, "part_no": part_no, "part_count": count,
                            "position": position, "split_status": meta["split_status"], "ref_path": path,
                            **({"preprocessing": meta} if part_no == 1 else {}),
                        })
                    content_parts.append({"type": "text", "text": f"产品{product_no}长截图结束"})
                    user_image_refs.extend(frames)
                    continue
                encoded_frames = list(
                    encode_frame(
                        Path(path),
                        max_edge=extraction.max_edge,
                        quality=extraction.jpeg_quality,
                    )
                    for path in frames
                )
                if query_metas or is_timeliness or is_knowledge_qa:
                    for frame_no, (path, encoded) in enumerate(zip(frames, encoded_frames), 1):
                        content_parts.extend([
                            {"type": "text", "text": f"产品{product_no}回答录屏第{frame_no}帧开始"},
                            {"type": "image_url", "image_url": {"url": encoded}},
                            {"type": "text", "text": f"产品{product_no}回答录屏第{frame_no}帧结束"},
                        ])
                        image_metadata.append({"image_role": "product_answer", "product_no": product_no,
                                               "frame_no": frame_no, "ref_path": path})
                else:
                    user_images.extend(encoded_frames)
                    image_metadata.extend({"image_role": "product_answer", "product_no": product_no,
                                           "frame_no": frame_no, "ref_path": path}
                                          for frame_no, path in enumerate(frames, 1))
                user_image_refs.extend(frames)

            return user_images, user_image_refs, content_parts, image_metadata

        conversation_manifest = None
        if conversation_input is not None:
            from .conversation_prompt import assemble_conversation
            system, content_parts, image_metadata, user_image_refs, conversation_manifest = await run_preparation(
                assemble_conversation, system, conversation_input, self.protocol, evaluation_datetime=evaluation_datetime, timeout=60)
            user_images = []
            user = content_parts[0]["text"]
        else:
            user_images, user_image_refs, content_parts, image_metadata = (
                await run_preparation(prepare_images, timeout=60)
            )

        started = time.perf_counter()
        v2_input_manifest = None
        if is_strict_output:
            # Collection conclusions influence final scores, so they must be
            # bound to the request even when media bytes are identical. Frozen
            # V1 request and conversation fingerprints remain untouched.
            binding = {"trusted_collection_metadata": trusted_collection}
            if conversation_manifest:
                binding["conversation_input_manifest_sha256"] = conversation_manifest
            else:
                actual_parts = content_parts if is_screenshot or query_metas or is_timeliness or is_knowledge_qa else [
                    {"type": "text", "text": user},
                    *({"type": "image_url", "image_url": {"url": url}} for url in user_images),
                ]
                binding.update(protocol=self.protocol.request_metadata(), system=system, content=actual_parts)
            v2_input_manifest = hashlib.sha256(json.dumps(
                binding, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        audit = None
        if is_screenshot or evidence_callback:
            from ..evidence_audit import capture_request
            preprocessing = []
            if is_screenshot:
                preprocessing = ([{"product_no": n, "source_turn": turn["turn_index"],
                                   "metadata": turn[f"screenshot_meta{n}"]}
                                  for turn in conversation_input["turns"]
                                  for n in range(1, actual_product_count + 1)] if conversation_input else
                                 [{"product_no": n, "metadata": meta} for n, meta in enumerate(metas, 1)])
            request_parts = content_parts if is_screenshot or query_metas or is_timeliness or is_knowledge_qa else [
                {"type": "text", "text": user},
                *({"type": "image_url", "image_url": {"url": url}} for url in user_images),
            ]
            audit = await run_preparation(capture_request, system, request_parts, image_metadata,
                                          preprocessing, self.protocol.public_metadata(), timeout=60)
            audit["judge_model"] = getattr(self.client, "model", None)
            audit["evidence_mode"] = evidence_mode
            if is_strict_output:
                audit["trusted_collection_metadata"] = trusted_collection
                audit["input_manifest_sha256"] = v2_input_manifest
            if evidence_callback:
                evidence_callback(audit)
        request_views = []
        if query_metas and conversation_input is None:
            request_views = await run_preparation(check_request_budget, system, content_parts, self.profile.query_images, timeout=60)
            for metadata, view in zip(image_metadata, request_views, strict=True):
                metadata.update(view)
                metadata.setdefault("image_role", "product_answer")
        if is_screenshot and conversation_input is None:
            check_context_budget(system, [p["text"] for p in content_parts if p["type"] == "text"], metas, self.profile.long_screenshot)
        if audit is not None:
            audit["record_status"] = "model_call_started"
            if evidence_callback:
                evidence_callback(audit)
        if is_screenshot or query_metas or is_timeliness or is_knowledge_qa:
            raw_output = await self.client.complete(
                system, user, stream_callback=stream_callback,
                content_parts=content_parts, image_metadata=image_metadata,
                user_image_refs=user_image_refs,
                **output_options,
            )
        else:
            raw_output = await self.client.complete(
                system, user, stream_callback=stream_callback,
                user_images=user_images or None, user_image_refs=user_image_refs or None,
                **output_options,
            )

        if audit is not None:
            audit["record_status"] = "model_response_received"
            if evidence_callback:
                evidence_callback(audit)
        repaired = ""
        local_format_recovery_steps: list[str] = []
        if is_knowledge_qa or is_flat_chat:
            # These rubrics have exactly one semantic response. Their
            # independent parser rejects duplicate keys and invalid numbers;
            # no repair request can invent a missing reason, status or score.
            parsed_pairs = None
            original_parsed_pairs = None
            repair_parsed_pairs = None
            try:
                parsed_pairs = parse_json_preserving_pairs(raw_output) if isinstance(raw_output, str) else None
                original_parsed_pairs = parsed_pairs
                parse_independent = parse_chitchat_flat_output if is_flat_chat else parse_knowledge_qa_output
                data = parse_independent(raw_output, actual_product_count).model_dump()
            except (ValueError, ValidationError) as exc:
                category = "empty_response" if not isinstance(raw_output, str) or not raw_output.strip() else "schema"
                error_cls = JudgeOutputEmptyError if category == "empty_response" else JudgeOutputParseError
                error = error_cls(
                    f"{output_label}输出不符合严格JSON合同：{exc}",
                    raw_output=raw_output or "", repair_output="", judge=self.client.cfg.name,
                    model=self.client.model, error_category=category,
                )
                error.raw_model_output = raw_output or ""
                error.raw_key_pairs = parsed_pairs["raw_key_pairs"] if parsed_pairs else []
                error.original_raw_key_pairs = error.raw_key_pairs
                error.locally_repaired_key_pairs = []
                error.repair_key_pairs = []
                error.repair_locally_repaired_key_pairs = []
                error.duplicate_keys = parsed_pairs["duplicate_keys"] if parsed_pairs else []
                error.structural_diagnostics = [str(exc)]
                error.trusted_collection_metadata = trusted_collection
                error.input_manifest_sha256 = v2_input_manifest
                raise error from exc
        elif strict_1022:
            if not isinstance(raw_output, str) or not raw_output.strip():
                error = JudgeOutputEmptyError(
                    "裁判返回空白响应，未生成最终JSON；不能通过格式修复补写评测结果",
                    raw_output=raw_output or "", repair_output="", judge=self.client.cfg.name,
                    model=self.client.model, error_category="empty_response",
                )
                error.structural_diagnostics = [str(error)]
                raise error
            try:
                if compact_1022 and not compact_r022:
                    data = parse_1022_output(raw_output, actual_product_count,
                                             recovery_steps=local_format_recovery_steps)
                else:
                    data = (parse_compact if compact_1022 else parse_1022_output_r021)(raw_output, actual_product_count)
            except OutputContractError as exc:
                error = JudgeOutputParseError(
                    str(exc), raw_output=raw_output, repair_output="",
                    judge=self.client.cfg.name, model=self.client.model,
                    error_category=exc.category,
                )
                error.structural_diagnostics = [str(exc)]
                error.local_format_recovery_steps = local_format_recovery_steps
                raise error from exc
            if not compact_1022:
                data["standard_id"] = self.protocol.standard_id
                data["standard_version"] = self.protocol.standard_version
        else:
            parsed_pairs = parse_json_preserving_pairs(raw_output) if is_strict_output else None
            original_parsed_pairs = parsed_pairs
            repair_parsed_pairs = None
            if is_strict_output:
                data = parsed_pairs["data"] if parsed_pairs else None
            else:
                data = parse_json_loose(raw_output)
            repaired = ""
            repair_original_data = None

            def duplicate_diagnostics(parsed):
                # Inspect names in the retained top-level pairs; do not collapse a
                # duplicate-key object into a dict even to run field diagnostics.
                diagnostic_pairs = parsed["raw_key_pairs"] or parsed["locally_repaired_key_pairs"]
                keys = {pair[0] for pair in diagnostic_pairs}
                expected = set(strict_output_keys(actual_product_count))
                allowed = set(allowed_output_keys(actual_product_count))
                diagnostics = list(parsed["structural_diagnostics"])
                if expected - keys:
                    diagnostics.append(f"缺少字段：{sorted(expected - keys)}")
                if keys - allowed:
                    diagnostics.append(f"多余字段：{sorted(keys - allowed)}")
                return diagnostics

            def v2_parse_error(message, diagnostics):
                error = JudgeOutputParseError(
                    message, raw_output=raw_output, repair_output=repaired,
                    judge=self.client.cfg.name, model=self.client.model)
                error.raw_model_output = raw_output
                error.raw_key_pairs = original_parsed_pairs["raw_key_pairs"] if original_parsed_pairs else []
                error.original_raw_key_pairs = error.raw_key_pairs
                error.locally_repaired_key_pairs = original_parsed_pairs["locally_repaired_key_pairs"] if original_parsed_pairs else []
                error.repair_key_pairs = repair_parsed_pairs["raw_key_pairs"] if repair_parsed_pairs else []
                error.repair_locally_repaired_key_pairs = repair_parsed_pairs["locally_repaired_key_pairs"] if repair_parsed_pairs else []
                error.structural_diagnostics = diagnostics
                error.duplicate_keys = parsed_pairs["duplicate_keys"] if parsed_pairs else []
                error.trusted_collection_metadata = trusted_collection
                error.input_manifest_sha256 = v2_input_manifest
                return error

            if is_strict_output and parsed_pairs and parsed_pairs["top_level_type"] != "object":
                raise v2_parse_error(f"{output_label}输出必须为JSON对象", parsed_pairs["structural_diagnostics"])
            if is_strict_output and parsed_pairs and parsed_pairs["duplicate_keys"]:
                raise v2_parse_error(f"{output_label}输出存在重复键", duplicate_diagnostics(parsed_pairs))
            if data is None:
                if is_strict_output:
                    recovered = scan_recoverable_json_object(raw_output)
                    original_parsed_pairs = {**recovered, "locally_repaired_key_pairs": [], "duplicate_keys": []}
                    if recovered["structural_diagnostics"]:
                        raise v2_parse_error(f"{output_label}原始字段不可安全恢复，禁止语义补判", recovered["structural_diagnostics"])
                    # All original fields, values, types and state relations must
                    # already be determined. A syntax repair cannot supply a gate,
                    # score, reason or applicability that the model never provided.
                    try:
                        self.protocol.observation_model.model_validate(
                            recovered["data"], context={"product_count": actual_product_count,
                                                        "trusted_collection_metadata": trusted_collection})
                    except ValidationError as exc:
                        raise v2_parse_error(f"{output_label}原始结构不合法，禁止语义补判：{exc}", [str(exc)]) from exc
                    repair_original_data = recovered["data"]
                if is_timeliness:
                    # This protocol has exactly one semantic model response per
                    # attempt. Local lossless JSON cleanup is allowed above, but
                    # malformed output never triggers another model invocation.
                    raise v2_parse_error(
                        "时效性评估V1输出JSON语法不完整，未调用模型修复",
                        ["本地无损解析未成功；保留原始响应，按技术失败处理"],
                    )
                repaired = await self.client.repair_json(
                    raw_output,
                    label="垂域视觉对比评测输出",
                    round_no=2,
                )
                if is_strict_output:
                    parsed_pairs = parse_json_preserving_pairs(repaired)
                    repair_parsed_pairs = parsed_pairs
                    if parsed_pairs and parsed_pairs["top_level_type"] != "object":
                        raise v2_parse_error(f"{output_label}修复输出必须为JSON对象", parsed_pairs["structural_diagnostics"])
                    if parsed_pairs and parsed_pairs["duplicate_keys"]:
                        raise v2_parse_error(f"{output_label}修复输出存在重复键", duplicate_diagnostics(parsed_pairs))
                    data = parsed_pairs["data"] if parsed_pairs else None
                    if data is not None and repair_original_data is not None:
                        expected = set(repair_original_data)
                        changed = [key for key in expected if key not in data or json.dumps(
                            repair_original_data[key], sort_keys=True, ensure_ascii=False) != json.dumps(
                            data[key], sort_keys=True, ensure_ascii=False)]
                        extra = sorted(set(data) - expected)
                        if changed or extra:
                            raise v2_parse_error(f"{output_label}语法修复改变了原始判定，禁止采用", [
                                f"修复改变或删除原字段：{sorted(changed)}；新增字段：{extra}"])
                else:
                    data = parse_json_loose(repaired)
            if data is None:
                if is_strict_output:
                    raise v2_parse_error(f"{output_label}输出无法解析为JSON对象", ["模型输出及语法修复后仍无法解析"])
                raise JudgeOutputParseError(
                    "垂域视觉对比评测输出无法解析为 JSON",
                    raw_output=raw_output,
                    repair_output=repaired,
                    judge=self.client.cfg.name,
                    model=self.client.model,
                )

        if not compact_1022 and not is_strict_output and "content_conflict" in data and "has_conflict" not in data:
            data["has_conflict"] = data.pop("content_conflict")
        if not compact_1022 and not is_strict_output:
            data["product_count"] = actual_product_count

        try:
            if compact_1022:
                observation = data
            elif is_strict_output:
                observation = self.protocol.observation_model.model_validate(
                    data, context={"product_count": actual_product_count,
                                   "trusted_collection_metadata": trusted_collection})
            else:
                observation = self.protocol.observation_model.model_validate(data)
                observation.evaluation_datetime = evaluation_datetime
        except ValidationError as exc:
            if is_strict_output:
                raise v2_parse_error(f"{output_label}字段不合法：{exc}", [str(exc)]) from exc
            raise JudgeOutputParseError(
                f"垂域视觉对比评测字段不合法：{exc}",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
                error_category="schema" if strict_1022 else "json_parse",
            ) from exc

        risky_screenshot = is_screenshot and any(
            meta.get("split_status") == "risky" or any(b.get("status") == "risky" for b in meta.get("boundaries", []))
            for meta in metas
        )
        if risky_screenshot and not is_strict_output and not compact_1022:
            observation.needs_human_review = True
            observation.review_reasons = list(dict.fromkeys([*observation.review_reasons, RISKY_REVIEW_REASON]))
        result = visual_compare_result_fields(
            observation, self.protocol, **({"trusted_collection_metadata": trusted_collection} if is_strict_output else {}))
        if compact_1022:
            result["evaluation_datetime"] = evaluation_datetime
            result.update(raw_model_output=raw_output,
                          locally_repaired_json=bool(local_format_recovery_steps),
                          local_format_recovery_steps=local_format_recovery_steps)
        if is_strict_output:
            result.update(raw_model_output=raw_output,
                          repaired_model_output=repaired,
                          raw_key_pairs=original_parsed_pairs["raw_key_pairs"] if original_parsed_pairs else [],
                          original_raw_key_pairs=original_parsed_pairs["raw_key_pairs"] if original_parsed_pairs else [],
                          locally_repaired_key_pairs=original_parsed_pairs["locally_repaired_key_pairs"] if original_parsed_pairs else [],
                          repair_key_pairs=repair_parsed_pairs["raw_key_pairs"] if repair_parsed_pairs else [],
                          repair_locally_repaired_key_pairs=repair_parsed_pairs["locally_repaired_key_pairs"] if repair_parsed_pairs else [],
                          duplicate_keys=[], structural_diagnostics=parsed_pairs["structural_diagnostics"],
                          locally_repaired_json=parsed_pairs["locally_repaired"])
            if is_timeliness or is_knowledge_qa:
                # Audit time is never evidence of when a product was tested.
                result["evaluation_run_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                result["test_time_policy"] = "provided_evidence_only"
            else:
                result["evaluation_datetime"] = evaluation_datetime
            if risky_screenshot:
                result["review_reasons"] = list(dict.fromkeys([*result["review_reasons"], RISKY_REVIEW_REASON]))
                result.update(needs_human_review=True, needs_review=True, needs_review_label="T",
                              review_reason="；".join(result["review_reasons"]))
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
            "prompt_sha256": hashlib.sha256(
                f"{system}\0{user}".encode("utf-8")
            ).hexdigest(),
        })
        if is_strict_output:
            result["input_manifest_sha256"] = v2_input_manifest
        elif conversation_manifest:
            result["input_manifest_sha256"] = conversation_manifest
        elif query_metas:
            identity = {**self.protocol.public_metadata(), "original_hashes": [m["original_sha256"] for m in query_metas], "answers": [answer1, answer2, answer3],
                        "contexts": [context1, context2, context3]}
            result["input_manifest_sha256"] = input_manifest(
                question, "", content_parts, request_views, identity,
                self.profile.query_images.model_dump(),
            )
        return result
