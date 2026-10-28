"""Strict, non-mutating model-output contract for the 1022 comparison protocol.

The model supplies judgments only. Task identity, rankings and other derived
fields belong to the application. No missing judgment is filled in here.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model


STANDARD_VERSION_1022 = "0.2-simplified-1022"
OUTPUT_CONTRACT_1022 = "compare_1022_v1"
DIMENSIONS_1022 = (
    "understanding", "accuracy", "service_closure", "scenario_fulfillment",
    "intuitive_efficiency", "evidence_quality", "guided_recommendation",
)
Score = Annotated[int, Field(strict=True, ge=1, le=5)]
Gate = Literal["pass", "fail", "unclear"]
InputStatus = Literal["complete", "partial", "failed"]
Verification = Literal["verified", "partial", "unverifiable", "not_required"]
ShortText = Annotated[str, Field(min_length=1, max_length=600)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=400)]
EvidenceList = Annotated[list[EvidenceText], Field(max_length=12)]
IssueText = Annotated[str, Field(max_length=600)]
ReasonText = Annotated[str, Field(min_length=1, max_length=1800)]


class OutputContractError(ValueError):
    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


class _StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _fields() -> dict:
    fields = {}
    for n in (1, 2, 3):
        fields[f"answer{n}_input_status"] = (
            InputStatus | None if n == 3 else InputStatus, ...,
        )
    for gate in ("response", "safety"):
        for n in (1, 2, 3):
            fields[f"answer{n}_{gate}_gate"] = (Gate | None if n == 3 else Gate, ...)
            fields[f"answer{n}_{gate}_gate_reason"] = (
                ShortText | None if n == 3 else ShortText, ...,
            )
    for dimension in DIMENSIONS_1022:
        fields[f"{dimension}_applicable"] = (bool, ...)
        fields[f"{dimension}_verification_status"] = (Verification, ...)
        fields[f"{dimension}_evidence"] = (EvidenceList, ...)
        fields[f"{dimension}_primary_issue"] = (IssueText, ...)
        fields[f"{dimension}_reason"] = (ReasonText, ...)
        for n in (1, 2, 3):
            fields[f"answer{n}_{dimension}_score"] = (Score | None, ...)
    fields.update({
        "content_conflict": (Literal["yes", "no", "unclear"], ...),
        "conflict_reason": (ShortText, ...),
        "evidence": (EvidenceList, ...),
        "confidence": (Literal["high", "medium", "low"], ...),
        "needs_human_review": (bool, ...),
        "review_reasons": (Annotated[list[EvidenceText], Field(max_length=12)], ...),
        "rationale": (Annotated[str, Field(min_length=1, max_length=1600)], ...),
    })
    return fields


Compare1022Output = create_model("Compare1022Output", __base__=_StrictOutput, **_fields())

# Match explicit grading language, not durations (4分钟), fractions (四分之一),
# source quotations, currency or other numbers in the evidence.
_SCORE_CLAIM = re.compile(
    r'(?:(?:^|[：:；;，,。“「『"]\s*)|(?:本次|本维度|该维度|裁判|最终|综合|最终裁判))'
    r"(?:评分|得分|分数|打分)\s*(?:为|是|应为|应是|[:：=])?\s*[0-5一二三四五](?![\d.．])"
    r"|(?:给|给出|判为|判|评为|评|扣至|降至|降为|定为)\s*[0-5一二三四五]\s*分(?!钟|之)"
    r"|[0-5一二三四五]\s*分档"
)
_PRODUCT_SECTION = re.compile(r"(?:^|[；;]\s*)产品([123])[:：]")
_ATTRIBUTED_QUOTE = re.compile(
    r"(?:用户(?:原文|要求|提问|输入)|引用原文|原文|"
    r"回答(?:原文|中|写道|写着|提到)|(?:页面|卡片|正文)(?:显示|写着|标注|记载)|"
    r"产品[123](?:原文|回答))\s*(?:是|为|中|写道|写着|提到|：|:)*\s*"
    r'(?:“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*")'
)
_UNSCORED_REASON = re.compile(r"未评分（([^（）]+)）：(.+)", re.DOTALL)


def _has_explanation(text: str) -> bool:
    """Reject empty/punctuation-only placeholders, without judging semantics."""
    return bool(re.search(r"\w", text))


def _valid_unscored_reason(text: str) -> bool:
    match = _UNSCORED_REASON.fullmatch(text)
    return bool(match and all(_has_explanation(part) for part in match.groups()))


def _state_error(message: str) -> None:
    raise OutputContractError(message, "state")


def _sections(text: str, product_count: int, field: str) -> dict[int, str]:
    matches = list(_PRODUCT_SECTION.finditer(text))
    if [int(match[1]) for match in matches] != list(range(1, product_count + 1)):
        _state_error(f"{field} 必须按产品顺序分别填写且不得重复或遗漏产品")
    if text[:matches[0].start()].strip():
        _state_error(f"{field} 不得在产品分段之前附加评价")
    result = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip().rstrip("；;").strip()
        if not value:
            _state_error(f"{field} 的产品{match[1]}说明不能为空")
        result[int(match[1])] = value
    return result


def _validate_states(data: dict, product_count: int) -> None:
    if product_count not in (2, 3):
        raise ValueError("product_count 必须是2或3")
    review_required = data["confidence"] == "low"
    for field, value in data.items():
        if isinstance(value, str) and field.endswith(("reason", "primary_issue", "rationale")):
            # Only explicitly attributed source quotations are exempt. Bare
            # quotes around a judge's own grading must not bypass this check.
            if _SCORE_CLAIM.search(_ATTRIBUTED_QUOTE.sub("", value)):
                _state_error(f"{field} 不得重复声明数值评分；最终分数仅写入score字段")
        if isinstance(value, str) and field.endswith("reason") and not value.strip():
            _state_error(f"{field} 不能仅包含空白")
        if isinstance(value, list) and any(isinstance(item, str) and not item.strip() for item in value):
            _state_error(f"{field} 不得包含空白条目")
    if not data["rationale"].strip():
        _state_error("rationale 不能仅包含空白")
    for n in (1, 2, 3):
        prefix = f"answer{n}_"
        if n > product_count:
            if any(value is not None for key, value in data.items() if key.startswith(prefix)):
                _state_error("双产品模式的所有answer3字段必须为null")
            continue
        for suffix in ("input_status", "response_gate", "response_gate_reason", "safety_gate", "safety_gate_reason"):
            if data[prefix + suffix] is None:
                _state_error(f"{prefix + suffix} 是实际参评产品的必填值")
        if data[prefix + "input_status"] == "failed":
            if any(data[prefix + gate + "_gate"] != "unclear" for gate in ("response", "safety")):
                _state_error(f"产品{n}输入失败时两个Gate必须为unclear")
            review_required = True
        if any(data[prefix + gate + "_gate"] == "unclear" for gate in ("response", "safety")):
            review_required = True

    for dimension in DIMENSIONS_1022:
        applicable = data[f"{dimension}_applicable"]
        verification = data[f"{dimension}_verification_status"]
        issue = data[f"{dimension}_primary_issue"]
        reason = data[f"{dimension}_reason"]
        evidence = data[f"{dimension}_evidence"]
        evidence_products = set()
        for item in evidence:
            parts = [part.strip() for part in item.split("|", 3)]
            if len(parts) != 4 or parts[0] != dimension or not parts[2] or not parts[3]:
                _state_error(f"{dimension}_evidence 必须是维度 | 产品N | 位置 | 可见事实")
            if parts[1] not in {f"产品{n}" for n in range(1, product_count + 1)}:
                _state_error(f"{dimension}_evidence 引用了不存在的产品")
            evidence_products.add(int(parts[1][-1]))
        if not applicable:
            if verification != "not_required" or issue != "":
                _state_error(f"{dimension}不适用时必须not_required且primary_issue为空字符串")
            if not reason.startswith("不适用：") or not _has_explanation(reason[len("不适用："):]):
                _state_error(f"{dimension}_reason 不适用时须以“不适用：”解释原因")
            reason_parts = issue_parts = {}
        else:
            reason_parts = _sections(reason, product_count, f"{dimension}_reason")
            issue_parts = _sections(issue, product_count, f"{dimension}_primary_issue")
        if applicable and verification == "unverifiable" and dimension != "accuracy":
            review_required = True
        for n in range(1, product_count + 1):
            score = data[f"answer{n}_{dimension}_score"]
            blocked = (
                not applicable or verification == "unverifiable"
                or data[f"answer{n}_input_status"] == "failed"
                or data[f"answer{n}_response_gate"] != "pass"
            )
            if blocked and score is not None:
                _state_error(f"answer{n}_{dimension}_score状态不允许评分，禁止静默清分")
            if not blocked and score is None:
                _state_error(f"answer{n}_{dimension}_score应评分却缺分，禁止用默认NA补齐")
            if applicable and blocked and not _valid_unscored_reason(reason_parts[n]):
                _state_error(f"{dimension}_reason产品{n}须用“未评分（具体原因）：具体说明”解释空分")
            if not blocked:
                if reason_parts[n].startswith("未评分"):
                    _state_error(f"{dimension}产品{n}有分数却声明未评分")
                if n not in evidence_products:
                    _state_error(f"{dimension}产品{n}评分必须至少有一条可定位证据")
                no_issue = issue_parts[n].rstrip("。") == "无实质问题"
                if (score == 5) != no_issue:
                    _state_error(f"{dimension}产品{n}最高档须无实质问题，扣分须指出实际缺陷")

    if review_required and not data["needs_human_review"]:
        _state_error("当前状态需要人工复核，needs_human_review不能为false")
    if data["needs_human_review"] != bool(data["review_reasons"]):
        _state_error("needs_human_review必须与非空review_reasons一致")


def validate_1022_output(data: dict, product_count: int) -> dict:
    """Validate without replacing, rounding or clearing any supplied judgment."""
    try:
        validated = Compare1022Output.model_validate(data, strict=True).model_dump()
    except ValidationError as exc:
        raise OutputContractError(f"1022输出字段或类型不合法：{exc}", "schema") from exc
    _validate_states(validated, product_count)
    return validated


def parse_1022_output(raw: str, product_count: int) -> dict:
    """Accept exactly one JSON object; never ask a repair model to invent fields."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复JSON字段：{key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"非法JSON常量：{value}")

    try:
        data = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(data, dict):
            raise ValueError("顶层必须为单个JSON对象")
    except (ValueError, TypeError, RecursionError) as exc:
        raise OutputContractError(f"1022输出JSON语法不合法：{exc}", "json_syntax") from exc
    return validate_1022_output(data, product_count)


