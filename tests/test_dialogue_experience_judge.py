"""Real judge adapter acceptance using one synthetic semantic model response."""
import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest

from auto_eval import query_images
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.dialogue_experience_schema import parse_output
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from test_knowledge_qa_platform import Client, FIXED_TIME, args_for_images
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_vqa_input import setup_images as image_setup


PROFILE = "dialogue_experience_compare@1.0"


def payload(count=2):
    return {
        f"answer{product}": {
            "score": 6 - product,
            "reason": f"【类型：任务型；目标：完成本轮任务；主查：需求和执行；证据状态：充分】产品{product}截图中部明确满足核心目标，按实际交付判档。",
            "suggestion": f"产品{product}减少重复反馈。",
            "domain": f"领域{product}",
        }
        for product in range(1, count + 1)
    }


def input_json(user):
    value, _tail = json.JSONDecoder().raw_decode(user[user.index("{"):])
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
@pytest.mark.parametrize("with_query_image", [False, True])
async def test_one_response_preserves_original_fields_and_sends_real_images(
    image_setup, count, mode, with_query_image,
):
    root, profile, paths = image_setup
    prepared = query_images.prepare_query_images(
        {"query": "QUERY_SENTINEL", "query_images": [paths[0]] if with_query_image else []},
        session_name="dialogue-experience-judge", cfg=profile.query_images,
    )
    expected = payload(count)
    client = Client(expected)
    arguments = args_for_images(root, profile, paths, count, mode)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **arguments, query_image_meta=prepared["query_image_meta"],
    )
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert result["model_product_results"] == expected
    assert json.loads(result["raw_model_output"]) == expected
    assert result["input_manifest_sha256"]
    assert result["product_count"] == count and result["evaluation_profile"] == PROFILE
    assert "has_conflict" not in result
    assert not any(key.endswith("_gate") for key in result)
    assert not any(key in result for key in ("answer1", "answer2", "answer3"))
    for product in range(1, count + 1):
        value = expected[f"answer{product}"]
        assert set(value) == {"score", "reason", "suggestion", "domain"}
        for original, projected in (("score", "overall_score"), ("reason", "overall_reason"),
                                    ("suggestion", "suggestion"), ("domain", "domain")):
            assert result[f"answer{product}_{projected}"] == value[original]
    assert any(key.startswith("answer3_") for key in result) is (count == 3)

    system, user, sent = client.calls[0]
    assert sent["require_complete"] is True
    received = input_json(user)
    assert received["query"] == "QUERY_SENTINEL" and received["product_count"] == count
    assert "测试时间" in system and "仅是评估调用时间" in system
    assert result["test_time_policy"] == "provided_evidence_only" and result["evaluation_run_at"]
    assert "evaluation_datetime" not in result
    for product, record in enumerate(received["products"], 1):
        assert record["product_no"] == product
        assert record["platform_context"] == arguments[f"context{product}"]
        assert record["answer"] == arguments[f"answer{product}"]
        assert record["context"] == []
        for missing in ("current_time_beijing", "user_info", "user_md", "knowledge", "resultDataList",
                        "intent", "slots", "intent_explain", "slot_explain"):
            assert record[missing] == "", missing
        assert all(f"P{other}_BACKGROUND" not in record["platform_context"]
                   for other in range(1, count + 1) if other != product)
    assert received["evaluation_datetime"] == FIXED_TIME.isoformat(timespec="seconds")
    parts = sent["content_parts"]
    images = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
    assert len(images) == count + int(with_query_image)
    assert len(sent["image_metadata"]) == len(images)
    labels = "\n".join(part.get("text", "") for part in parts)
    for product in range(1, count + 1):
        assert f"产品{product}" in labels
    if with_query_image:
        assert "共享用户题目，不是产品回答" in labels
        assert base64.b64decode(images[0].split(",", 1)[1]) == Path(paths[0]).read_bytes()
    offset = int(with_query_image)
    if mode == "long_screenshot":
        for product in range(1, count + 1):
            assert base64.b64decode(images[offset + product - 1].split(",", 1)[1]) == Path(paths[product]).read_bytes()
    else:
        assert all("image/jpeg" in url for url in images[offset:])


