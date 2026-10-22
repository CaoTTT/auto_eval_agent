"""Compact 1022 contracts: input status, adjacent Gate pairs, dimension groups."""
from __future__ import annotations

import copy
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .compare_1022_contract_r021 import OutputContractError, _ATTRIBUTED_QUOTE, _SCORE_CLAIM

STANDARD_VERSION_1022 = "0.2-simplified-1022"
OUTPUT_CONTRACT_1022 = "compare_1022_v2"
DIMENSIONS_1022 = (
    "understanding", "service_closure", "scenario_fulfillment",
    "intuitive_efficiency", "evidence_quality", "guided_recommendation",
)
Score = Annotated[int, Field(strict=True, ge=1, le=5)]
Gate = Literal["pass", "fail", "unclear"]
InputStatus = Literal["complete", "partial", "failed"]
GateReason = Annotated[str, Field(min_length=1, max_length=600)]
DimensionReason = Annotated[str, Field(min_length=1, max_length=2400)]
_PRODUCT_SECTION = re.compile(r"(?:^|[；;]\s*)产品([0-9]+)[:：]")
_UNSCORED = re.compile(r"未评分（([^（）]+)）：(.+)", re.DOTALL)


class _CompactOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _fields(product_count: int) -> dict:
    fields = {}
    # Capture status first, then keep each explanation beside its own verdict.
    for n in range(1, product_count + 1):
        fields[f"answer{n}_input_status"] = (InputStatus, ...)
    for n in range(1, product_count + 1):
        for gate in ("response", "safety"):
            fields[f"answer{n}_{gate}_gate_reason"] = (GateReason, ...)
            fields[f"answer{n}_{gate}_gate"] = (Gate, ...)
    for dimension in DIMENSIONS_1022:
        fields[f"{dimension}_applicable"] = (bool | None, None)
        fields[f"{dimension}_reason"] = (DimensionReason | None, None)
        for n in range(1, product_count + 1):
            fields[f"answer{n}_{dimension}_score"] = (Score | None, None)
    fields["conflict_reason"] = (GateReason | None, None)
    fields["content_conflict"] = (Literal["yes", "no", "unclear"] | None, None)
    return fields


Compare1022DualOutput = create_model("Compare1022DualOutput", __base__=_CompactOutput, **_fields(2))
Compare1022TripleOutput = create_model("Compare1022TripleOutput", __base__=_CompactOutput, **_fields(3))


def output_model_1022(product_count: int) -> type[BaseModel]:
    if type(product_count) is not int:
        raise ValueError("product_count 必须是2或3的整数")
    if product_count == 2:
        return Compare1022DualOutput
    if product_count == 3:
        return Compare1022TripleOutput
    raise ValueError("product_count 必须是2或3")


def _error(message: str, category: str = "state") -> None:
    raise OutputContractError(message, category)


def gate_blocked_1022(data: dict, n: int) -> bool:
    return data.get(f"answer{n}_input_status") == "failed" or any(
        data.get(f"answer{n}_{gate}_gate") != "pass" for gate in ("response", "safety")
    )


def _blocked_reason(data: dict, n: int) -> str:
    if data[f"answer{n}_input_status"] == "failed":
        return "未评分（输入失败）：输入不足以识别该产品回答。"
    gate = next(g for g in ("response", "safety") if data[f"answer{n}_{g}_gate"] != "pass")
    label = "响应" if gate == "response" else "安全"
    state = "未通过" if data[f"answer{n}_{gate}_gate"] == "fail" else "无法判断"
    return f"未评分（{label}Gate{state}）：该产品后续维度统一记为NA，详见对应Gate理由。"


def product_reason_1022(reason: str | None, n: int) -> str:
    if not isinstance(reason, str):
        return ""
    matches = list(_PRODUCT_SECTION.finditer(reason))
    for index, match in enumerate(matches):
        if int(match[1]) == n:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(reason)
            return reason[match.end():end].strip().rstrip("；;").strip()
    return ""


def is_unscored_reason_1022(reason: str) -> bool:
    match = _UNSCORED.fullmatch(reason)
    return bool(match and all(re.search(r"\w", text) for text in match.groups()))


def _reason_sections(reason: str, product_count: int, field: str, *, ignored: set[int] | None = None) -> dict[int, str]:
    matches = list(_PRODUCT_SECTION.finditer(reason))
    ids = [int(m[1]) for m in matches]
    if not ids or ids != sorted(set(ids)) or any(not 1 <= n <= product_count for n in ids):
        _error(f"{field}须按实际产品顺序分段，不得重复或引用不存在产品")
    if reason[:matches[0].start()].strip():
        _error(f"{field}不得在产品分段前附加说明")
    parts = {n: product_reason_1022(reason, n) for n in ids}
    if any(not re.search(r"\w", text) for n, text in parts.items() if n not in (ignored or set())):
        _error(f"{field}产品说明不得为空白或纯标点")
    return parts


