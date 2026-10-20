"""Original media inputs, one-shot calls and stable time evidence."""
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from auto_eval.config import VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot
from test_timeliness_protocol import PROFILE, payload
from test_qa_chat_protocol import FakeClient
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation


class RawClient(FakeClient):
    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.data


def profile():
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
async def test_original_media_is_sent_once_with_explicit_product_roles(tmp_path, count, mode):
    cfg = profile()
    client = FakeClient(payload(count))
    kwargs, metadata = {}, []
    for p in range(1, count + 1):
        path = tmp_path / f"p{p}.png"
        Image.new("RGB", (64, 96), (p * 30, 20, 60)).save(path)
        if mode == "long_screenshot":
            meta = prepare_long_screenshot(path, tmp_path / str(p), cfg.long_screenshot)
            metadata.append(meta)
            kwargs[f"frames{p}"] = [part["path"] for part in meta["slices"]]
        else:
            kwargs[f"frames{p}"] = [str(path)]
    if metadata:
        kwargs["screenshot_metas"] = metadata
    result = await VisualCompareJudge(client, cfg, PROFILE).evaluate(
        question="今天几点开放？", context="测试日期2026-10-20", product_count=count, evidence_mode=mode, **kwargs)
    assert len(client.calls) == 1 and not client.repairs
    parts = client.calls[0][2]["content_parts"]
    assert sum(p["type"] == "image_url" for p in parts) == count
    text = "\n".join(p["text"] for p in parts if p["type"] == "text")
    for p in range(1, count + 1):
        assert f"产品{p}" in text
    assert len(result["raw_key_pairs"]) == 7 * count + 6
    assert result[f"answer{count}_timeliness_score"] == 3
    assert "evaluation_datetime" not in result
    assert result["test_time_policy"] == "provided_evidence_only"


