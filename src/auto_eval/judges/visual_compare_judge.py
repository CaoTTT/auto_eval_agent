"""可按任务冻结 V0.2 简化版或 V0.3 的垂域视觉对比裁判。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..config import VisualModeProfile
from ..media import encode_frame
from ..long_screenshot import (
    RISKY_REVIEW_REASON, check_context_budget, encode_original_image,
)
from ..paths import resolve_project_path
from ..schema import VisualCompareObservation
from .base import JudgeClient, JudgeOutputParseError
from .prompts import parse_json_loose
from .compare_protocols import (
    CompareProtocol,
    DIMENSIONS,
    resolve_compare_protocol,
)


# 按当前实验约定：准确性保留模型结果，但暂不参与任何汇总或总排名。
AGGREGATION_DIMENSIONS = tuple(d for d in DIMENSIONS if d != "accuracy")


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


def visual_compare_result_fields(
    observation: VisualCompareObservation,
    protocol: CompareProtocol | str | None = None,
) -> dict[str, Any]:
    """返回扁平结果；新增字段可直接被 Excel 导出，旧字段继续存在。"""
    selected_protocol = (
        resolve_compare_protocol(protocol) if isinstance(protocol, str)
        else protocol or resolve_compare_protocol(None)
    )
    _finalize_observation(observation, selected_protocol)
    result = observation.model_dump()
    result["evaluation_profile"] = selected_protocol.id
    result["bundle_revision"] = selected_protocol.bundle_revision
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
        evidence_mode: str = "video_frames",
        screenshot_metas: list[dict] | None = None,
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
        if evidence_mode not in ("video_frames", "long_screenshot"):
            raise ValueError("未知视觉证据模式")
        is_screenshot = evidence_mode == "long_screenshot"
        metas = screenshot_metas or []
        if is_screenshot and len(metas) != actual_product_count:
            raise ValueError("长截图缺少逐产品预处理元数据")

        system = self.protocol.system_template.render(
            persona=self.client.persona,
            product_count=actual_product_count,
            evidence_mode=evidence_mode,
        )
        user = self.protocol.user_template.render(
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
            for product_no, frames in enumerate(frame_groups, 1):
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
                            "product_no": product_no, "part_no": part_no, "part_count": count,
                            "position": position, "split_status": meta["split_status"], "ref_path": path,
                            **({"preprocessing": meta} if part_no == 1 else {}),
                        })
                    content_parts.append({"type": "text", "text": f"产品{product_no}长截图结束"})
                    user_image_refs.extend(frames)
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

            return user_images, user_image_refs, content_parts, image_metadata

        user_images, user_image_refs, content_parts, image_metadata = await asyncio.to_thread(prepare_images)

        started = time.perf_counter()
        if is_screenshot:
            check_context_budget(system, [p["text"] for p in content_parts if p["type"] == "text"], metas, self.profile.long_screenshot)
            raw_output = await self.client.complete(
                system, user, stream_callback=stream_callback,
                content_parts=content_parts, image_metadata=image_metadata,
                user_image_refs=user_image_refs,
            )
        else:
            raw_output = await self.client.complete(
                system, user, stream_callback=stream_callback,
                user_images=user_images or None, user_image_refs=user_image_refs or None,
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
            observation = self.protocol.observation_model.model_validate(data)
            observation.evaluation_datetime = evaluation_datetime
        except ValidationError as exc:
            raise JudgeOutputParseError(
                f"垂域视觉对比评测字段不合法：{exc}",
                raw_output=raw_output,
                repair_output=repaired,
                judge=self.client.cfg.name,
                model=self.client.model,
            ) from exc

        if is_screenshot and any(
            meta.get("split_status") == "risky" or any(b.get("status") == "risky" for b in meta.get("boundaries", []))
            for meta in metas
        ):
            observation.needs_human_review = True
            observation.review_reasons = list(dict.fromkeys([*observation.review_reasons, RISKY_REVIEW_REASON]))
        result = visual_compare_result_fields(observation, self.protocol)
        result.update({
            "judge": self.client.cfg.name,
            "judge_model": self.client.model,
            "judge_latency_ms": int((time.perf_counter() - started) * 1000),
            "prompt_sha256": hashlib.sha256(
                f"{system}\0{user}".encode("utf-8")
            ).hexdigest(),
        })
        return result
