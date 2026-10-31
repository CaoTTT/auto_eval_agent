"""Recover two explicit chat-output format mistakes without changing judgments.

The strict parser remains authoritative. This adapter only escapes paired plain
text quotes inside JSON strings and renames exact, whitelisted score aliases;
it never supplies missing fields or values or resolves competing judgments.
"""
from __future__ import annotations

from .chitchat_flat_schema import (
    CHITCHAT_FLAT_DIMENSIONS, expected_output_keys, validate_output,
)
from .json_recovery import recover_json_object


def recover_chitchat_flat_output(raw: str, product_count: int) -> dict:
    """Return a strictly validated observation and lossless recovery audit.

    ``raw_key_pairs`` represents only a syntactically valid original object.
    Recovered pairs are recorded separately and only after complete validation.
    The caller retains ``raw`` verbatim as the original model response.
    """
    expected_output_keys(product_count)  # Validate the strict active-product count.
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("闲聊格式恢复要求非空JSON文本")
    # JSON allows only these four whitespace characters. The shared recovery
    # helper also understands wrappers, but none are authorized by this adapter.
    candidate = raw.strip(" \t\r\n")
    if not candidate.startswith("{") or not candidate.endswith("}"):
        raise ValueError("闲聊格式恢复仅接受完整的单个JSON对象，不补全或移除包装")

    recovered = recover_json_object(raw)
    unsupported = sorted(set(recovered.steps) - {"escape_internal_quote"})
    if unsupported:
        raise ValueError("闲聊格式恢复不允许这些变换：" + ", ".join(unsupported))

    steps = list(recovered.steps)
    raw_key_pairs = [] if steps else [[key, value] for key, value in recovered.data.items()]
    aliases = {
        f"{dimension}_answer{product}_score": f"answer{product}_{dimension}_score"
        for dimension in CHITCHAT_FLAT_DIMENSIONS
        for product in range(1, product_count + 1)
    }
    canonical = {}
    for key, value in recovered.data.items():
        mapped = aliases.get(key, key)
        # Even equal-valued aliases are conflicting source fields. Never choose
        # between them or silently drop an explicitly returned judgment.
        if mapped in canonical:
            raise ValueError(f"闲聊评分字段别名冲突：{key} -> {mapped}")
        canonical[mapped] = value
        if mapped != key:
            steps.append(f"rename_score_alias: {key} -> {mapped}")

    observation = validate_output(canonical, product_count)
    return {
        "observation": observation,
        "raw_key_pairs": raw_key_pairs,
        "locally_repaired_key_pairs": (
            [[key, value] for key, value in observation.model_dump().items()] if steps else []
        ),
        "local_format_recovery_steps": steps,
    }