def output_schema_1022(product_count: int) -> dict:
    """Schema and runtime share field definitions; task-specific nulls are explicit."""
    if product_count not in (2, 3):
        raise ValueError("product_count 必须是2或3")
    schema = copy.deepcopy(Compare1022Output.model_json_schema())
    for name, spec in schema["properties"].items():
        if name.startswith("answer3_"):
            if product_count == 2:
                spec.clear()
                spec["type"] = "null"
            elif not name.endswith("_score") and "anyOf" in spec:
                options = [part for part in spec["anyOf"] if part.get("type") != "null"]
                spec.clear()
                spec.update(options[0])
    return schema


def response_format_1022(mode: str, product_count: int) -> dict | None:
    if mode == "off":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": "compare_1022", "strict": True, "schema": output_schema_1022(product_count)},
        }
    raise ValueError(f"未知结构化输出模式：{mode}")


def output_field_instructions_1022(product_count: int) -> str:
    """Human-readable field list generated from the very same strict schema."""
    schema = output_schema_1022(product_count)

    def describe(spec: dict) -> str:
        if "anyOf" in spec:
            return " 或 ".join(describe(part) for part in spec["anyOf"])
        if "enum" in spec:
            return "字符串，选择 " + " / ".join(spec["enum"])
        kind = spec.get("type")
        if kind == "array":
            return f"数组（最多{spec.get('maxItems', 12)}条），元素为" + describe(spec["items"])
        if kind == "string":
            return f"字符串（最多{spec.get('maxLength', 1800)}字符）"
        return {"integer": "1—5整数（不能是布尔值、小数或字符串）", "boolean": "布尔值 true 或 false", "null": "JSON null"}.get(kind, str(kind))

    return "\n".join(f"- {name}：{describe(spec)}" for name, spec in schema["properties"].items())


