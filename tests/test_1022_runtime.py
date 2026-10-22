"""Exercise the 1022 wire contract through the actual comparison evaluator."""
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from auto_eval import query_images
from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_1022_contract_r021 import DIMENSIONS_1022, example_1022_output
from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot


class OutputClient:
    persona = "离线测试裁判"
    model = "offline-test-model"

    def __init__(self, output, mode="off"):
        self.cfg = JudgeConfig(name="offline", compare_structured_output=mode)
        self.output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        self.calls = []
        self.repair_calls = 0

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.output

    async def repair_json(self, *args, **kwargs):
        self.repair_calls += 1
        pytest.fail("1022 malformed judgments must never enter model JSON repair")


def profile():
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


async def evaluate(output, *, count=2, mode="off", **kwargs):
    client = OutputClient(output, mode)
    result = await VisualCompareJudge(client, profile(), resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.1")).evaluate(
        question="你好", product_count=count,
        evaluation_time=datetime(2026, 10, 22, tzinfo=timezone.utc), **kwargs,
    )
    return result, client


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_verified_judgments_are_preserved_and_task_metadata_is_injected(count):
    raw = example_1022_output(count)
    result, client = await evaluate(raw, count=count)
    assert not {"standard_id", "standard_version", "product_count"} & raw.keys()
    for key, value in raw.items():
        assert result[key] == value, key
    assert result["standard_id"] == "qa_competitor_compare"
    assert result["standard_version"] == "0.2-simplified-1022"
    assert result["product_count"] == count
    assert result["evaluation_profile"] == V1022_COMPARE_PROTOCOL_ID
    assert result["bundle_revision"] == "0.2.1"
    assert result["output_contract"] == "compare_1022_v1"
    assert result["evaluation_datetime"] == "2026-10-22T00:00:00+00:00"
    assert result["understanding_rank_groups"] == [[f"product{n}" for n in range(1, count + 1)]]
    assert result["overall_winner"] is None
    assert all(result[f"answer{n}_total_score"] is None for n in (1, 2, 3))
    assert len(result["prompt_sha256"]) == 64
    assert len(client.calls) == 1 and not client.repair_calls
    assert client.calls[0][2]["require_complete"] is True


@pytest.mark.asyncio
async def test_nonmaximal_scores_and_reasons_survive_with_derived_ranking():
    raw = example_1022_output(3)
    raw["answer1_understanding_score"] = 1
    raw["answer2_understanding_score"] = 4
    raw["understanding_primary_issue"] = "产品1：答非所问；产品2：次要格式约束遗漏；产品3：无实质问题"
    raw["understanding_reason"] = (
        "产品1：回答围绕无关对象展开，未完成核心要求；"
        "产品2：核心要求已满足，但未采用指定的简短句式；"
        "产品3：核心要求和可见约束均已满足"
    )
    result, _ = await evaluate(raw, count=3)
    assert [result[f"answer{n}_understanding_score"] for n in (1, 2, 3)] == [1, 4, 5]
    assert result["understanding_reason"] == raw["understanding_reason"]
    assert result["understanding_primary_issue"] == raw["understanding_primary_issue"]
    assert result["understanding_rank_groups"] == [["product3"], ["product2"], ["product1"]]


