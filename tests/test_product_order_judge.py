"""Judge-position reversal keeps source identities across contracts and media."""
import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest

from auto_eval import evidence_audit, query_images
from auto_eval.config import VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.chitchat_flat_schema import CHITCHAT_FLAT_COMPARE_PROTOCOL_ID
from auto_eval.judges.compare_1022_contract import example_1022_output, product_reason_1022
from auto_eval.judges.compare_protocols import list_compare_protocols, resolve_compare_protocol
from auto_eval.judges.product_order import remap_judge_result
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from test_chitchat_flat_runtime import payload as chat_payload
from test_compare_protocol_versions import _observation_data
from test_knowledge_qa_platform import Client, FIXED_TIME, args_for_images, payload as knowledge_payload
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_qa_chat_protocol import observation_data as old_chat_payload
from test_qa_chat_v2_protocol import payload as chat_v2_payload
from test_timeliness_protocol import payload as timeliness_payload
from test_vqa_input import setup_images as image_setup


def profile():
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


def payload(protocol):
    if protocol.output_contract == "compare_1022_v2":
        return example_1022_output(2)
    if protocol.result_schema_version == "chitchat-flat-result-1":
        return chat_payload()
    if protocol.result_schema_version == "knowledge-qa-result-2":
        return knowledge_payload()
    if protocol.standard_id == "timeliness_compare":
        return timeliness_payload()
    if protocol.result_schema_version == "qa-chat-result-2":
        return chat_v2_payload()
    if protocol.standard_id == "qa_chat_compare":
        return old_chat_payload()
    return {**_observation_data(2), "answer2_understanding_score": 1,
            "understanding_reason": "产品1：第一位置依据；产品2：第二位置依据"}


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", list_compare_protocols(), ids=lambda p: p.id)
async def test_each_protocol_keeps_default_request_and_restores_reversed_scores(protocol):
    raw = payload(protocol)
    dimension = "understanding" if "answer1_understanding_score" in raw else protocol.dimensions[0]
    raw.update({f"answer1_{dimension}_score": 2, f"answer2_{dimension}_score": 1})
    client = Client(raw)
    judge = VisualCompareJudge(client, profile(), protocol)
    args = dict(question="同一共享问题", product_count=2, evaluation_time=FIXED_TIME,
                answer1="FIRST_ANSWER", answer2="SECOND_ANSWER",
                context1="FIRST_CONTEXT", context2="SECOND_CONTEXT")
    in_order = await judge._evaluate_in_order(**args)
    default = await judge.evaluate(**args)
    assert client.calls[0] == client.calls[1]
    assert default[f"answer1_{dimension}_score"] == in_order[f"answer1_{dimension}_score"] == 2
    assert default["judge_product_order"] == [1, 2]
    swapped = await judge.evaluate(**args, reverse_product_order=True)
    _, user, _ = client.calls[-1]
    assert user.index("SECOND_ANSWER") < user.index("FIRST_ANSWER")
    assert user.index("SECOND_CONTEXT") < user.index("FIRST_CONTEXT")
    assert swapped[f"answer1_{dimension}_score"] == 1
    assert swapped[f"answer2_{dimension}_score"] == 2
    if f"{dimension}_winner" in swapped:
        assert swapped[f"{dimension}_winner"] == "answer2"
        assert swapped[f"{dimension}_rank_groups"] == [["product2"], ["product1"]]
    reason_key = f"{dimension}_reason" if f"{dimension}_reason" in raw else f"answer1_{dimension}_reason"
    mapped_key = reason_key.replace("answer1_", "answer2_")
    expected_reason = raw[reason_key].replace("产品1", "ORIGINAL_PRODUCT_2").replace("产品2", "产品1").replace("ORIGINAL_PRODUCT_2", "产品2")
    if protocol.output_contract == "compare_1022_v2":
        for n in (1, 2):
            assert product_reason_1022(swapped[mapped_key], n) == product_reason_1022(raw[reason_key], 3 - n)
    else:
        assert swapped[mapped_key] == expected_reason
    if "raw_model_output" in swapped:
        assert json.loads(swapped["raw_model_output"]) == raw
    if "raw_key_pairs" in swapped:
        assert dict(swapped["raw_key_pairs"]) == raw
    assert swapped["judge_product_order"] == [2, 1]


