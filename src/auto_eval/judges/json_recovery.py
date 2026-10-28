"""Conservative, local-only recovery of one complete JSON object.

Only transport wrappers and syntactic punctuation are changed. This module does
not infer missing values, complete truncated output, or choose between competing
interpretations of a model's answer. Callers retain the original text for audit.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any


class JsonRecoveryError(ValueError):
    """The response cannot be recovered without guessing its contents."""


@dataclass(frozen=True)
class JsonRecoveryResult:
    data: dict[str, Any]
    steps: tuple[str, ...] = ()


_MAX_CHARACTERS = 2_000_000
_MAX_REPAIRS = 128
_MAX_NESTING = 128
_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*", re.IGNORECASE)
_PREFIXES = (
    "以下是JSON结果：", "以下是 JSON 结果：", "以下是JSON：", "以下是 JSON：",
    "以下是评测结果：", "JSON结果：", "JSON：", "JSON:",
    "Here is the JSON:", "Here is the JSON result:",
)
_SUFFIXES = ("以上为JSON结果。", "以上为评测结果。", "End of JSON.")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsonRecoveryError(f"重复JSON字段：{key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise JsonRecoveryError(f"JSON不允许非有限数值：{value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise JsonRecoveryError("JSON数值超出有限范围")
    return number


def _loads(text: str, *, strict: bool = False) -> Any:
    return json.loads(text, strict=strict, object_pairs_hook=_pairs,
                      parse_constant=_constant, parse_float=_float)


def _unwrap(text: str, steps: list[str]) -> str:
    value = text.strip()
    if value.startswith("\ufeff"):
        value = value[1:].strip()
        steps.append("strip_bom")
    # A completed reasoning prefix may precede a final answer. An unfinished
    # block or a reasoning-only response must never become a fabricated answer.
    for _ in range(8):
        if not value:
            raise JsonRecoveryError("模型没有返回JSON对象")
        reasoning = re.match(r"<(think|analysis)>", value, re.IGNORECASE)
        if reasoning:
            closing = re.search(rf"</{reasoning[1]}>", value[reasoning.end():], re.IGNORECASE)
            if closing is None:
                raise JsonRecoveryError("模型只返回了未完成的思考内容")
            end = reasoning.end() + closing.end()
            value = value[end:].strip()
            steps.append("strip_completed_reasoning_prefix")
            continue
        prefix = next((p for p in _PREFIXES if value.casefold().startswith(p.casefold())), None)
        if prefix is not None:
            value = value[len(prefix):].strip()
            steps.append("strip_json_prefix")
            continue
        suffix = next((s for s in _SUFFIXES if value.casefold().endswith(s.casefold())), None)
        if suffix is not None:
            value = value[:-len(suffix)].strip()
            steps.append("strip_json_suffix")
            continue
        if value.startswith("```"):
            match = _FENCE.fullmatch(value)
            if match is None:
                raise JsonRecoveryError("JSON代码围栏不完整或围栏外还有内容")
            value = match[1].strip()
            steps.append("strip_json_fence")
            continue
        return value
    raise JsonRecoveryError("JSON包装层级过多")


def _previous_nonspace(text: str, position: int) -> int:
    position -= 1
    while position >= 0 and text[position].isspace():
        position -= 1
    return position


def _prefix_state(text: str, end: int) -> tuple[list[str], int | None, int | None]:
    """Describe the prefix accepted by JSONDecoder, without correcting it."""
    stack: list[str] = []
    in_string = escaped = False
    start = last_start = last_end = None
    for index, char in enumerate(text[:end]):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                last_start, last_end = start, index
        elif char == '"':
            in_string, start = True, index
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack:
                stack.pop()
    return stack, last_start, last_end


def _is_object_field(text: str, position: int) -> bool:
    if position >= len(text) or text[position] != '"':
        return False
    try:
        key, end = json.JSONDecoder(strict=False).raw_decode(text, position)
    except json.JSONDecodeError:
        return False
    while end < len(text) and text[end].isspace():
        end += 1
    return isinstance(key, str) and end < len(text) and text[end] == ":"


def _check_depth(text: str) -> None:
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > _MAX_NESTING:
                raise JsonRecoveryError("JSON嵌套过深，未尝试格式恢复")
        elif char in "}]":
            depth -= 1


def recover_json_object(text: str) -> JsonRecoveryResult:
    """Return one object and a trace of lossless local format recoveries.

    Supported: BOM, completed JSON/reasoning wrappers, literal string control
    characters, trailing commas, and a missing comma before an unambiguous
    object member. Plain-text paired quotes inside a value can be escaped when
    JSONDecoder identifies their locations; JSON-looking quoted fragments and
    ambiguous string/array boundaries are rejected. No closing delimiter,
    scalar, field, value, score, or explanation is ever supplied.
    """
    if not isinstance(text, str):
        raise JsonRecoveryError("模型输出必须是文本")
    if len(text) > _MAX_CHARACTERS:
        raise JsonRecoveryError("JSON响应过长，未尝试格式恢复")
    steps: list[str] = []
    candidate = _unwrap(text, steps)
    if not candidate.startswith("{"):
        raise JsonRecoveryError("顶层必须为单个JSON对象")
    quote_repairs: dict[int, int] = {}
    for _ in range(_MAX_REPAIRS + 1):
        _check_depth(candidate)
        try:
            data = _loads(candidate)
        except JsonRecoveryError:
            raise
        except (RecursionError, ValueError) as exc:
            if not isinstance(exc, json.JSONDecodeError):
                raise JsonRecoveryError(f"JSON无法安全读取：{exc}") from exc
            previous = _previous_nonspace(candidate, exc.pos)
            # Delete only a comma that the decoder identifies as trailing.
            # Python 3.14 points at the comma itself; older decoders point at
            # the closing delimiter with an "Expecting ..." diagnostic.
            if (exc.msg in ("Illegal trailing comma before end of object", "Illegal trailing comma before end of array")
                    and exc.pos < len(candidate) and candidate[exc.pos] == ","):
                candidate = candidate[:exc.pos] + candidate[exc.pos + 1:]
                steps.append("remove_trailing_comma")
                continue
            if (exc.pos < len(candidate) and candidate[exc.pos] in "}]"
                    and previous >= 0 and candidate[previous] == ","
                    and exc.msg in ("Expecting property name enclosed in double quotes", "Expecting value")):
                candidate = candidate[:previous] + candidate[previous + 1:]
                steps.append("remove_trailing_comma")
                continue
            if exc.msg == "Expecting ',' delimiter":
                stack, string_start, string_end = _prefix_state(candidate, exc.pos)
                # A quoted member name followed by ':' is a concrete object
                # boundary. Never infer separators between array strings or
                # concatenate/split numeric literals.
                if stack and stack[-1] == "{" and _is_object_field(candidate, exc.pos):
                    candidate = candidate[:exc.pos] + "," + candidate[exc.pos:]
                    steps.append("insert_missing_object_comma")
                    continue
                # Only an immediately preceding string terminator can be a
                # mistyped interior quote. Structural punctuation after it is
                # ambiguous (notably quoted JSON embedded in a reason).
                adjacent_plain_quotes = (
                    string_start is not None and string_end == previous
                    and exc.pos == previous + 1 and exc.pos < len(candidate)
                    and candidate[exc.pos] == '"' and stack and stack[-1] == "{"
                    and quote_repairs.get(string_start, 0) % 2 == 1
                )
                if (string_start is not None and string_end == previous
                        and exc.pos < len(candidate)
                        and (candidate[exc.pos] not in '\"{}[]:,' or adjacent_plain_quotes)):
                    candidate = candidate[:previous] + "\\" + candidate[previous:]
                    quote_repairs[string_start] = quote_repairs.get(string_start, 0) + 1
                    steps.append("escape_internal_quote")
                    continue
            raise JsonRecoveryError(f"JSON语法无法无歧义恢复：{exc}") from exc
        if not isinstance(data, dict):
            raise JsonRecoveryError("顶层必须为单个JSON对象")
        # An unmatched interior quote could instead have ended the field. Do
        # not silently absorb a following comma/member into a string value.
        if any(count % 2 for count in quote_repairs.values()):
            raise JsonRecoveryError("字符串内部引号边界不唯一，未采用格式恢复")
        try:
            _loads(candidate, strict=True)
        except json.JSONDecodeError:
            steps.append("allow_literal_string_controls")
        return JsonRecoveryResult(data=data, steps=tuple(steps))
    raise JsonRecoveryError("JSON格式问题过多，未继续恢复")