@pytest.mark.asyncio
async def test_safety_failure_does_not_apply_legacy_safety_score_clearing():
    raw = example_1022_output()
    raw["answer1_safety_gate"] = "fail"
    raw["answer1_safety_gate_reason"] = "可见回答包含明确危险保证。"
    result, _ = await evaluate(raw)
    assert result["answer1_safety_gate"] == "fail"
    assert result["answer1_understanding_score"] == 5
    assert result["understanding_reason"] == raw["understanding_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["input_failed", "response_fail", "response_unclear"])
async def test_legitimate_product_gate_na_keeps_its_explanation(state):
    raw = example_1022_output()
    if state == "input_failed":
        raw["answer1_input_status"] = "failed"
        raw["answer1_response_gate"] = raw["answer1_safety_gate"] = "unclear"
        cause = "输入失败"
    else:
        raw["answer1_response_gate"] = "fail" if state == "response_fail" else "unclear"
        cause = "响应门槛未通过或无法确认"
    if state != "response_fail":
        raw["needs_human_review"] = True
        raw["review_reasons"] = [cause]
    for dimension in DIMENSIONS_1022:
        raw[f"answer1_{dimension}_score"] = None
        if raw[f"{dimension}_applicable"]:
            raw[f"{dimension}_primary_issue"] = f"产品1：{cause}；产品2：无实质问题"
            raw[f"{dimension}_reason"] = (
                f"产品1：未评分（{cause}）：不能对该产品公平评价；"
                "产品2：可见问候完整且重点清楚，满足当前要求。"
            )
    result, _ = await evaluate(raw)
    assert result["answer1_understanding_score"] is None
    assert result["answer2_understanding_score"] == 5
    assert result["understanding_reason"] == raw["understanding_reason"]
    assert result["needs_human_review"] == raw["needs_human_review"]


@pytest.mark.asyncio
async def test_accuracy_unverifiable_is_valid_na_without_automatic_review():
    raw = example_1022_output()
    raw.update(
        accuracy_applicable=True,
        accuracy_verification_status="unverifiable",
        accuracy_primary_issue="产品1：缺少外部核验依据；产品2：缺少外部核验依据",
        accuracy_reason=(
            "产品1：未评分（无法核验）：当前未提供独立事实依据；"
            "产品2：未评分（无法核验）：当前未提供独立事实依据"
        ),
    )
    result, _ = await evaluate(raw)
    assert result["answer1_accuracy_score"] is None
    assert result["answer2_accuracy_score"] is None
    assert result["accuracy_reason"] == raw["accuracy_reason"]
    assert result["needs_human_review"] is False


@pytest.mark.asyncio
async def test_partial_dimension_with_bounded_judgment_is_not_forced_into_legacy_review():
    raw = example_1022_output()
    raw["understanding_verification_status"] = "partial"
    raw["confidence"] = "medium"
    raw["understanding_reason"] = (
        "产品1：可见核心问候完整，边缘装饰未覆盖，不影响对意图的判断；"
        "产品2：可见核心问候完整，边缘装饰未覆盖，不影响对意图的判断"
    )
    result, _ = await evaluate(raw)
    assert result["answer1_understanding_score"] == 5
    assert result["understanding_reason"] == raw["understanding_reason"]
    assert result["needs_human_review"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["json_syntax", "schema", "state"])
async def test_bad_output_retains_raw_category_and_never_invokes_semantic_repair(category):
    raw = example_1022_output()
    if category == "schema":
        del raw["answer1_response_gate"]
    elif category == "state":
        raw["answer1_understanding_score"] = None
    text = json.dumps(raw, ensure_ascii=False)
    if category == "json_syntax":
        text = text[:-1]
    client = OutputClient(text)
    judge = VisualCompareJudge(client, profile(), resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.1"))
    with pytest.raises(JudgeOutputParseError) as caught:
        await judge.evaluate(question="你好", product_count=2)
    assert caught.value.error_category == category
    assert caught.value.raw_output == text
    assert caught.value.repair_output == ""
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "json_object", "json_schema"])
async def test_request_uses_only_explicit_structured_output_mode(mode):
    _, client = await evaluate(example_1022_output(), mode=mode)
    sent = client.calls[0][2]
    assert sent["require_complete"] is True
    if mode == "off":
        assert sent["response_format"] is None
    else:
        assert sent["response_format"]["type"] == mode
    if mode == "json_schema":
        schema = sent["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert len(schema["required"]) == 78
        assert schema["properties"]["answer3_response_gate"] == {"type": "null"}


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_mode,with_query_image,count", [
    ("video_frames", False, 2), ("video_frames", True, 2),
    ("long_screenshot", False, 2), ("long_screenshot", True, 3),
])
async def test_visual_and_shared_query_inputs_reach_strict_evaluator(
    tmp_path, monkeypatch, evidence_mode, with_query_image, count,
):
    monkeypatch.setattr(query_images, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(query_images, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    cfg = profile()
    paths = []
    for n in range(count + 1):
        path = tmp_path / f"input{n}.png"
        Image.new("RGB", (48, 64), (n * 50, 40, 80)).save(path)
        paths.append(path)
    prepared = query_images.prepare_query_images(
        {"query": "你好", "query_images": [str(paths[0])] if with_query_image else []},
        session_name="1022", cfg=cfg.query_images,
    )
    arguments = {f"frames{n}": [str(paths[n])] for n in range(1, count + 1)}
    if evidence_mode == "long_screenshot":
        metas = [prepare_long_screenshot(paths[n], tmp_path / f"parts{n}", cfg.long_screenshot)
                 for n in range(1, count + 1)]
        arguments["screenshot_metas"] = metas
        for n, meta in enumerate(metas, 1):
            arguments[f"frames{n}"] = [part["path"] for part in meta["slices"]]
    client = OutputClient(example_1022_output(count, evidence_mode=evidence_mode), "json_object")
    result = await VisualCompareJudge(client, cfg, resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.1")).evaluate(
        question="你好", product_count=count, evidence_mode=evidence_mode,
        query_image_meta=prepared["query_image_meta"], **arguments,
    )
    system, user, sent = client.calls[0]
    assert sent["require_complete"] is True
    assert sent["response_format"] == {"type": "json_object"}
    assert result["answer1_understanding_score"] == 5
    if with_query_image or evidence_mode == "long_screenshot":
        images = [part for part in sent["content_parts"] if part["type"] == "image_url"]
        assert len(images) == count + int(with_query_image)
    else:
        assert len(sent["user_images"]) == count
    if with_query_image:
        assert "共享提问图片读取说明" in system
        assert sent["image_metadata"][0]["image_role"] == "query_image"
        assert "input_manifest_sha256" in result
    if evidence_mode == "long_screenshot":
        assert "最终回答长截图" in user
        assert all(str(n) in user for n in range(1, count + 1))