def test_result_mapping_changes_only_product_owned_business_fields():
    raw_pairs = [["answer1_understanding_score", 4]]
    result = {
        "answer1_understanding_score": 4, "answer2_understanding_score": 1,
        "answer1_response_gate_reason": "产品 1回应，Product1与answer1_understanding_score一致",
        "understanding_reason": "产品1：P1-T01-A01；产品2：P2-T01-A01；product 1, Answer2；产品12保持",
        "understanding_evidence": ["产品1的依据", "产品2的依据"],
        "normalization_diagnostics": ["answer1_understanding_score: 产品1无效"],
        "understanding_winner": "answer1", "relevance": "answer1",
        "understanding_rank_groups": [["product1", "product2"]],
        "trusted_collection_metadata": {"answer1": {"input_status": "failed", "source": "产品1采集器", "details": {"answer1": "product1"}}},
        "raw_model_output": '{"answer1_understanding_score": 4}',
        "raw_key_pairs": raw_pairs, "original_raw_key_pairs": raw_pairs,
        "repair_key_pairs": raw_pairs, "prompt_sha256": "same-hash",
    }
    mapped = remap_judge_result(result)
    assert mapped["answer2_understanding_score"] == 4 and mapped["answer1_understanding_score"] == 1
    assert mapped["answer2_response_gate_reason"] == "产品 2回应，Product2与answer2_understanding_score一致"
    assert mapped["understanding_reason"] == "产品2：P2-T01-A01；产品1：P1-T01-A01；product 2, Answer1；产品12保持"
    assert mapped["understanding_evidence"] == ["产品2的依据", "产品1的依据"]
    assert mapped["normalization_diagnostics"] == ["answer2_understanding_score: 产品2无效"]
    assert mapped["understanding_winner"] == mapped["relevance"] == "answer2"
    assert mapped["understanding_rank_groups"] == [["product1", "product2"]]
    assert mapped["trusted_collection_metadata"] == {"answer2": result["trusted_collection_metadata"]["answer1"]}
    for field in ("raw_model_output", "raw_key_pairs", "original_raw_key_pairs", "repair_key_pairs", "prompt_sha256"):
        assert mapped[field] == result[field]
    assert result["answer1_understanding_score"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
@pytest.mark.parametrize("with_query_image", [False, True])
async def test_real_request_reverses_answers_contexts_and_media_together(image_setup, monkeypatch, mode, with_query_image):
    root, profile, paths = image_setup
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", root / "evidence")
    protocol = resolve_compare_protocol("qa_competitor_compare@0.2-simplified-1022")
    raw = example_1022_output(2)
    raw.update(answer1_understanding_score=2, answer2_understanding_score=1)
    args = args_for_images(root, profile, paths, mode=mode)
    if with_query_image:
        args.update(query_images.prepare_query_images(
            {"query": "QUERY_SENTINEL", "query_images": [paths[0]]},
            session_name="order", cfg=profile.query_images))
        args.pop("query_images", None)
        args.pop("query_image_manifest", None)
    # Only pass the judge API inputs from query-image preparation.
    args = {key: value for key, value in args.items() if key in {
        "question", "evidence_mode", "product_count", "evaluation_time", "context1", "context2",
        "answer1", "answer2", "frames1", "frames2", "screenshot_metas", "query_image_meta"}}
    original = deepcopy(args)
    audits = []
    client = Client(raw)
    result = await VisualCompareJudge(client, profile, protocol).evaluate(
        **args, reverse_product_order=True, evidence_callback=audits.append)
    _, user, sent = client.calls[0]
    assert user.index("P2_COPIED_ANSWER") < user.index("P1_COPIED_ANSWER")
    assert user.index("P2_BACKGROUND") < user.index("P1_BACKGROUND")
    expected_paths = (original.get("query_image_meta", []) and [original["query_image_meta"][0]["path"]] or [])
    expected_paths += original["frames2"] + original["frames1"]
    assert sent["user_image_refs"] == expected_paths
    audit = audits[-1]
    assert audit["judge_product_order"] == [2, 1]
    product_images = [m for m in audit["images"] if m.get("image_role") == "product_answer"]
    assert [(m["product_no"], m["original_product_no"]) for m in product_images] == [(1, 2), (2, 1)]
    assert [m["ref_path"] for m in product_images] == original["frames2"] + original["frames1"]
    if mode == "long_screenshot":
        urls = [p["image_url"]["url"] for p in sent["content_parts"] if p["type"] == "image_url"]
        assert base64.b64decode(urls[-2].split(",", 1)[1]) == Path(original["frames2"][0]).read_bytes()
    assert result["answer1_understanding_score"] == 1
    assert result["answer2_understanding_score"] == 2
    assert result["understanding_winner"] == "answer2"
    assert json.loads(result["raw_model_output"]) == raw
    assert args == original


@pytest.mark.asyncio
async def test_multiturn_reverses_whole_isolated_histories_without_changing_sources(conversation_data, monkeypatch, tmp_path):
    items, profile, _ = conversation_data
    parsed, preparation = prepare_conversation(conversation_data)
    target = parsed[1]
    bundle = preparation.prepare(target)
    original_turns, original_diagnostics = deepcopy(bundle["turns"]), deepcopy(bundle["diagnostics"])
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", tmp_path / "evidence")
    raw = chat_payload()
    raw["understanding_reason"] = "产品1：P1-T01-A01承接；产品2：P2-T01-A01承接"
    client = Client(raw)
    await VisualCompareJudge(client, profile, CHITCHAT_FLAT_COMPARE_PROTOCOL_ID).evaluate(
        question=target["query"], product_count=2, evidence_mode="long_screenshot",
        frames1=target["frames1"], frames2=target["frames2"],
        screenshot_metas=[target["screenshot_meta1"], target["screenshot_meta2"]],
        conversation_input=bundle, reverse_product_order=True, evaluation_time=FIXED_TIME,
    )
    sent = client.calls[0][2]
    text = "\n".join(part.get("text", "") for part in sent["content_parts"])
    first = text.split("产品1自身轨迹开始", 1)[1].split("产品1轨迹结束", 1)[0]
    second = text.split("产品2自身轨迹开始", 1)[1].split("产品2轨迹结束", 1)[0]
    assert "产品2自己的回答1" in first and "产品2自己的回答2" in first
    assert "产品1自己的回答1" in second and "产品1自己的回答2" in second
    assert "P1-T01-A01-S01" in first and "P2-T01-A01-S01" in second
    products = [m for m in sent["image_metadata"] if m["image_role"] == "product_answer"]
    assert [m["ref_path"] for m in products] == [
        *original_turns[0]["frames2"], *original_turns[1]["frames2"],
        *original_turns[0]["frames1"], *original_turns[1]["frames1"],
    ]
    assert bundle["turns"] == original_turns and bundle["diagnostics"] == original_diagnostics


@pytest.mark.asyncio
async def test_trusted_capture_failure_remains_with_original_product_after_reversal():
    raw = chat_payload()
    raw.update(answer1_understanding_score=5, answer2_understanding_score=2)
    client = Client(raw)
    trusted = {"answer1": {"input_status": "failed", "source": "original product 1 collector"}}
    result = await VisualCompareJudge(client, profile(), CHITCHAT_FLAT_COMPARE_PROTOCOL_ID).evaluate(
        question="问题", product_count=2, trusted_collection_metadata=trusted, reverse_product_order=True)
    assert result["answer1_understanding_score"] is None
    assert result["answer2_understanding_score"] == 5
    assert result["trusted_collection_metadata"] == trusted
    assert "产品1可信采集失败" in result["understanding_reason"]
    assert trusted == {"answer1": {"input_status": "failed", "source": "original product 1 collector"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"product_count": 3}, {"answer3": "third"}, {"frames3": []}])
