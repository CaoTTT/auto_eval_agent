"""Frozen judge presentation order, independent of source product identity."""
from __future__ import annotations


def validate_product_order(mode: str, items: list[dict], options: dict) -> bool:
    reverse = options.get("reverse_product_order", False)
    if type(reverse) is not bool:
        raise ValueError("reverse_product_order 必须是布尔值 true 或 false")
    if not reverse:
        return False
    if mode != "compare":
        raise ValueError("产品位置互换仅支持双产品对比评估")
    for item in items:
        has_third = item.get("product_count") == 3 or any(
            item.get(field) not in (None, "", [])
            for field in ("video3", "screenshot3", "context3", "answer3", "frames3")
        )
        if has_third:
            raise ValueError("三产品评估不支持产品位置互换，请关闭互换后重试")
    return True


def product_order_metadata(reverse: bool, product_count: int = 2) -> dict:
    if reverse:
        order = [2, 1]
        note = (
            "已互换：裁判位置1=原产品2，裁判位置2=原产品1；"
            "产品数据、评分和胜负均按原产品身份展示。裁判原始输出保留裁判位置编号。"
        )
    else:
        order = list(range(1, product_count + 1))
        note = "未互换：" + "，".join(f"裁判位置{n}=原产品{n}" for n in order)
    return {
        "reverse_product_order": reverse,
        "judge_product_order": order,
        "judge_product_order_note": note,
    }