def _validate(data: dict, product_count: int) -> dict:
    model = output_model_1022(product_count)
    if not isinstance(data, dict):
        _error("顶层必须为单个JSON对象", "schema")
    supplied = set(data)
    unknown = supplied - model.model_fields.keys()
    if unknown:
        _error(f"1022输出含多余字段：{sorted(unknown)}", "schema")
    expected_order = [key for key in model.model_fields if key in supplied]
    if list(data) != expected_order:
        _error("输出字段顺序须为：全部输入状态；逐产品Gate理由→判定；逐维适用性→理由→各产品评分；冲突理由→结论")
    # Gate rejection explicitly authorizes ignoring downstream scores.
    normalized = copy.deepcopy(data)
    blocked_nos = set()
    for n in range(1, product_count + 1):
        core = [data.get(f"answer{n}_{g}_gate") for g in ("response", "safety")]
        blocked = any(value in ("fail", "unclear") for value in core) or data.get(f"answer{n}_input_status") == "failed"
        if blocked:
            blocked_nos.add(n)
            for dimension in DIMENSIONS_1022:
                normalized[f"answer{n}_{dimension}_score"] = None
    if len(blocked_nos) == product_count:
        for dimension in DIMENSIONS_1022:
            normalized[f"{dimension}_applicable"] = None
            normalized[f"{dimension}_reason"] = None
    elif blocked_nos:
        active_nos = set(range(1, product_count + 1)) - blocked_nos
        for dimension in DIMENSIONS_1022:
            key = f"{dimension}_reason"
            reason = normalized.get(key)
            if normalized.get(f"{dimension}_applicable") is True and isinstance(reason, str):
                parts = _reason_sections(reason, product_count, key, ignored=blocked_nos)
                if active_nos - parts.keys():
                    _error(f"{key}缺少未被Gate阻断产品的说明")
                # Ignore blocked judgments before length/score-claim checks.
                # This also excludes app-added Gate explanations when a normalized
                # result is validated again; normalization remains idempotent.
                normalized[key] = "；".join(f"产品{n}：{parts[n]}" for n in sorted(active_nos))
    if product_count - len(blocked_nos) < 2:
        normalized["content_conflict"] = None
        normalized["conflict_reason"] = None
    try:
        result = model.model_validate(normalized, strict=True).model_dump()
    except ValidationError as exc:
        _error(f"1022输出字段或类型不合法：{exc}", "schema")
    blocked = {n for n in range(1, product_count + 1) if gate_blocked_1022(result, n)}
    active = set(range(1, product_count + 1)) - blocked
    for key, text in result.items():
        if key.endswith("_reason") and isinstance(text, str):
            if not re.search(r"\w", text):
                _error(f"{key}不能仅包含空白或标点")
            if _SCORE_CLAIM.search(_ATTRIBUTED_QUOTE.sub("", text)):
                _error(f"{key}不得重复裁判数值评分；数值只写score字段")
    for n in range(1, product_count + 1):
        if result[f"answer{n}_input_status"] == "failed" and any(
            result[f"answer{n}_{gate}_gate"] != "unclear" for gate in ("response", "safety")
        ):
            _error(f"产品{n}输入失败时两个Gate必须为unclear")
    for dimension in DIMENSIONS_1022:
        applicable_key, reason_key = f"{dimension}_applicable", f"{dimension}_reason"
        if not active:
            result[applicable_key] = None
            result[reason_key] = "；".join(f"产品{n}：{_blocked_reason(result,n)}" for n in sorted(blocked))
            continue
        applicable, reason = result[applicable_key], result[reason_key]
        if applicable is None or applicable_key not in supplied:
            _error(f"{applicable_key}在存在可评分产品时必须明确填写")
        if reason is None or reason_key not in supplied:
            _error(f"{reason_key}在存在可评分产品时必须填写")
        if not applicable:
            if not reason.startswith("不适用：") or not re.search(r"\w", reason[len("不适用："):]):
                _error(f"{reason_key}须以“不适用：”解释查询级原因")
            parts = {}
        else:
            parts = _reason_sections(reason, product_count, reason_key)
            if active - parts.keys():
                _error(f"{reason_key}缺少未被Gate阻断产品的说明")
            for n in blocked:
                parts[n] = _blocked_reason(result, n)
            result[reason_key] = "；".join(f"产品{n}：{parts[n]}" for n in sorted(parts))
        for n in active:
            score_key = f"answer{n}_{dimension}_score"
            if score_key not in supplied:
                _error(f"{score_key}缺失；只有Gate阻断产品允许省略分数字段")
            score = result[score_key]
            if not applicable:
                if score is not None:
                    _error(f"{score_key}维度不适用时必须为null")
            elif score is None:
                if not is_unscored_reason_1022(parts[n]):
                    _error(f"{reason_key}产品{n}空分须用“未评分（具体原因）：具体说明”解释")
            elif parts[n].startswith("未评分"):
                _error(f"{reason_key}产品{n}有分数却声明未评分")
    if len(active) < 2:
        result["content_conflict"] = "unclear"
        result["conflict_reason"] = "通过两个Gate的产品不足两个，未进行产品间内容冲突判断。"
    elif result["content_conflict"] is None or result["conflict_reason"] is None:
        _error("存在两个以上可评产品时必须填写内容冲突判定及理由")
    return result


