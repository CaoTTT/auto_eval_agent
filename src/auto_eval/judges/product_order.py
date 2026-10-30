"""Translate judge slots without changing stored product identities or raw traces."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from ..compare_input import normalize_product_contexts
from .compare_1022_contract import (
    DIMENSIONS_1022, OUTPUT_CONTRACT_1022,
    _join_reason_sections, _reason_matches, _section_parts,
)


_INPUT_FIELD = re.compile(
    r"^(answer|context|frames|screenshot|screenshot_meta|screenshot_scope|video)([12])(_path)?$"
)
_RESULT_KEY = re.compile(r"^answer([12])(?=_|$)")
_PRODUCT_REFERENCE = re.compile(
    r"(?P<label>产品\s*|(?<![A-Za-z0-9_])(?:answer|product)\s*)(?P<number>[12１２])(?![0-9０-９])",
    re.IGNORECASE,
)
_ASSET_REFERENCE = re.compile(r"\bP([12])(?=-T\d)")
_RAW_FIELDS = {
    "raw_model_output", "model_raw_output", "repaired_model_output", "raw_output", "repair_output",
    "raw_key_pairs", "original_raw_key_pairs", "locally_repaired_key_pairs",
    "repair_key_pairs", "repair_locally_repaired_key_pairs", "local_format_recovery_steps",
}


def _other(number: str) -> str:
    return {"1": "2", "2": "1", "１": "２", "２": "１"}[number]


def _swap_input_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        (_INPUT_FIELD.sub(lambda match: match[1] + _other(match[2]) + (match[3] or ""), key)
         if isinstance(key, str) else key): value
        for key, value in record.items()
    }


def _slot_screenshot_meta(meta: dict[str, Any], slot: int) -> dict[str, Any]:
    """Relabel derived asset IDs while retaining original bytes, paths and hashes."""
    result = deepcopy(meta)
    if isinstance(result.get("asset_id"), str):
        result["asset_id"] = _ASSET_REFERENCE.sub(f"P{slot}", result["asset_id"], count=1)
    if "product_no" in result:
        result["product_no"] = slot
    return result


def swap_judge_inputs(arguments: dict[str, Any], *, normalize_legacy_context: bool = False) -> dict[str, Any]:
    """Return a slot-ordered request; caller-owned source records stay untouched."""
    result = _swap_input_fields(arguments)
    if arguments.get("screenshot_metas") is not None:
        result["screenshot_metas"] = [
            _slot_screenshot_meta(meta, slot)
            for slot, meta in enumerate(reversed(arguments["screenshot_metas"]), 1)
        ]
    if arguments.get("trusted_collection_metadata") is not None:
        result["trusted_collection_metadata"] = {
            _RESULT_KEY.sub(lambda match: "answer" + _other(match[1]), key): deepcopy(value)
            for key, value in arguments["trusted_collection_metadata"].items()
        }
    bundle = arguments.get("conversation_input")
    if bundle is not None:
        ordered = {**bundle, "diagnostics": deepcopy(bundle.get("diagnostics", {}))}
        ordered["turns"] = []
        for turn in bundle["turns"]:
            if normalize_legacy_context:
                turn = {**turn, **normalize_product_contexts(turn, 2)}
                turn.pop("context", None)
            swapped = _swap_input_fields(turn)
            for slot in (1, 2):
                key = f"screenshot_meta{slot}"
                if key in swapped:
                    swapped[key] = _slot_screenshot_meta(swapped[key], slot)
            ordered["turns"].append(swapped)
        result["conversation_input"] = ordered
    return result


def _is_explanation(key: str) -> bool:
    return key in {"evidence", "rationale", "reason", "diagnostics"} or key.endswith(
        ("_reason", "_reasons", "_evidence", "_primary_issue", "_diagnostics")
    )


def remap_judge_result(result: dict[str, Any]) -> dict[str, Any]:
    """Restore slot-owned scores and explanations to original product identities."""
    def convert(value: Any, *, explain: bool = False, field: str = "") -> Any:
        if isinstance(value, dict):
            converted = {}
            for key, item in value.items():
                if key in _RAW_FIELDS:
                    converted[key] = item
                    continue
                mapped = _RESULT_KEY.sub(lambda match: "answer" + _other(match[1]), key)
                if key == "trusted_collection_metadata":
                    converted[mapped] = {
                        _RESULT_KEY.sub(lambda match: "answer" + _other(match[1]), product): record
                        for product, record in item.items()
                    }
                    continue
                converted[mapped] = convert(item, explain=explain or _is_explanation(key), field=key)
            return converted
        if isinstance(value, (list, tuple)):
            items = [convert(item, explain=explain, field=field) for item in value]
            # Equal ranks are sets; keep original product numbering within a tie.
            if field.endswith("_rank_groups"):
                items = [sorted(group) if isinstance(group, list) else group for group in items]
            return tuple(items) if isinstance(value, tuple) else items
        if isinstance(value, str):
            product_token_field = field.endswith(("_winner", "_rank_groups")) or field in {
                "overall_ranking", "relevance", "safety", "content_quality", "need_closure", "personalization",
            }
            if product_token_field and value in {"answer1", "answer2", "product1", "product2"}:
                return value[:-1] + _other(value[-1])
            if explain:
                value = _PRODUCT_REFERENCE.sub(
                    lambda match: match["label"] + _other(match["number"]), value)
                return _ASSET_REFERENCE.sub(lambda match: "P" + _other(match[1]), value)
        return value

    mapped = convert(result)
    if result.get("output_contract") == OUTPUT_CONTRACT_1022:
        # The compact contract's consumers require product sections in numeric
        # order, including when extracting an explicit NA explanation. Reuse
        # its quote-aware parser so quoted source labels cannot become headings.
        for dimension in DIMENSIONS_1022:
            key = f"{dimension}_reason"
            reason = mapped.get(key)
            if not isinstance(reason, str):
                continue
            matches = _reason_matches(reason)
            if matches:
                prefix = reason[:matches[0].start("heading")]
                mapped[key] = _join_reason_sections(prefix, _section_parts(reason, matches))
    return mapped
