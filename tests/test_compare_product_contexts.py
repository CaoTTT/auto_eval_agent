"""Required product backgrounds are normalized before any model evaluation."""
import json

import pytest
from fastapi import HTTPException

from auto_eval.compare_input import ProductContextError, normalize_product_contexts
from auto_eval.config import AppConfig
from auto_eval.web import server
from auto_eval.web.parse_input import normalize_compare_evidence, parse_csv, parse_jsonl


def case(count=2, **extra):
    result = {"id": "context-case", "query": "问题", "product_count": count}
    for number in range(1, count + 1):
        result[f"screenshot{number}"] = f"product-{number}.png"
        result[f"context{number}"] = f"产品{number}的条件"
    return {**result, **extra}


@pytest.mark.parametrize("count", [2, 3])
def test_contexts_are_required_for_actual_products_only(count):
    data = case(count)
    assert normalize_product_contexts(data, count) == {
        f"context{number}": f"产品{number}的条件" for number in range(1, count + 1)
    }
    data.pop(f"context{count}")
    with pytest.raises(ProductContextError, match=f"context{count}"):
        normalize_product_contexts(data, count)


@pytest.mark.parametrize("value", ["", "  \n", None, 4, False, {}, []])
@pytest.mark.parametrize("number", [1, 2, 3])
def test_explicit_invalid_context_does_not_borrow_any_other_product(value, number):
    data = case(3, context="有效旧字段")
    data[f"context{number}"] = value
    with pytest.raises(ProductContextError, match=f"context{number}"):
        normalize_product_contexts(data, 3)


def test_legacy_context_is_only_a_fallback_for_absent_context1():
    data = case(context="  产品1旧背景  ")
    assert normalize_product_contexts(data, 2)["context1"] == "产品1的条件"
    data.pop("context1")
    assert normalize_product_contexts(data, 2) == {
        "context1": "产品1旧背景", "context2": "产品2的条件"
    }
    data.pop("context2")
    with pytest.raises(ProductContextError, match="context2"):
        normalize_product_contexts(data, 2)


@pytest.mark.parametrize("legacy", [False, True])
def test_json_and_direct_api_normalize_identically_and_preserve_original(legacy):
    data = case(3, context="旧背景", context1="  专属产品1  ")
    if legacy:
        data.pop("context1")
    parsed, errors = parse_jsonl(json.dumps(data), "compare")
    assert not errors
    assert parsed[0]["source_data"] == data
    assert parsed[0]["context"] == data["context"]
    assert parsed[0]["context1"] == ("旧背景" if legacy else "专属产品1")
    normalized = normalize_compare_evidence(data)
    assert normalized["context"] == data["context"]
    request = server.EvalReq(mode="compare", items=[data.copy()])
    server._validate_eval_request(request, AppConfig(judges=[]))
    assert all(request.items[0]["source_data"][key] == value for key, value in data.items())
    for number in (1, 2, 3):
        field = f"context{number}"
        assert request.items[0][field] == parsed[0][field] == normalized[field]


def test_parse_blocks_mixed_valid_and_context_invalid_cases_without_silent_skip():
    good, bad = case(id="good"), case(id="bad", context2="")
    response = server.api_parse(server.ParseReq(mode="compare", jsonl=json.dumps([good, bad])))
    assert response["blocked"] is True
    assert [item["id"] for item in response["items"]] == ["good"]
    assert "第 2 条" in response["errors"][0]
    assert "Case bad" in response["errors"][0] and "context2" in response["errors"][0]
    valid = server.api_parse(server.ParseReq(mode="compare", jsonl=json.dumps(good)))
    assert valid["blocked"] is False


@pytest.mark.parametrize("other_problem", ["query", "screenshot2", "session_id"])
def test_other_invalid_fields_cannot_hide_missing_context(other_problem):
    bad = case(id="bad", context2="")
    if other_problem == "session_id":
        bad.update(turn_index=1, screenshot_scope="current_turn")
    else:
        bad.pop(other_problem)
    response = server.api_parse(server.ParseReq(mode="compare", jsonl=json.dumps([case(id="good"), bad])))
    assert response["blocked"] is True
    assert any("context2" in error and "Case bad" in error for error in response["errors"])


def test_api_rejects_all_missing_product_contexts_with_case_identifier():
    data = case(3)
    for number in (1, 2, 3):
        data.pop(f"context{number}")
    request = server.EvalReq(mode="compare", items=[case(id="valid"), data])
    with pytest.raises(HTTPException) as caught:
        server._validate_eval_request(request, AppConfig(judges=[]))
    assert caught.value.status_code == 422
    assert "第2条（Case context-case）" in caught.value.detail
    assert all(f"context{number}" in caught.value.detail for number in (1, 2, 3))


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["api_compare_preflight", "api_eval"])
async def test_endpoint_rejects_context_before_creating_judge_runtime(monkeypatch, endpoint):
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[]))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid context must be rejected before creating model runtime")
    monkeypatch.setattr(server, "_new_judge_runtime", forbidden)
    request = server.EvalReq(mode="compare", items=[case(context2="")])
    with pytest.raises(HTTPException) as caught:
        await getattr(server, endpoint)(request)
    assert caught.value.status_code == 422 and "context2" in caught.value.detail


def test_every_turn_requires_its_own_product_contexts():
    rows = [case(id=f"turn-{turn}", session_id="session", turn_index=turn,
                 screenshot_scope="current_turn") for turn in (1, 2)]
    rows[1].pop("context2")
    response = server.api_parse(server.ParseReq(mode="compare", jsonl=json.dumps(rows)))
    assert response["blocked"] is True
    assert response["items"] == []
    assert any("context2" in error and "turn-2" in error for error in response["errors"])


def test_rich_content_keeps_optional_shared_context_and_existing_csv_behavior():
    data = {"query": "问题", "video_path": "one.mp4", "context": "共享背景"}
    items, errors = parse_jsonl(json.dumps(data), "rich_content")
    assert not errors and items[0]["context"] == "共享背景"
    response = server.api_parse(server.ParseReq(mode="rich_content", jsonl=json.dumps(data)))
    assert response["blocked"] is False
    assert parse_csv("query,context,context2\n问题,产品1,产品2", "compare")[0] == []
