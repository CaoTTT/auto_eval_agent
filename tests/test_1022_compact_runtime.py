"""Offline integration checks for the separate compact comparison protocols."""
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_1022_contract import DIMENSIONS_1022, example_1022_output
from auto_eval.judges.compare_1022_contract_r021 import example_1022_output as legacy_example
from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge, visual_compare_result_fields
from auto_eval.long_screenshot import prepare_long_screenshot


class OfflineClient:
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
        pytest.fail("Compact judgments must not enter model semantic repair")


def profile():
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


async def evaluate(output, *, count=2, mode="off", revision=None, **kwargs):
    client = OfflineClient(output, mode)
    protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, revision)
    result = await VisualCompareJudge(client, profile(), protocol).evaluate(
        question="你好", product_count=count,
        evaluation_time=datetime(2026, 10, 22, tzinfo=timezone.utc), **kwargs,
    )
    return result, client


def assert_compact_fields(result, count):
    forbidden = {"evidence", "confidence", "needs_human_review", "review_reasons", "rationale",
                 "needs_review", "needs_review_label", "review_reason"}
    assert not forbidden.intersection(result)
    assert not any("accuracy" in key for key in result)
    assert not any(key.endswith(("_verification_status", "_evidence", "_primary_issue")) for key in result)
    assert any(key.startswith("answer3_") for key in result) == (count == 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,fields", [(2, 36), (3, 47)])
async def test_new_revision_uses_actual_product_fields_and_derived_only_metadata(count, fields):
    raw = example_1022_output(count)
    result, client = await evaluate(raw, count=count, mode="json_schema")
    assert all(result[key] == value for key, value in raw.items())
    assert_compact_fields(result, count)
    assert result["standard_id"] == "qa_competitor_compare"
    assert result["standard_version"] == "0.2-simplified-1022"
    assert result["product_count"] == count
    assert result["evaluation_profile"] == V1022_COMPARE_PROTOCOL_ID
    assert result["bundle_revision"] == "0.2.3"
    assert result["output_contract"] == "compare_1022_v2"
    assert result["evaluation_datetime"] == "2026-10-22T00:00:00+00:00"
    assert result["understanding_rank_groups"] == [[f"product{n}" for n in range(1, count + 1)]]
    assert result["overall_winner"] is None
    assert all(result[f"answer{n}_total_score"] is None for n in range(1, count + 1))
    assert len(result["prompt_sha256"]) == 64
    assert len(client.calls) == 1 and client.repair_calls == 0
    system, user, options = client.calls[0]
    assert options["require_complete"] is True
    schema = options["response_format"]["json_schema"]["schema"]
    assert len(schema["properties"]) == fields
    assert list(schema["properties"]) == list(raw)
    assert list(result)[:fields] == list(raw)
    assert ("answer3_response_gate" in schema["properties"]) == (count == 3)
    assert ("answer3_response_gate" in system + user) == (count == 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_frozen_r022_keeps_reasons_first_prompt_schema_parser_and_projection(count):
    from auto_eval.judges.compare_1022_contract_r022 import example_1022_output as old_example
    from auto_eval.judges.visual_compare_prompt_v02_1022_r022 import templates_1022 as old_templates

    raw = old_example(count)
    result, client = await evaluate(raw, count=count, revision="0.2.2", mode="json_schema")
    assert result["bundle_revision"] == "0.2.2"
    assert result["output_contract"] == "compare_1022_v2"
    assert_compact_fields(result, count)
    assert list(result)[:len(raw)] == list(raw)
    assert list(client.calls[0][2]["response_format"]["json_schema"]["schema"]["properties"]) == list(raw)
    assert client.calls[0][0] == old_templates(count)[0].render(
        persona=client.persona, product_count=count, evidence_mode="video_frames")


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["0.2.2", "0.2.3"])
async def test_runtime_does_not_mix_frozen_output_order(revision):
    from auto_eval.judges.compare_1022_contract_r022 import example_1022_output as old_example

    wrong = example_1022_output(2) if revision == "0.2.2" else old_example(2)
    with pytest.raises(JudgeOutputParseError) as caught:
        await evaluate(wrong, revision=revision)
    assert caught.value.error_category == "state"


@pytest.mark.asyncio
async def test_triple_scores_generate_ranking_without_reintroducing_removed_fields():
    raw = example_1022_output(3)
    raw["answer1_understanding_score"] = 1
    raw["answer2_understanding_score"] = 4
    raw["understanding_reason"] = (
        "产品1：依据：第1帧回应无关对象；判定：偏离核心目标；影响：无法使用；"
        "产品2：依据：第1帧完成问候但遗漏简短要求；判定：次要约束遗漏；影响：轻微；"
        "产品3：依据：第1帧完整回应问候；判定：满足可见约束；影响：可直接使用"
    )
    result, _ = await evaluate(raw, count=3)
    assert result["understanding_rank_groups"] == [["product3"], ["product2"], ["product1"]]
    assert result["understanding_winner"] == "answer2"
    assert result["understanding_reason"] == raw["understanding_reason"]
    assert_compact_fields(result, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["response", "safety"])
@pytest.mark.parametrize("verdict", ["fail", "unclear"])
async def test_gate_shortcut_is_applied_before_ranking(gate, verdict):
    raw = example_1022_output(2)
    raw[f"answer1_{gate}_gate"] = verdict
    for dimension in DIMENSIONS_1022:
        del raw[f"answer1_{dimension}_score"]
    result, client = await evaluate(raw)
    assert all(result[f"answer1_{dimension}_score"] is None for dimension in DIMENSIONS_1022)
    assert result["answer2_understanding_score"] == 5
    assert result["understanding_rank_groups"] == [["product2"]]
    assert result["understanding_winner"] is None
    assert result["content_conflict"] == "unclear"
    assert_compact_fields(result, 2)
    assert client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_all_blocked_minimal_response_is_completed_with_na(count):
    raw = {key: value for key, value in example_1022_output(count).items()
           if key.startswith("answer") and not key.endswith("_score")}
    for n in range(1, count + 1):
        raw[f"answer{n}_safety_gate"] = "fail"
    result, _ = await evaluate(raw, count=count)
    assert_compact_fields(result, count)
    for dimension in DIMENSIONS_1022:
        assert result[f"{dimension}_applicable"] is None
        assert result[f"{dimension}_rank_groups"] == []
        for n in range(1, count + 1):
            assert result[f"answer{n}_{dimension}_score"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "json_object", "json_schema"])