def malformed_outputs():
    valid = payload()
    cases = []
    for score in (0, 6, 1.5, True, "5", None):
        data = deepcopy(valid)
        data["answer1"]["score"] = score
        cases.append(json.dumps(data, ensure_ascii=False))
    for field in ("score", "reason", "suggestion", "domain"):
        data = deepcopy(valid)
        data["answer2"].pop(field)
        cases.append(json.dumps(data, ensure_ascii=False))
    for field in ("reason", "suggestion", "domain"):
        data = deepcopy(valid)
        data["answer1"][field] = "  "
        cases.append(json.dumps(data, ensure_ascii=False))
    nested_extra = deepcopy(valid)
    nested_extra["answer1"]["response_gate"] = "pass"
    cases.extend(json.dumps(data, ensure_ascii=False) for data in (
        nested_extra, {**valid, "answer1_response_gate": "pass"},
        {"answer1": valid["answer1"]}, {**valid, "answer3": valid["answer1"]},
    ))
    raw = json.dumps(valid, ensure_ascii=False)
    cases.extend([
        raw.replace('"score": 5', '"score": 5, "score": 1', 1),
        raw[:-1] + ', "answer1": ' + json.dumps(valid["answer1"], ensure_ascii=False) + '}',
        raw[:-1], "[]", "", "```json\n" + raw + "\n```",
    ])
    return cases


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", malformed_outputs())
async def test_invalid_contract_rejects_without_semantic_repair(image_setup, raw):
    root, profile, paths = image_setup
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile, PROFILE).evaluate(
            **args_for_images(root, profile, paths),
        )
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert caught.value.raw_output == raw and caught.value.raw_model_output == raw
    assert caught.value.structural_diagnostics and caught.value.input_manifest_sha256


@pytest.mark.asyncio
async def test_three_product_request_does_not_accept_two_product_response(image_setup):
    root, profile, paths = image_setup
    client = Client(payload(2))
    with pytest.raises(JudgeOutputParseError):
        await VisualCompareJudge(client, profile, PROFILE).evaluate(
            **args_for_images(root, profile, paths, 3),
        )
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
async def test_three_product_null_block_is_audited_strict_failure(image_setup):
    root, profile, paths = image_setup
    expected = payload(3)
    expected["answer3"] = None
    client = Client(expected)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile, PROFILE).evaluate(
            **args_for_images(root, profile, paths, 3),
        )
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert caught.value.raw_output == client.raw
    assert caught.value.raw_model_output == client.raw
    assert caught.value.structural_diagnostics and caught.value.input_manifest_sha256


@pytest.mark.parametrize("count", [0, 1, 4, True, "2"])
def test_schema_request_count_never_coerces_invalid_values(count):
    with pytest.raises(ValueError):
        parse_output(json.dumps(payload()), count)


