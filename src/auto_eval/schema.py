"""核心数据模型。

所有跨模块流转的结构都在这里定义，用 pydantic v2。
仅保留垂域视觉评测（rich_content）与垂域视觉对比评测（compare）所需模型。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

AnswerCoverage = Literal["complete", "partial", "unclear"]
CompareWinner = Literal["answer1", "answer2", "tie"]
ConflictVerdict = Literal["yes", "no", "unclear"]
GateStatus = Literal["pass", "fail", "unclear"]
JudgeConfidence = Literal["low", "medium", "high"]
InputStatus = Literal["complete", "partial", "failed"]
ProductId = Literal["product1", "product2", "product3"]
QualityScore = Literal[0, 1, 2, 3]
VerificationStatus = Literal["verified", "partial", "unverifiable", "not_required"]


# --------------------------------------------------------------------------- #
# 评测集
# --------------------------------------------------------------------------- #
class EvalItem(BaseModel):
    """一条评测题。"""

    id: str
    question: str
    query_images: list[str] = Field(default_factory=list, max_length=1)
    input_modality: Literal["text", "text_image"] = "text"
    context: str | None = None  # 可选背景/多模态描述
    category: str = "default"  # 垂域（分组展示用）
    media: list[str] = Field(default_factory=list)  # 任务类评测：录屏/图片本地路径（裁判抽帧后以 image_url 多图盲评）
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_question_images(self):
        if any(not path.strip() for path in self.query_images):
            raise ValueError("提问图片路径不能为空")
        modality = "text_image" if self.query_images else "text"
        if "input_modality" in self.model_fields_set and self.input_modality != modality:
            raise ValueError("input_modality 与提问图片声明冲突")
        self.input_modality = modality
        return self


# --------------------------------------------------------------------------- #
# 垂域视觉评测（rich_content）
# --------------------------------------------------------------------------- #
class RichContentCard(BaseModel):
    """视觉裁判识别到的一张结构化富内容挂卡。"""

    type: str
    entity: str = ""
    visible_content: str = ""
    answer_position: str = ""
    evidence_frames: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RichContentSuperlink(BaseModel):
    """回答区域中一处可点击蓝色文字；同一处跨帧重复只保留一条。"""

    text: str
    answer_position: str = ""
    surrounding_context: str = ""
    evidence_frames: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RichContentObservation(BaseModel):
    """一次垂域视觉评测视频识别结果。"""

    answer_coverage: AnswerCoverage = "unclear"
    visual_description: str = ""  # 纯客观视觉描述（Part 1），不包含评价性语言
    turn_summary: str = ""  # 本轮总结(≤120字)：用户意图+核心结果/关键实体+是否闭环，供多轮下一轮 context 用
    cards: list[RichContentCard] = Field(default_factory=list)
    superlinks: list[RichContentSuperlink] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""
    card_suitability: str = ""  # Part 2：卡片是否合适（"ok"/"nok"/""）
    card_suitability_reason: str = ""  # Part 2：卡片是否合适的原因
    superlink_suitability: str = ""  # Part 2：Superlink是否合适（"ok"/"nok"/""）
    superlink_suitability_reason: str = ""  # Part 2：Superlink是否合适的原因
    problem_solved: str = ""  # Part 2：是否解决了用户问题（"ok"/"nok"/"need_review"）
    problem_solved_reason: str = ""  # Part 2：评价的原因
    answer_issues: str = ""  # Part 2：回答的内容有什么问题（分类标签：具体描述）
    rationale: str = ""


# --------------------------------------------------------------------------- #
# 垂域视觉对比评测（compare）
# --------------------------------------------------------------------------- #
class VisualCompareObservation(BaseModel):
    """V0.3 多模态对比结果。

    继续采用扁平字段，避免重写 Web/Excel 链路；answer3 为增量字段。
    旧五维字段仅作为产品1/产品2兼容投影，不代表三产品正式总排名。
    """

    relevance: CompareWinner | None = None
    relevance_reason: str = ""
    safety: CompareWinner | None = None
    safety_reason: str = ""
    content_quality: CompareWinner | None = None
    content_quality_reason: str = ""
    need_closure: CompareWinner | None = None
    need_closure_reason: str = ""
    personalization: CompareWinner | None = None
    personalization_reason: str = ""

    has_conflict: ConflictVerdict = "unclear"
    conflict_reason: str = ""

    needs_review: bool = False
    review_reason: str = ""
    rationale: str = ""

    standard_id: str = "qa_competitor_compare"
    standard_version: str = "0.3"
    evaluation_datetime: str = ""
    product_count: Literal[2, 3] = 3

    answer1_input_status: InputStatus = "complete"
    answer2_input_status: InputStatus = "complete"
    answer3_input_status: InputStatus | None = "complete"

    answer1_response_gate: GateStatus = "unclear"
    answer1_response_gate_reason: str = ""
    answer2_response_gate: GateStatus = "unclear"
    answer2_response_gate_reason: str = ""
    answer1_safety_gate: GateStatus = "unclear"
    answer1_safety_gate_reason: str = ""
    answer2_safety_gate: GateStatus = "unclear"
    answer2_safety_gate_reason: str = ""
    answer3_response_gate: GateStatus | None = "unclear"
    answer3_response_gate_reason: str | None = None
    answer3_safety_gate: GateStatus | None = "unclear"
    answer3_safety_gate_reason: str | None = None

    understanding_applicable: bool = True
    understanding_verification_status: VerificationStatus = "not_required"
    understanding_evidence: list[str] = Field(default_factory=list)
    understanding_primary_issue: str = ""
    understanding_reason: str = ""
    answer1_understanding_score: QualityScore | None = None
    answer2_understanding_score: QualityScore | None = None
    answer3_understanding_score: QualityScore | None = None
    understanding_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    understanding_winner: CompareWinner | None = None

    accuracy_applicable: bool = True
    accuracy_verification_status: VerificationStatus = "unverifiable"
    accuracy_evidence: list[str] = Field(default_factory=list)
    accuracy_primary_issue: str = ""
    accuracy_reason: str = ""
    answer1_accuracy_score: QualityScore | None = None
    answer2_accuracy_score: QualityScore | None = None
    answer3_accuracy_score: QualityScore | None = None
    accuracy_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    accuracy_winner: CompareWinner | None = None

    service_closure_applicable: bool = False
    service_closure_verification_status: VerificationStatus = "not_required"
    service_closure_evidence: list[str] = Field(default_factory=list)
    service_closure_primary_issue: str = ""
    service_closure_reason: str = ""
    answer1_service_closure_score: QualityScore | None = None
    answer2_service_closure_score: QualityScore | None = None
    answer3_service_closure_score: QualityScore | None = None
    service_closure_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    service_closure_winner: CompareWinner | None = None

    scenario_fulfillment_applicable: bool = False
    scenario_fulfillment_verification_status: VerificationStatus = "not_required"
    scenario_fulfillment_evidence: list[str] = Field(default_factory=list)
    scenario_fulfillment_primary_issue: str = ""
    scenario_fulfillment_reason: str = ""
    answer1_scenario_fulfillment_score: QualityScore | None = None
    answer2_scenario_fulfillment_score: QualityScore | None = None
    answer3_scenario_fulfillment_score: QualityScore | None = None
    scenario_fulfillment_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    scenario_fulfillment_winner: CompareWinner | None = None

    intuitive_efficiency_applicable: bool = True
    intuitive_efficiency_verification_status: VerificationStatus = "not_required"
    intuitive_efficiency_evidence: list[str] = Field(default_factory=list)
    intuitive_efficiency_primary_issue: str = ""
    intuitive_efficiency_reason: str = ""
    answer1_intuitive_efficiency_score: QualityScore | None = None
    answer2_intuitive_efficiency_score: QualityScore | None = None
    answer3_intuitive_efficiency_score: QualityScore | None = None
    intuitive_efficiency_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    intuitive_efficiency_winner: CompareWinner | None = None

    evidence_quality_applicable: bool = False
    evidence_quality_verification_status: VerificationStatus = "not_required"
    evidence_quality_evidence: list[str] = Field(default_factory=list)
    evidence_quality_primary_issue: str = ""
    evidence_quality_reason: str = ""
    answer1_evidence_quality_score: QualityScore | None = None
    answer2_evidence_quality_score: QualityScore | None = None
    answer3_evidence_quality_score: QualityScore | None = None
    evidence_quality_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    evidence_quality_winner: CompareWinner | None = None

    guided_recommendation_applicable: bool = False
    guided_recommendation_verification_status: VerificationStatus = "not_required"
    guided_recommendation_evidence: list[str] = Field(default_factory=list)
    guided_recommendation_primary_issue: str = ""
    guided_recommendation_reason: str = ""
    answer1_guided_recommendation_score: QualityScore | None = None
    answer2_guided_recommendation_score: QualityScore | None = None
    answer3_guided_recommendation_score: QualityScore | None = None
    guided_recommendation_rank_groups: list[list[ProductId]] = Field(default_factory=list)
    guided_recommendation_winner: CompareWinner | None = None

    answer1_total_score: float | None = Field(default=None, ge=0.0, le=100.0)
    answer2_total_score: float | None = Field(default=None, ge=0.0, le=100.0)
    answer3_total_score: float | None = Field(default=None, ge=0.0, le=100.0)
    overall_winner: CompareWinner | None = None
    overall_ranking: list[list[ProductId]] | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: JudgeConfidence = "low"
    needs_human_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


    @model_validator(mode="after")
    def normalize_compare_states(self) -> "VisualCompareObservation":
        """规范化 applicable / verifiable / score 的状态关系。

        这里以“兼容旧结果”为优先：对模型可确定修复的状态直接归一化，
        不因单个派生字段不一致让整条评测解析失败。真正需要人工复核的
        情况由 visual_compare_judge 的确定性收尾逻辑统一标记。
        """
        dimensions = (
            "understanding", "accuracy", "service_closure",
            "scenario_fulfillment", "intuitive_efficiency",
            "evidence_quality", "guided_recommendation",
        )
        product_nos = (1, 2, 3) if self.product_count == 3 else (1, 2)

        if self.product_count == 2:
            self.answer3_input_status = None
            self.answer3_response_gate = None
            self.answer3_response_gate_reason = None
            self.answer3_safety_gate = None
            self.answer3_safety_gate_reason = None
            for dimension in dimensions:
                setattr(self, f"answer3_{dimension}_score", None)

        # N/A 维度必须没有绝对分。
        for dimension in dimensions:
            if not getattr(self, f"{dimension}_applicable"):
                for answer_no in (1, 2, 3):
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)
                setattr(self, f"{dimension}_winner", None)
                setattr(self, f"{dimension}_rank_groups", [])

        # 无法核验时禁止保留猜测分；输入失败或任一 Gate 失败时不评分。
        # Gate=unclear 是人工复核信号，但证据足够的后续维度仍需评分。
        for dimension in dimensions:
            if getattr(self, f"{dimension}_verification_status") == "unverifiable":
                for answer_no in product_nos:
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)
            for answer_no in product_nos:
                input_failed = getattr(self, f"answer{answer_no}_input_status") == "failed"
                response_failed = getattr(self, f"answer{answer_no}_response_gate") == "fail"
                safety_failed = getattr(self, f"answer{answer_no}_safety_gate") == "fail"
                if input_failed or response_failed or safety_failed:
                    setattr(self, f"answer{answer_no}_{dimension}_score", None)

        return self