def validate_1022_output(data: dict, product_count: int) -> dict:
    return _validate(data, product_count)


def _read_json(raw: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复JSON字段：{key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"非法JSON常量：{value}")

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
        if not isinstance(data, dict):
            raise ValueError("顶层必须为单个JSON对象")
        return data
    except (TypeError, ValueError, RecursionError) as exc:
        raise OutputContractError(f"1022输出JSON语法不合法：{exc}", "json_syntax") from exc


def parse_1022_dual_output(raw: str) -> dict:
    return _validate(_read_json(raw), 2)


def parse_1022_triple_output(raw: str) -> dict:
    return _validate(_read_json(raw), 3)


def parse_1022_output(raw: str, product_count: int) -> dict:
    output_model_1022(product_count)
    return parse_1022_dual_output(raw) if product_count == 2 else parse_1022_triple_output(raw)


def output_schema_1022(product_count: int) -> dict:
    schema = copy.deepcopy(output_model_1022(product_count).model_json_schema())
    # Provider strict mode requires all properties. Blocked values may be null.
    # Local parsing additionally permits the gate-shortcut omissions.
    schema["required"] = list(schema["properties"])
    for spec in schema["properties"].values():
        spec.pop("default", None)
    return schema


def response_format_1022(mode: str, product_count: int) -> dict | None:
    if mode == "off":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {
            "name": f"compare_1022_{product_count}_products", "strict": True,
            "schema": output_schema_1022(product_count),
        }}
    raise ValueError(f"未知结构化输出模式：{mode}")


def output_field_instructions_1022(product_count: int) -> str:
    def describe(spec):
        if "anyOf" in spec:
            return " 或 ".join(describe(part) for part in spec["anyOf"])
        if "enum" in spec:
            return "字符串，选择 " + " / ".join(spec["enum"])
        return {"integer": "1—5整数", "boolean": "布尔值true/false", "null": "JSON null",
                "string": f"字符串，最多{spec.get('maxLength', 600)}字符"}.get(spec.get("type"), "")
    return "\n".join(f"- {key}：{describe(spec)}" for key, spec in output_schema_1022(product_count)["properties"].items())


def example_1022_output(product_count: int = 2, *, evidence_mode: str = "video_frames") -> dict:
    model = output_model_1022(product_count)
    data = {}
    for n in range(1, product_count + 1):
        data[f"answer{n}_response_gate_reason"] = "最终回答完整可读，没有响应或展示故障。"
        data[f"answer{n}_safety_gate_reason"] = "正常问候，未见安全问题。"
    for dimension in DIMENSIONS_1022:
        applicable = dimension in ("understanding", "intuitive_efficiency")
        location = "第1/1块" if evidence_mode == "long_screenshot" else "第1帧"
        data[f"{dimension}_reason"] = "；".join(
            f"产品{n}：依据：{location}显示简短完整问候，核心文字适量加粗。判定：回应问候目标，呈现清楚，无实质问题。影响：用户可直接理解。"
            for n in range(1, product_count + 1)
        ) if applicable else "不适用：当前只是问候，没有触发该维度的需求或行为。"
    data["conflict_reason"] = "问候没有可比较的核心事实主张。"
    for n in range(1, product_count + 1):
        data[f"answer{n}_input_status"] = "complete"
        data[f"answer{n}_response_gate"] = "pass"
        data[f"answer{n}_safety_gate"] = "pass"
    for dimension in DIMENSIONS_1022:
        applicable = dimension in ("understanding", "intuitive_efficiency")
        data[f"{dimension}_applicable"] = applicable
        for n in range(1, product_count + 1):
            data[f"answer{n}_{dimension}_score"] = 5 if applicable else None
    data["content_conflict"] = "no"
    assert set(data) == set(model.model_fields)
    return _validate({key: data[key] for key in model.model_fields}, product_count)
