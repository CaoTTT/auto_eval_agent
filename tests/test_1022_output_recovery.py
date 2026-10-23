"""Batch-output compatibility retains every original judgment and audit body."""
import json

import pytest

from auto_eval.judges.base import JudgeOutputEmptyError, JudgeOutputParseError
from auto_eval.judges.compare_1022_contract import example_1022_output
from test_1022_compact_runtime import evaluate
from test_1022_transport import _client
from test_evaluation_time_reproducibility import evaluation_runtime, make_task
from auto_eval.web import runner


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("format_issue", ["fence", "bom", "trailing_comma", "missing_comma", "quotes", "newline"])
async def test_local_recovery_preserves_judgments_without_model_repair(count, format_issue):
    expected = example_1022_output(count)
    if format_issue == "quotes":
        expected["understanding_reason"] = expected["understanding_reason"].replace("简短完整问候", '"简短完整问候"')
    if format_issue == "newline":
        expected["understanding_reason"] = expected["understanding_reason"].replace("依据：", "依据：\n")
    raw = json.dumps(expected, ensure_ascii=False)
    if format_issue == "fence":
        raw = "```json\n" + raw + "\n```"
    elif format_issue == "bom":
        raw = "\ufeff" + raw
    elif format_issue == "trailing_comma":
        raw = raw[:-1] + ",}"
    elif format_issue == "missing_comma":
        raw = raw.replace(', "answer2_input_status"', ' "answer2_input_status"', 1)
    elif format_issue == "quotes":
        raw = raw.replace('\\"简短完整问候\\"', '"简短完整问候"')
    else:
        raw = raw.replace("\\n", "\n")
    result, client = await evaluate(raw, count=count)
    for key, value in expected.items():
        assert result[key] == value
    assert result["raw_model_output"] == raw
    assert result["locally_repaired_json"] is True and result["local_format_recovery_steps"]
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
async def test_canonical_output_is_not_reported_as_repaired():
    expected = example_1022_output(2)
    result, _ = await evaluate(expected)
    assert json.loads(result["raw_model_output"]) == expected
    assert result["locally_repaired_json"] is False
    assert result["local_format_recovery_steps"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", " \r\n\t"])
async def test_empty_model_output_is_classified_without_fabricated_scores(raw):
    with pytest.raises(JudgeOutputEmptyError) as caught:
        await evaluate(raw)
    assert caught.value.error_category == "empty_response"
    assert caught.value.raw_output == raw and caught.value.repair_output == ""
    assert caught.value.retriable is True
    assert caught.value.structural_diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", " \r\n\t"])
async def test_complete_empty_response_preserves_transport_audit(tmp_path, raw):
    path = tmp_path / "empty.jsonl"
    client, requests = _client(content=raw, finish_reason="stop", trace_path=path)
    with pytest.raises(JudgeOutputEmptyError) as caught:
        await client.complete("system", "user", require_complete=True)
    assert len(requests) == 1 and caught.value.finish_reason == "stop"
    trace = json.loads(path.read_text(encoding="utf-8"))
    assert trace["status"] == "error" and trace["error_category"] == "empty_response"
    assert trace["llm_rounds"][0]["content"] == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_empty_output_retries_once_through_existing_batch_policy(evaluation_runtime, monkeypatch, recovers):
    cfg, item, protocol, _, _, requests = evaluation_runtime

    class Client(runner.JudgeClient):
        attempts = 0

        async def complete(self, *args, **kwargs):
            type(self).attempts += 1
            if self.attempts == 1 or not recovers:
                raise JudgeOutputEmptyError("empty", raw_output="", repair_output="", judge="fake",
                                            model="fake", error_category="empty_response", finish_reason="stop")
            return await super().complete(*args, **kwargs)

    monkeypatch.setattr(runner, "JudgeClient", Client)
    task = make_task(item, protocol)
    await runner.run_eval(task, cfg)
    assert Client.attempts == 2 and len(requests) == int(recovers)
    assert task.results and bool(task.results[-1].get("error")) is not recovers


def test_filtered_empty_response_is_not_automatically_retried():
    error = JudgeOutputEmptyError("blocked", raw_output="", repair_output="", judge="fake",
                                  model="fake", error_category="empty_response", finish_reason="content_filter")
    assert error.retriable is False


@pytest.mark.asyncio
async def test_failed_semantic_contract_keeps_raw_output_in_error_audit():
    data = example_1022_output(2)
    data["understanding_reason"] = "产品1：回答清楚，但没有产品2的独立说明。"
    raw = "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"
    with pytest.raises(JudgeOutputParseError) as caught:
        await evaluate(raw)
    assert caught.value.error_category == "state"
    audit = runner._structure_error_audit(caught.value)
    assert audit["raw_output"] == raw
    assert audit["structural_diagnostics"]
    assert audit["local_format_recovery_steps"] == ["strip_json_fence"]
