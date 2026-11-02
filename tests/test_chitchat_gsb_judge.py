"""Exercise real GSB requests, product identity, media and strict parsing."""
from copy import deepcopy
import hashlib
import json

import pytest

from auto_eval import evidence_audit, query_images
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.product_order import remap_judge_result
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.web import server
from auto_eval.web.prompt_names import prompt_name
from test_knowledge_qa_platform import Client, FIXED_TIME, args_for_images
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_vqa_input import setup_images as image_setup


PROFILE = "chitchat_gsb_compare@1.0"


def payload(count=2):
    return {
        f"pair_{a}_{b}": {
            "status": "evaluated", "gsb": "G", "serious_issue": "none",
            "reason": f"产品{a}截图正文给出适合处境的话术；产品{b}只有原则，需要用户自行组织，前者更满足本轮需求。",
        }
        for a in range(1, count + 1) for b in range(a + 1, count + 1)
    }


def input_json(user):
    return json.JSONDecoder().raw_decode(user[user.index("{"):])[0]


def test_protocol_is_independent_selectable_and_has_no_absolute_score_range():
    protocol = server._compare_protocol_or_422(PROFILE, require_selectable=True)
    metadata = protocol.public_metadata()
    assert metadata["result_type"] == "pairwise_gsb"
    assert metadata["score_range"] is None and metadata["score_dimensions"] == []
    assert metadata["has_gates"] is False
    assert metadata["conversation_adapter_version"] == "compare-conversation-1"
    assert metadata["input_modalities"] == ["text", "text_image"]
    assert prompt_name(PROFILE, "1.0.0") == "闲聊GSB对比 | 标准S1.0 | Prompt P01 | Qwen3.8-Flash | 实验"
    assert prompt_name(PROFILE, "unknown", fallback="historical") == "historical"
    with pytest.raises(ValueError):
        resolve_compare_protocol(PROFILE, "99.0.0")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
@pytest.mark.parametrize("with_query_image", [False, True])
async def test_images_contexts_and_strict_pair_results_share_one_request(
    image_setup, count, mode, with_query_image,
):
    root, profile, paths = image_setup
    prepared = query_images.prepare_query_images(
        {"query": "QUERY_SENTINEL", "query_images": [paths[0]] if with_query_image else []},
        session_name="gsb-image-test", cfg=profile.query_images,
    )
    client = Client(payload(count))
    args = args_for_images(root, profile, paths, count, mode)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args, query_image_meta=prepared["query_image_meta"],
    )
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert result["model_pair_results"] == payload(count)
    assert json.loads(result["raw_model_output"]) == payload(count)
    assert set(result["gsb_pairs"]) == ({"1_2"} if count == 2 else {"1_2", "1_3", "2_3"})
    assert not any(key.endswith(("_score", "_gate", "_winner", "_ranking")) for key in result)
    assert result["input_manifest_sha256"] and result["test_time_policy"] == "provided_evidence_only"
    assert "evaluation_datetime" not in result
    system, user, sent = client.calls[0]
    data = input_json(user)
    assert data["query"] == args["question"]
    assert data["product_count"] == count and sent["require_complete"] is True
    assert len([p for p in sent["content_parts"] if p["type"] == "image_url"]) == count + int(with_query_image)
    assert len(sent["image_metadata"]) == count + int(with_query_image)
    for product, record in enumerate(data["products"], 1):
        assert record["product_no"] == product
        assert record["platform_context"] == args[f"context{product}"]
        assert record["answer"] == args[f"answer{product}"]
        assert record["context"] == []
        for missing in ("current_time_beijing", "user_info", "user_md", "knowledge", "resultDataList",
                        "intent", "slots", "intent_explain", "slot_explain"):
            assert record[missing] == ""
    assert "先分别绝对评分，再比较" not in system


@pytest.mark.asyncio
async def test_reverse_restores_gsb_serious_ownership_and_keeps_raw_trace(image_setup):
    root, profile, paths = image_setup
    raw = payload()
    raw["pair_1_2"]["serious_issue"] = "B"
    client = Client(raw)
    args = args_for_images(root, profile, paths)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(**args, reverse_product_order=True)
    received = input_json(client.calls[0][1])
    assert received["products"][0]["platform_context"] == args["context2"]
    assert received["products"][0]["answer"] == args["answer2"]
    pair = result["gsb_pairs"]["1_2"]
    assert pair["gsb"] == "B" and pair["serious_issue"] == "A"
    assert pair["product_a"] == 1 and pair["product_b"] == 2
    assert pair["reason"].startswith("产品2截图正文") and "产品1只有原则" in pair["reason"]
    assert result["model_pair_results"]["pair_1_2"]["gsb"] == "B"
    assert json.loads(result["raw_model_output"]) == raw
    assert result["judge_product_order"] == [2, 1]
    restored = remap_judge_result(result)
    assert restored["gsb_pairs"]["1_2"]["gsb"] == raw["pair_1_2"]["gsb"]
    assert restored["gsb_pairs"]["1_2"]["reason"] == raw["pair_1_2"]["reason"]


