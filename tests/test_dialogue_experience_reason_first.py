"""Generation order changes only in revision 1.0.1, never in frozen tasks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.dialogue_experience_schema import parse_output
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.judges import visual_compare_prompt_dialogue_experience_r100 as frozen
from auto_eval.web.prompt_names import prompt_name, prompt_name_catalog
from test_dialogue_experience_judge import payload
from test_knowledge_qa_platform import Client, FIXED_TIME, args_for_images
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_vqa_input import setup_images as image_setup


PROFILE = "dialogue_experience_compare@1.0"
ORDER = ["reason", "score", "suggestion", "domain"]
NAME = "对话体验对比 | 标准S1.0 | Prompt P01 | Qwen3.8-Flash | 实验"

# Independently captured from unmodified commit 1ff28fb62760d7ad293162cddb0e90b59e75e840.
FROZEN_SOURCE_SHA256 = "9b7b6682dfce8eff5dc89eeedda8a6b5360708e42d56f550b4b02ea6d3638640"
FROZEN_METADATA_SHA256 = "7b599b158bd3bbdb9e1d489c3698d227ab4c9de7cc470a3649f8fcc27a5b6b9f"
FROZEN_RENDERS = {
    (2, "video_frames", False): "2d5bc99921bbd65ace32a768566bf709aee60b73a54b73a194940b1468a5a074",
    (2, "video_frames", True): "8197c75f9fbc1211f69a6f6f6d75f8ed01095934132d668e865e13555068f0e2",
    (2, "long_screenshot", False): "7b122774e011e94529f72ea039e0c8374ccf5bebfea60d0d8d00b8db75672b25",
    (2, "long_screenshot", True): "3858d5239b9c4ee13b56016c75372d7ef214d41902187cb0b9458d988c6f3b42",
    (3, "video_frames", False): "89bd8e1da74b9b0ceea44b537b082910bb6313c9d1ebd504ceb39113c67d57b4",
    (3, "video_frames", True): "effae7cbd13e0ea5823333f0a17a6cd1dfb01610a60c389ae875f5970fc1242f",
    (3, "long_screenshot", False): "e3bbf70ca677a7eb9375ee32af02c5697f62486f87e6a33ae3f5d3e8c5e7bb9f",
    (3, "long_screenshot", True): "bc8cb102998b260f619686909509002da329af7d7dd24f9ab9885f7a974d0530",
}


def output_example(system):
    section = system.split("输出示例仅演示格式，不指定默认分数：", 1)[1]
    value, _tail = json.JSONDecoder().raw_decode(section[section.index("{"):])
    return value


def assert_reason_first_system(system, count):
    assert "先写reason，再写score，最后写suggestion和domain" in system
    assert "不要先确定分数再倒推理由" in system
    example = output_example(system)
    assert list(example) == [f"answer{n}" for n in range(1, count + 1)]
    assert all(list(product) == ORDER for product in example.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
async def test_single_turn_real_request_instructs_reason_before_score(image_setup, count, mode):
    root, profile, paths = image_setup
    client = Client(payload(count))
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args_for_images(root, profile, paths, count, mode),
    )
    assert len(client.calls) == 1 and client.repair_calls == 0
    system, user, _sent = client.calls[0]
    assert_reason_first_system(system, count)
    assert "每个产品先生成reason，再生成score，最后生成suggestion和domain" in user
    assert result["bundle_revision"] == "1.0.1"
    assert result["model_product_results"] == payload(count)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("revision", ["1.0.0", "1.0.1"])
async def test_multiturn_actual_request_keeps_its_frozen_generation_order(
    conversation_data, count, revision,
):
    if count == 3:
        for item in conversation_data[0]:
            item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"),
                        answer3=f"产品3自己的回答{item['turn_index']}", context3="产品3独立背景")
    _items, profile, _judge = conversation_data
    parsed, preparation = prepare_conversation(conversation_data)
    target = parsed[1]
    bundle = preparation.prepare(target)
    arguments = {
        "question": target["query"], "product_count": count, "evidence_mode": "long_screenshot",
        "screenshot_metas": [target[f"screenshot_meta{n}"] for n in range(1, count + 1)],
        "conversation_input": bundle, "evaluation_time": FIXED_TIME,
    }
    for n in range(1, count + 1):
        arguments[f"frames{n}"] = [part["path"] for part in target[f"screenshot_meta{n}"]["slices"]]
    client = Client(payload(count))
    protocol = resolve_compare_protocol(PROFILE, revision)
    result = await VisualCompareJudge(client, profile, protocol).evaluate(**arguments)
    system, _user, sent = client.calls[0]
    texts = [part["text"] for part in sent["content_parts"] if part["type"] == "text"]
    last = texts[-1]
    assert "target_turn=2" in last
    assert "产品1自己的回答1" in "\n".join(texts)
    assert "产品2自己的回答1" in "\n".join(texts)
    assert result["bundle_revision"] == revision
    assert len(client.calls) == 1 and client.repair_calls == 0
    if revision == "1.0.1":
        assert_reason_first_system(system, count)
        assert "先生成reason，再生成score(1—5整数)，最后生成suggestion和domain" in last
    else:
        example = output_example(system)
        assert all(list(product) == ["score", "reason", "suggestion", "domain"] for product in example.values())
        assert "先写reason，再写score" not in system
        assert "每个产品仅score(1—5整数)、reason、suggestion、domain" in last
        assert "先生成reason" not in last


@pytest.mark.parametrize("key,expected", FROZEN_RENDERS.items())
def test_frozen_prompt_and_request_metadata_match_original_commit(key, expected):
    count, mode, prior = key
    protocol = resolve_compare_protocol(PROFILE, "1.0.0")
    values = dict(persona="reason-order-frozen", product_count=count, evidence_mode=mode,
                  include_prior_knowledge=prior, evaluation_datetime="2026-11-02T12:00:00+08:00",
                  question="原题", context1="背景1", context2="背景2", context3="背景3",
                  answer1="回答1", answer2="回答2", answer3="回答3",
                  frame_count1=1, frame_count2=1, frame_count3=1,
                  image_count1=1, image_count2=1, image_count3=1,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system, user = protocol.prompt_templates(count)
    assert hashlib.sha256((system.render(**values) + "\0" + user.render(**values)).encode()).hexdigest() == expected
    metadata = json.dumps(protocol.request_metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(metadata.encode()).hexdigest() == FROZEN_METADATA_SHA256
    assert hashlib.sha256(Path(frozen.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == FROZEN_SOURCE_SHA256


@pytest.mark.parametrize("count", [2, 3])
def test_parser_accepts_equivalent_json_member_orders_without_changing_values(count):
    score_first = payload(count)
    reason_first = {key: {field: value[field] for field in ORDER} for key, value in score_first.items()}
    original = deepcopy(score_first)
    left = parse_output(json.dumps(score_first, ensure_ascii=False), count)
    right = parse_output(json.dumps(reason_first, ensure_ascii=False), count)
    assert left.model_dump() == right.model_dump() == original
    assert score_first == original


def test_current_catalog_and_historical_alias_keep_one_existing_label():
    current = resolve_compare_protocol(PROFILE)
    historical = resolve_compare_protocol(PROFILE, "1.0.0")
    assert current.bundle_revision == "1.0.1"
    assert current.public_metadata()["model_output_fields"] == ORDER
    assert historical.public_metadata()["model_output_fields"] == ["score", "reason", "suggestion", "domain"]
    records = [record for record in prompt_name_catalog() if record["evaluation_profile"] == PROFILE]
    assert len(records) == 1 and records[0]["bundle_revision"] == "1.0.1"
    assert records[0]["canonical_name"] == NAME
    assert prompt_name(PROFILE, "1.0.0") == prompt_name(PROFILE, "1.0.1") == NAME