def example_1022_output(product_count: int = 2, *, evidence_mode: str = "video_frames") -> dict:
    """Synthetic greeting with a supplied final frame and visually bold core text."""
    data = {}
    for n in (1, 2, 3):
        data[f"answer{n}_input_status"] = "complete" if n <= product_count else None
    for gate in ("response", "safety"):
        for n in (1, 2, 3):
            data[f"answer{n}_{gate}_gate"] = "pass" if n <= product_count else None
            data[f"answer{n}_{gate}_gate_reason"] = (
                ("可见回答完整可读，无响应故障。" if gate == "response" else "正常问候，无可见安全风险。")
                if n <= product_count else None
            )
    for dimension in DIMENSIONS_1022:
        applicable = dimension in ("understanding", "intuitive_efficiency")
        data[f"{dimension}_applicable"] = applicable
        data[f"{dimension}_verification_status"] = "not_required"
        data[f"{dimension}_evidence"] = [
            f"{dimension} | 产品{n} | "
            + (f"产品{n} 第1/1块" if evidence_mode == "long_screenshot" else "第1帧")
            + " | 以简短完整问候回应用户，核心问候适量加粗且渲染正常。"
            for n in range(1, product_count + 1)
        ] if applicable else []
        data[f"{dimension}_primary_issue"] = "；".join(
            f"产品{n}：无实质问题" for n in range(1, product_count + 1)
        ) if applicable else ""
        data[f"{dimension}_reason"] = "；".join(
            f"产品{n}：可见问候与用户意图一致，核心适量加粗，表达简短完整，没有额外理解成本。"
            for n in range(1, product_count + 1)
        ) if applicable else "不适用：当前只是问候，没有触发此维度的任务或行为。"
        for n in (1, 2, 3):
            data[f"answer{n}_{dimension}_score"] = 5 if applicable and n <= product_count else None
    data.update({
        "content_conflict": "no", "conflict_reason": "问候未包含可比较的事实主张。",
        "evidence": [], "confidence": "high", "needs_human_review": False,
        "review_reasons": [], "rationale": "各产品均简短回应问候，无实质缺陷。",
    })
    return validate_1022_output(data, product_count)