@pytest.mark.asyncio
async def test_trusted_failure_is_local_to_pairs_and_hashes_and_survives_reversal(image_setup):
    root, profile, paths = image_setup
    args = args_for_images(root, profile, paths, count=3)
    client = Client(payload(3))
    judge = VisualCompareJudge(client, profile, PROFILE)
    normal = await judge.evaluate(**args)
    trusted = {"answer3": {"input_status": "failed", "source": "collector"}}
    failed = await judge.evaluate(**args, trusted_collection_metadata=trusted)
    assert failed["gsb_pairs"]["1_2"]["gsb"] == "G"
    for key in ("1_3", "2_3"):
        assert failed["gsb_pairs"][key]["status"] == "insufficient_evidence"
        assert failed["gsb_pairs"][key]["gsb"] is None
    assert normal["input_manifest_sha256"] != failed["input_manifest_sha256"]
    assert failed["model_pair_results"] == payload(3)
    client2 = Client(payload())
    dual = await VisualCompareJudge(client2, profile, PROFILE).evaluate(
        question="同题", answer1="材料1", answer2="材料2", product_count=2,
        reverse_product_order=True,
        trusted_collection_metadata={"answer1": {"input_status": "failed", "source": "collector"}},
    )
    assert dual["trusted_collection_metadata"]["answer1"]["input_status"] == "failed"
    assert dual["gsb_pairs"]["1_2"]["gsb"] is None
    assert "产品1可信采集失败" in dual["gsb_pairs"]["1_2"]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{}', '{"pair_1_2":{"status":"evaluated","gsb":"G","gsb":"B","reason":"证据","serious_issue":"none"}}',
    '{"pair_1_2":{"status":"insufficient_evidence","gsb":"S","reason":"缺证","serious_issue":"none"}}',
])
async def test_invalid_semantic_contract_never_calls_a_second_model(image_setup, raw):
    _, profile, _ = image_setup
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile, PROFILE).evaluate(question="问题", product_count=2)
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert caught.value.raw_output == raw and caught.value.structural_diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_multiturn_has_no_future_or_absolute_scoring_and_keeps_pair_contract(
    conversation_data, count, monkeypatch, tmp_path,
):
    items, profile, _ = conversation_data
    if count == 3:
        for item in items:
            item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"),
                        answer3="产品3自己的回答", context3="产品3自己的背景")
    parsed, preparation = prepare_conversation(conversation_data)
    target = parsed[1]
    bundle = preparation.prepare(target)
    future = deepcopy(bundle["turns"][-1])
    future.update(turn_index=3, query="FUTURE_DO_NOT_READ", answer1="FUTURE_ANSWER_DO_NOT_READ")
    bundle["turns"].append(future)
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", tmp_path / "evidence")
    client = Client(payload(count))
    audits = []
    args = dict(question=target["query"], product_count=count, evidence_mode="long_screenshot",
                screenshot_metas=[target[f"screenshot_meta{n}"] for n in range(1, count + 1)],
                conversation_input=bundle, evaluation_time=FIXED_TIME, evidence_callback=audits.append)
    args.update({f"frames{n}": target[f"frames{n}"] for n in range(1, count + 1)})
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(**args)
    system, _, sent = client.calls[0]
    text = "\n".join(p.get("text", "") for p in sent["content_parts"])
    assert "target_turn=2" in text and "问题1" in text and "问题2" in text
    assert "FUTURE_DO_NOT_READ" not in text and "FUTURE_ANSWER_DO_NOT_READ" not in text
    assert "先分别绝对评分，再比较" not in system
    assert "原有 Schema、适用性、Gate 与分数范围" not in text
    assert "每对仅status、gsb、reason、serious_issue" in text
    assert {m.get("source_turn") for m in audits[-1]["preprocessing"]} == {1, 2}
    assert result["model_pair_results"] == payload(count)


def test_existing_dialogue_experience_identity_and_prompt_renders_are_frozen():
    protocol = resolve_compare_protocol("dialogue_experience_compare@1.0", "1.0.0")
    renders = []
    for count in (2, 3):
        for mode in ("video_frames", "long_screenshot"):
            for prior in (False, True):
                args = dict(persona="frozen-check", product_count=count, evidence_mode=mode,
                            include_prior_knowledge=prior, evaluation_datetime="2026-11-02T12:00:00+08:00",
                            question="原题", context1="背景1", context2="背景2", context3="背景3",
                            answer1="回答1", answer2="回答2", answer3="回答3",
                            frame_count1=1, frame_count2=1, frame_count3=1,
                            image_count1=1, image_count2=1, image_count3=1,
                            split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
                system, user = protocol.prompt_templates(count)
                renders.append(hashlib.sha256((system.render(**args) + "\0" + user.render(**args)).encode()).hexdigest())
    value = {"metadata": protocol.request_metadata(), "renders": renders}
    assert hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() == "4a42d1a4276a9da504ca1020376f2a7b2728973bea8a834943dc6ef6ecb01a8a"