@pytest.mark.asyncio
async def test_audit_clock_never_changes_prompt_but_supplied_test_context_does():
    client = FakeClient(payload())
    judge = VisualCompareJudge(client, profile(), PROFILE)
    kwargs = dict(question="今天几点开放？", context="测试日期2026-10-20")
    first = await judge.evaluate(**kwargs, evaluation_time=datetime(2026, 10, 20, tzinfo=timezone.utc))
    later = await judge.evaluate(**kwargs, evaluation_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert client.calls[0] == client.calls[1]
    assert first["input_manifest_sha256"] == later["input_manifest_sha256"]
    assert first["prompt_sha256"] == later["prompt_sha256"]
    changed = await judge.evaluate(question=kwargs["question"], context="测试日期2026-11-20")
    assert first["input_manifest_sha256"] != changed["input_manifest_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_multiturn_has_own_histories_no_future_and_one_request(conversation_data, count):
    items, cfg, _ = conversation_data
    if count == 3:
        for item in items:
            item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"),
                        answer3="产品3原始回答")
    prepared, service = prepare_conversation(conversation_data)
    target = prepared[1]
    bundle = service.prepare(target)
    kwargs = {f"frames{p}": target[f"frames{p}"] for p in range(1, count + 1)}
    kwargs.update(screenshot_metas=[target[f"screenshot_meta{p}"] for p in range(1, count + 1)],
                  query_image_meta=target.get("query_image_meta"))
    client = FakeClient(payload(count))
    judge = VisualCompareJudge(client, cfg, PROFILE)
    args = dict(question=target["query"], product_count=count, evidence_mode="long_screenshot",
                conversation_input=bundle, **kwargs)
    result = await judge.evaluate(**args, evaluation_time=datetime(2026, 10, 20, tzinfo=timezone.utc))
    text = "\n".join(p["text"] for p in client.calls[0][2]["content_parts"] if p["type"] == "text")
    assert "问题1" in text and "问题2" in text and "问题3" not in text
    assert "任务创建时间和审核时间不是产品测试时间" in text
    assert len(client.calls) == 1 and not client.repairs
    assert len(client.calls[0][2]["image_metadata"]) == 1 + 2 * count
    later = await judge.evaluate(**args, evaluation_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert client.calls[0] == client.calls[1]
    assert result["input_manifest_sha256"] == later["input_manifest_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transform", [
    lambda d: json.dumps(d)[:-1] + ',"answer1_response_gate":"fail"}',
    lambda d: json.dumps({k: v for k, v in d.items() if k != "timeliness_reason"}),
    lambda d: json.dumps([d]),
    lambda d: json.dumps(d)[:-1],
    lambda d: "invalid JSON",
])
async def test_bad_output_is_technical_failure_never_second_model_call(transform):
    raw = transform(payload())
    client = RawClient(raw)
    with pytest.raises(JudgeOutputParseError) as exc:
        await VisualCompareJudge(client, profile(), PROFILE).evaluate(question="q")
    assert exc.value.raw_model_output == raw
    assert exc.value.structural_diagnostics
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
async def test_local_lossless_cleanup_retains_raw_and_uses_one_call():
    data = payload()
    data["timeliness_reason"] = '产品1：原文 ,} 与 "引号"；产品2：原文 ,]'
    raw = json.dumps(data, ensure_ascii=False)[:-1] + ",}"
    client = RawClient(raw)
    result = await VisualCompareJudge(client, profile(), PROFILE).evaluate(question="q")
    assert result["timeliness_reason"] == data["timeliness_reason"]
    assert result["raw_model_output"] == raw
    assert result["locally_repaired_json"] is True
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
async def test_conditional_na_fields_pass_in_the_same_single_call():
    data = payload(3)
    data.update(answer1_understanding_score=None,
                answer1_authority_score=None, answer1_authority_na_reason="not_applicable",
                answer2_timeliness_score=None, answer2_timeliness_na_reason="insufficient_evidence",
                answer3_authority_score=None, answer3_authority_na_reason="applicability_unknown")
    client = FakeClient(data)
    result = await VisualCompareJudge(client, profile(), PROFILE).evaluate(question="q", product_count=3)
    assert len(client.calls) == 1 and not client.repairs
    assert len(result["raw_key_pairs"]) == 30
    assert result["answer1_understanding_score_status"] == "insufficient_evidence"
    assert result["answer1_authority_score_status"] == "not_applicable"
    assert result["answer2_timeliness_score_status"] == "insufficient_evidence"
    assert result["answer3_authority_applicable"] is None
    assert result["answer2_authority_score"] == 3
    assert result["answer2_authority_verification_status"] is None
    assert "answer1_understanding_na_reason" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"answer1_authority_score": None},
    {"answer1_authority_na_reason": "not_applicable"},
    {"answer1_understanding_na_reason": "insufficient_evidence"},
    {"answer1_timeliness_score": None, "answer1_timeliness_na_reason": "I do not know"},
])
async def test_na_contract_errors_never_trigger_model_repair(changes):
    client = FakeClient({**payload(), **changes})
    with pytest.raises(JudgeOutputParseError):
        await VisualCompareJudge(client, profile(), PROFILE).evaluate(question="q")
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
async def test_frozen_legacy_task_still_uses_32_key_prompt_and_schema():
    from auto_eval.judges.visual_compare_prompt_timeliness_r100 import output_example as legacy_example
    legacy = resolve_compare_protocol(PROFILE, "1.0.0")
    client = FakeClient(json.loads(legacy_example(2)))
    result = await VisualCompareJudge(client, profile(), legacy).evaluate(question="q")
    assert len(client.calls) == 1 and not client.repairs
    assert result["bundle_revision"] == "1.0.0"
    assert result["result_schema_version"] == "timeliness-result-1"
    assert len(result["raw_key_pairs"]) == 32
    assert result["answer1_authority_verification_status"] == "verified"
    assert '"answer1_understanding_applicable"' in client.calls[0][0]