async def test_structured_transport_does_not_change_compact_contract(mode):
    result, client = await evaluate(example_1022_output(2), mode=mode)
    sent = client.calls[0][2]
    assert sent["require_complete"] is True
    assert sent["response_format"] is None if mode == "off" else sent["response_format"]["type"] == mode
    assert_compact_fields(result, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_frozen_r021_remains_resumable_with_old_contract(count):
    result, client = await evaluate(legacy_example(count), count=count, revision="0.2.1")
    assert result["bundle_revision"] == "0.2.1"
    assert result["output_contract"] == "compare_1022_v1"
    assert "accuracy_reason" in result
    assert "understanding_verification_status" in result
    assert "answer3_response_gate" in result
    assert "needs_human_review" in result
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["json_syntax", "schema", "state"])
async def test_invalid_output_reports_raw_error_without_semantic_repair(category):
    raw = example_1022_output(2)
    if category == "schema":
        raw["evidence"] = []
    elif category == "state":
        raw["answer1_understanding_score"] = None
    text = json.dumps(raw, ensure_ascii=False)
    if category == "json_syntax":
        text = text[:-1]
    client = OfflineClient(text)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile(), V1022_COMPARE_PROTOCOL_ID).evaluate(
            question="你好", product_count=2,
        )
    assert caught.value.error_category == category
    assert caught.value.raw_output == text
    assert caught.value.repair_output == ""
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,received", [(2, 3), (3, 2)])
async def test_evaluator_routes_to_matching_product_parser(requested, received):
    with pytest.raises(JudgeOutputParseError):
        await evaluate(example_1022_output(received), count=requested)


@pytest.mark.asyncio
async def test_risky_screenshot_does_not_resurrect_removed_review_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    cfg = profile()
    metas = []
    for n in (1, 2):
        path = tmp_path / f"product{n}.png"
        Image.new("RGB", (48, 64), (n * 60, 50, 70)).save(path)
        meta = prepare_long_screenshot(path, tmp_path / f"parts{n}", cfg.long_screenshot)
        meta["split_status"] = "risky"
        metas.append(meta)
    client = OfflineClient(example_1022_output(2, evidence_mode="long_screenshot"))
    result = await VisualCompareJudge(client, cfg, V1022_COMPARE_PROTOCOL_ID).evaluate(
        question="你好", product_count=2, evidence_mode="long_screenshot",
        screenshot_metas=metas,
        frames1=[part["path"] for part in metas[0]["slices"]],
        frames2=[part["path"] for part in metas[1]["slices"]],
    )
    assert_compact_fields(result, 2)
    assert result["answer1_understanding_score"] == 5
    assert "最终回答长截图" in client.calls[0][1]


def test_direct_projection_does_not_add_fake_third_product():
    result = visual_compare_result_fields(
        example_1022_output(2), resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID),
    )
    assert_compact_fields(result, 2)
    assert result["understanding_rank_groups"] == [["product1", "product2"]]


def test_compact_summary_does_not_invent_review_count():
    from auto_eval.web.runner import _summarize
    from auto_eval.web.tasks import Task

    protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)
    result = visual_compare_result_fields(example_1022_output(2), protocol)
    result.update(index=0, item_id="case0", query="你好")
    task = Task(id="compact-summary", mode="compare", items=[{"id": "case0", "query": "你好"}],
                options={}, results=[result], evaluation_profile=protocol.id,
                protocol_manifest=protocol.public_metadata(), status="done")
    summary = _summarize(task, include_subsets=False)
    assert "needs_human_review_count" not in summary
    assert summary["understanding_answer1_avg"] == 5