@pytest.mark.asyncio
async def test_capture_failure_only_clears_affected_final_score_and_keeps_original(image_setup):
    root, profile, paths = image_setup
    expected = payload(3)
    expected["answer1"]["reason"] = "【类型：任务型；目标：设闹钟；主查：执行；证据状态：不足】缺少准确测试时刻，暂评5。"
    client = Client(expected)
    trusted = {"answer2": {"input_status": "failed", "source": "synthetic collector"},
               "answer3": {"input_status": "partial", "source": "synthetic collector"}}
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args_for_images(root, profile, paths, 3), trusted_collection_metadata=trusted,
    )
    assert result["answer1_overall_score"] == 5
    assert result["answer1_overall_score_status"] == "scored"
    assert result["answer2_overall_score"] is None
    assert result["answer2_overall_score_status"] == "unverifiable"
    assert result["answer3_overall_score"] == 3
    assert result["model_product_results"] == expected
    assert json.loads(result["raw_model_output"]) == expected
    assert result["needs_human_review"] and result["normalization_diagnostics"]
    assert result["answer2_suggestion"] == expected["answer2"]["suggestion"]
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_multiturn_uses_target_and_own_history_without_no_total_conflict(conversation_data, count):
    if count == 3:
        for item in conversation_data[0]:
            item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"),
                        answer3=f"产品3自己的回答{item['turn_index']}", context3="产品3独立背景")
    items, profile, _judge = conversation_data
    parsed, preparation = prepare_conversation(conversation_data)
    target = parsed[1]
    bundle = preparation.prepare(target)
    # Deliberately include a future record to verify target clipping independently
    # of the normal prefix-only conversation preparation contract.
    future = deepcopy(bundle["turns"][-1])
    future.update(turn_index=3, query="FUTURE_DO_NOT_READ", answer1="FUTURE_ANSWER_DO_NOT_READ")
    bundle["turns"].append(future)
    client = Client(payload(count))
    arguments = {
        "question": target["query"], "product_count": count, "evidence_mode": "long_screenshot",
        "screenshot_metas": [target[f"screenshot_meta{p}"] for p in range(1, count + 1)],
        "conversation_input": bundle, "evaluation_time": FIXED_TIME,
    }
    for product in range(1, count + 1):
        arguments[f"frames{product}"] = [part["path"] for part in target[f"screenshot_meta{product}"]["slices"]]
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(**arguments)
    system, _user, sent = client.calls[0]
    text = "\n".join(part.get("text", "") for part in sent["content_parts"])
    assert "问题1" in text and "问题2" in text
    assert "FUTURE_DO_NOT_READ" not in text and "FUTURE_ANSWER_DO_NOT_READ" not in text
    assert "target_turn=2" in text and "score(1—5整数)" in text
    assert "不新增记忆分、自定义总分" not in system
    assert "不输出个性化、总分" not in text
    assert "原有 Schema、适用性、Gate" not in text
    for product in range(1, count + 1):
        assert f"产品{product}自己的回答1" in text and f"产品{product}自己的回答2" in text
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert result["model_product_results"] == payload(count)


@pytest.mark.asyncio
async def test_two_product_reverse_restores_four_fields_and_preserves_raw_trace(image_setup):
    root, profile, paths = image_setup
    original = payload(2)
    client = Client(original)
    arguments = args_for_images(root, profile, paths)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **arguments, reverse_product_order=True,
    )
    received = input_json(client.calls[0][1])
    assert received["products"][0]["platform_context"] == arguments["context2"]
    assert received["products"][0]["answer"] == arguments["answer2"]
    assert received["products"][1]["platform_context"] == arguments["context1"]
    assert result["answer1_overall_score"] == original["answer2"]["score"]
    assert result["answer2_overall_score"] == original["answer1"]["score"]
    assert result["answer1_domain"] == original["answer2"]["domain"]
    assert result["answer2_domain"] == original["answer1"]["domain"]
    assert "产品1截图" in result["answer1_overall_reason"]
    assert "产品2截图" in result["answer2_overall_reason"]
    assert result["answer1_suggestion"] == "产品1减少重复反馈。"
    assert result["answer2_suggestion"] == "产品2减少重复反馈。"
    assert result["model_product_results"]["answer1"]["score"] == original["answer2"]["score"]
    assert result["raw_model_output"] == client.raw
    assert json.loads(result["raw_model_output"]) == original
    assert result["judge_product_order"] == [2, 1] and result["reverse_product_order"] is True
    assert result["overall_winner"] == "answer2"
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_context1", [False, True])
async def test_reverse_keeps_legacy_product_one_background_without_overriding_explicit_empty(
    monkeypatch, explicit_context1,
):
    from auto_eval.judges.compare_protocols import resolve_compare_protocol

    judge = object.__new__(VisualCompareJudge)
    judge.protocol = resolve_compare_protocol(PROFILE)
    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return {"result_schema_version": "dialogue-experience-result-1"}

    monkeypatch.setattr(judge, "_evaluate_in_order", capture)
    await judge.evaluate(question="共享问题", context="LEGACY_PRODUCT_ONE", context2="PRODUCT_TWO",
                         product_count=2, reverse_product_order=True,
                         **({"context1": ""} if explicit_context1 else {}))
    assert calls[0]["context1"] == "PRODUCT_TWO"
    assert calls[0]["context2"] == ("" if explicit_context1 else "LEGACY_PRODUCT_ONE")
