"""V0.2 垂域视觉对比裁判：兼容双产品，增量支持三产品。"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..config import VisualModeProfile
from ..media import encode_frame
from ..schema import VisualCompareObservation
from .base import JudgeClient, JudgeOutputParseError
from .prompts import parse_json_loose
from .visual_compare_prompt_v02 import VISUAL_COMPARE_SYSTEM, VISUAL_COMPARE_USER


STANDARD_ID = "qa_competitor_compare"
STANDARD_VERSION = "0.2-simplified"
DIMENSIONS = (
    "understanding",
    "accuracy",
    "service_closure",
    "scenario_fulfillment",
    "intuitive_efficiency",
    "evidence_quality",
    "guided_recommendation",
)
# 按当前实验约定：准确性保留模型结果，但暂不参与任何汇总或总排名。
AGGREGATION_DIMENSIONS = tuple(d for d in DIMENSIONS if d != "accuracy")


def _product_nos(observation: VisualCompareObservation) -> tuple[int, ...]:
    return (1, 2, 3) if observation.product_count == 3 else (1, 2)


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


def _normalize_observation_state(observation: VisualCompareObservation) -> None:
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
            response_pass = getattr(observation, f"answer{answer_no}_response_gate") == "pass"
            if not applicable or unverifiable or input_failed or not response_pass:
                setattr(observation, f"answer{answer_no}_{dimension}_score", None)


def _score_is_required(
    observation: VisualCompareObservation,
    answer_no: int,
    dimension: str,
) -> bool:
    if not getattr(observation, f"{dimension}_applicable"):
        return False
    if getattr(observation, f"{dimension}_verification_status") == "unverifiable":
        return False
    if getattr(observation, f"answer{answer_no}_input_status") == "failed":
        return False
    return getattr(observation, f"answer{answer_no}_response_gate") == "pass"


def _finalize_observation(observation: VisualCompareObservation) -> None:
    """只确定性计算派生字段，不让模型自行创造总分权重。"""
    _normalize_observation_state(observation)
    observation.standard_id = STANDARD_ID
    observation.standard_version = STANDARD_VERSION

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

    # V0.2 尚无正式权重；禁止擅自计算总分和整体第一名。
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
    observation.content_quality_reason = "V0.2未定义正式维度权重，不生成综合内容质量胜方"
    observation.need_closure = observation.service_closure_winner
    observation.need_closure_reason = observation.service_closure_reason
    observation.personalization = observation.scenario_fulfillment_winner
    observation.personalization_reason = observation.scenario_fulfillment_reason

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
            if _score_is_required(observation, answer_no, dimension)
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


def visual_compare_result_fields(
    observation: VisualCompareObservation,
) -> dict[str, Any]:
    """返回扁平结果；新增字段可直接被 Excel 导出，旧字段继续存在。"""
    _finalize_observation(observation)
    result = observation.model_dump()
    result["needs_review_label"] = "T" if observation.needs_review else "F"
    return result


class VisualCompareJudge:
    """同一调用兼容2个产品，并通过可选参数增量支持第3个产品。"""

    def __init__(self, client: JudgeClient, profile: VisualModeProfile):
        self.client = client
        self.profile = profile

    async def evaluate(
        self,
        *,
        question: str,
        context: str = "",
        context1: str = "",
        answer1: str = "",
        frames1: list[str] | None = None,
        context2: str = "",
        answer2: str = "",
        frames2: list[str] | None = None,
        context3: str = "",
        answer3: str = "",
        frames3: list[str] | None = None,
        product_count: Literal[2, 3] | None = None,
        evaluation_time: datetime | None = None,
        stream_callback=None,
    ) -> dict[str, Any]:
        extraction = self.profile.extraction
        now = evaluation_time or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        evaluation_datetime = now.isoformat(timespec="seconds")
        actual_product_count: Literal[2, 3] = product_count or (
            3 if (context3 or answer3 or frames3 is not None) else 2
        )

        system = VISUAL_COMPARE_SYSTEM.render(
            persona=self.client.persona,
            product_count=actual_product_count,
        )
        user = VISUAL_COMPARE_USER.render(
            evaluation_datetime=evaluation_datetime,
            question=question,
            context=context,
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
        )

        user_images: list[str] = []
        user_image_refs: list[str] = []
        frame_groups = (frames1, frames2, frames3) if actual_product_count == 3 else (frames1, frames2)
        for frames in frame_groups:
            if not frames:
                continue
            user_images.extend(
                encode_frame(
                    Path(path),
                    max_edge=extraction.max_edge,
                    quality=extraction.jpeg_quality,
                )
                for path in frames
            )
            user_image_refs.extend(frames)

        started = time.perf_counter()
        raw_output = await self.client.complete(
            system,
            user,
            stream_callback=stream_callback,
            user_images=user_images or None,
            user_image_refs=user_image_refs or None,
        )

        data = parse_json_loose(raw_output)
        repaired = ""
        if data is None:
            repaired = await self.client.repair_json(
                raw_output,
                label="垂域视觉对比评测输出",
                round_no=2,
            )
            data = parse_json_loose(repaired)
        if data is None:
            raise JudgeOutputParseError(
                "垂域视觉对比评测输出无法解析为 JSON",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            )

        if "content_conflict" in data and "has_conflict" not in data:
            data["has_conflict"] = data.pop("content_conflict")
        data["product_count"] = actual_product_count

        try:
            observation = VisualCompareObservation.model_validate(data)
            observation.evaluation_datetime = evaluation_datetime
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"垂域视觉对比评测字段不合法：{exc}",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        result = visual_compare_result_fields(observation)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
        })
        return result