async def test_reversal_rejects_three_products_before_model_call(extra):
    client = Client({})
    with pytest.raises(ValueError, match="三产品"):
        await VisualCompareJudge(client, profile()).evaluate(question="问题", reverse_product_order=True, **extra)
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", ["true", 1, None])
async def test_reversal_flag_requires_a_real_boolean(reverse):
    client = Client({})
    with pytest.raises(ValueError, match="布尔值"):
        await VisualCompareJudge(client, profile()).evaluate(question="问题", reverse_product_order=reverse)
    assert not client.calls


@pytest.mark.asyncio
async def test_legacy_product_one_context_alias_moves_with_its_answer():
    client = Client(example_1022_output(2))
    judge = VisualCompareJudge(client, profile(), "qa_competitor_compare@0.2-simplified-1022")
    await judge.evaluate(question="问题", context="LEGACY_CONTEXT_ONE", context2="CONTEXT_TWO", reverse_product_order=True)
    user = client.calls[0][1]
    assert user.index("CONTEXT_TWO") < user.index("LEGACY_CONTEXT_ONE")


@pytest.mark.asyncio
async def test_compact_reversal_keeps_canonical_reasons_and_explicit_na_statistics():
    from auto_eval.web.compare_statistics import Case, score_state

    raw = example_1022_output(2)
    raw["answer1_understanding_score"] = None
    prefix = "Query判断：当前任务是简短问候；"
    insufficient = "未评分（证据不足）：截图未展示核心问句对应的回答部分"
    scored = "可见回答完整回应问候目标。"
    raw["understanding_reason"] = prefix + f"产品１：{insufficient}；产品２：{scored}"
    protocol = resolve_compare_protocol("qa_competitor_compare@0.2-simplified-1022")
    result = await VisualCompareJudge(Client(raw), profile(), protocol).evaluate(
        question="问题", context1="产品1背景", context2="产品2背景", product_count=2, reverse_product_order=True)
    reason = result["understanding_reason"]
    assert reason == prefix + f"产品1：{scored}；产品2：{insufficient}"
    assert product_reason_1022(reason, 1) == scored
    assert product_reason_1022(reason, 2) == insufficient
    case = Case(0, {}, result, "done", 2, protocol.standard_version, protocol.bundle_revision)
    assert score_state(case, 1, "understanding", protocol) == ("有效评分", 5.0)
    assert score_state(case, 2, "understanding", protocol) == ("证据不足", None)
    assert json.loads(result["raw_model_output"]) == raw


@pytest.mark.asyncio
async def test_fullwidth_product_references_are_restored_in_freeform_reasons():
    protocol = resolve_compare_protocol(None)
    raw = payload(protocol)
    raw["understanding_reason"] = "产品１：Product１、answer１_understanding_score；产品２：Product２、answer２_understanding_score"
    result = await VisualCompareJudge(Client(raw), profile(), protocol).evaluate(
        question="问题", product_count=2, reverse_product_order=True)
    assert result["understanding_reason"] == "产品２：Product２、answer２_understanding_score；产品１：Product１、answer１_understanding_score"
