import json

import pytest

from auto_eval.judges.compare_protocols import (
    V02_PRIOR_KNOWLEDGE_COMPARE_PROTOCOL_ID,
    resolve_compare_protocol,
)
from auto_eval.web.parse_input import normalize_compare_evidence, parse_jsonl, parse_prior_bool


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("否", False),
        ("False", False),
        ("false", False),
        ("FALSE", False),
        (False, False),
        (0, False),
        ("0", False),
        ("是", True),
        ("True", True),
        ("true", True),
        ("TRUE", True),
        (True, True),
        (1, True),
        ("1", True),
    ],
)
def test_parse_prior_bool(value, expected):
    assert parse_prior_bool(value, "include_prior_knowledge") is expected


@pytest.mark.parametrize("value", ["maybe", 2, -1, 1.0, [], {}])
def test_parse_prior_bool_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="include_prior_knowledge"):
        parse_prior_bool(value, "include_prior_knowledge")


def _compare_item(**extra):
    return {
        "id": "case-1",
        "query": "比较两个产品",
        "product_count": 2,
        "context1": "产品1条件",
        "context2": "产品2条件",
        "screenshot1": "one.png",
        "screenshot2": "two.png",
        **extra,
    }


def test_normalize_compare_evidence_parses_prior_knowledge_flag():
    normalized = normalize_compare_evidence(_compare_item(include_prior_knowledge="TRUE"))

    assert normalized["evidence_mode"] == "long_screenshot"
    assert normalized["include_prior_knowledge"] is True


def test_parse_jsonl_parses_prior_knowledge_flag():
    items, errors = parse_jsonl(
        json.dumps(_compare_item(include_prior_knowledge="否"), ensure_ascii=False),
        "compare",
    )

    assert errors == []
    assert items[0]["include_prior_knowledge"] is False


def test_parse_jsonl_reports_invalid_prior_knowledge_flag():
    items, errors = parse_jsonl(
        json.dumps(_compare_item(include_prior_knowledge="maybe"), ensure_ascii=False),
        "compare",
    )

    assert items == []
    assert len(errors) == 1
    assert "include_prior_knowledge" in errors[0]


def test_prior_knowledge_rules_render_only_when_enabled():
    protocol = resolve_compare_protocol(V02_PRIOR_KNOWLEDGE_COMPARE_PROTOCOL_ID)
    enabled = protocol.system_template.render(
        persona="普通用户",
        product_count=2,
        evidence_mode="long_screenshot",
        include_prior_knowledge=True,
    )
    disabled = protocol.system_template.render(
        persona="普通用户",
        product_count=2,
        evidence_mode="long_screenshot",
        include_prior_knowledge=False,
    )

    assert "必须检查子项的原则细节" in enabled
    assert "必须检查子项的原则细节" not in disabled


def test_prior_knowledge_protocol_is_selectable():
    protocol = resolve_compare_protocol(V02_PRIOR_KNOWLEDGE_COMPARE_PROTOCOL_ID)

    assert protocol.display == "垂域先验知识测试"
    assert protocol.selectable is True
