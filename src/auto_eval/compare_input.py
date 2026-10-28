"""Normalize required per-product context without sharing one product's data."""
from __future__ import annotations

from collections.abc import Mapping


CONTEXT_ERROR_CODE = "compare_context_missing"


class ProductContextError(ValueError):
    """A participating product has no usable context in the imported sample."""


def normalize_product_contexts(obj: Mapping, product_count: int) -> dict[str, str]:
    """Require each contextN; only an absent context1 may use legacy context.

    A present but empty/null/invalid context1 is an input error, so an old alias
    cannot silently replace an explicitly supplied field. Other products never
    borrow the first product's context.
    """
    if type(product_count) is not int or product_count not in (2, 3):
        raise ValueError("product_count 只能是整数 2 或 3")
    contexts: dict[str, str] = {}
    invalid: list[str] = []
    for number in range(1, product_count + 1):
        field = f"context{number}"
        source = "context" if number == 1 and field not in obj else field
        value = obj.get(source)
        if not isinstance(value, str) or not value.strip():
            label = f"{field}（兼容字段 context）" if source == "context" else field
            invalid.append(label)
        else:
            contexts[field] = value.strip()
    if invalid:
        raise ProductContextError(
            f"{CONTEXT_ERROR_CODE}: 缺少或无效的产品背景：{'、'.join(invalid)}；"
            "每个参评产品必须提供非空字符串背景，补齐后才能评估"
        )
    return contexts
