"""The task's presentation order survives every execution entry point."""
import copy

import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.product_order import validate_product_order
from auto_eval.observability import current_context
from auto_eval.task_control import check_pause
from auto_eval.web import runner
from auto_eval.web.history import task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot, latest_results_by_index


@pytest.fixture
def order_runtime(monkeypatch, tmp_path):
    protocol = resolve_compare_protocol(None)
    cfg = AppConfig(
        judges=[JudgeConfig(name="judge", model="fake")],
        visual_modes={"rich_content": VisualModeProfile(name="rich_content", extraction={"algorithm_version": "test"})},
    )
    item = {
        "id": "q1", "query": "比较结果", "product_count": 2,
        "frames1": ["frame-one"], "frames2": ["frame-two"],
        "answer1": "原产品一回答", "answer2": "原产品二回答",
        "context1": "原产品一上下文", "context2": "原产品二上下文",
    }
    calls = []

    class Client:
        def __init__(self, _cfg):
            pass

        async def aclose(self):
            pass

    class Judge:
        def __init__(self, _client, _profile, selected_protocol):
            self.protocol = selected_protocol

        async def evaluate(self, **kwargs):
            calls.append({k: v for k, v in kwargs.items() if k != "evidence_callback"})
            callback = kwargs.get("evidence_callback")
            if callback:
                callback({"record_status": "model_response_received", "images": [{"product": 1}]})
            return {"answer1_understanding_score": 2, "answer2_understanding_score": 1}

    async def persist(_task):
        return True

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "VisualCompareJudge", Judge)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "retire_task", lambda _task: None)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    return cfg, item, protocol, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("path", ["run", "resume", "retry", "update"])
async def test_order_survives_restore_and_all_execution_paths(order_runtime, reverse, path):
    cfg, item, protocol, calls = order_runtime
    original = Task(
        id="order-lifecycle", mode="compare", items=[copy.deepcopy(item)],
        options={"reverse_product_order": True} if reverse else {},
        evaluation_profile=protocol.id, protocol_manifest=protocol.public_metadata(), active_runs=1,
    )
    await runner.run_eval(original, cfg)
    restored = _task_from_snapshot(copy.deepcopy(task_to_snapshot(original)), original.id)
    restored.active_runs = 1
    if path == "run":
        await runner.run_eval(restored, cfg)
    elif path == "resume":
        restored.execution_control = {
            "state": "queued", "pending_indexes": [0],
            "resumes": [{"id": "resume_order", "status": "queued", "indexes": [0],
                         "concurrency": 1, "completed": 0, "failed": 0}],
        }
        await runner.run_resume(restored, cfg)
    elif path == "retry":
        restored.results = [{"index": 0, "item_id": "q1", "error": "old failure"}]
        restored.retry_runs["retry_order"] = {
            "retry_id": "retry_order", "status": "queued", "indexes": [0],
            "reasons": {"0": "failed"}, "options": {}, "total": 1,
            "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0,
            "items": {"0": {"status": "queued"}},
        }
        await runner.run_retry(restored, cfg, "retry_order")
    else:
        # Program callers may omit options; the task's frozen value still wins.
        await runner.run_update_batch(restored, cfg, [(0, restored.items[0])], options={})
    result = latest_results_by_index(restored)[0]
    assert not result.get("error"), result
    assert len(calls) == 2
    assert all(call.get("reverse_product_order", False) is reverse for call in calls)
    assert result["reverse_product_order"] is reverse
    assert result["judge_product_order"] == ([2, 1] if reverse else [1, 2])
    assert result["model_input_evidence"]["reverse_product_order"] is reverse
    for key in ("answer1", "answer2", "context1", "context2"):
        assert result[key] == item[key]
        assert restored.items[0][key] == item[key]


def test_runner_rejects_changing_frozen_order_before_client_creation(order_runtime):
    cfg, item, protocol, calls = order_runtime
    task = Task(id="frozen-order", mode="compare", items=[item], options={"reverse_product_order": True})
    with pytest.raises(ValueError, match="冻结"):
        runner._make_item_evaluator(task, cfg, options={"reverse_product_order": False})
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_paused_calls_keep_raw_trace_and_annotate_reversed_positions(order_runtime, monkeypatch, reverse):
    cfg, item, protocol, _ = order_runtime
    task = Task(
        id="order-paused-trace", mode="compare", items=[copy.deepcopy(item)],
        options={"reverse_product_order": reverse}, evaluation_profile=protocol.id,
        protocol_manifest=protocol.public_metadata(), active_runs=1,
    )
    raw_record = {"status": "success", "llm_rounds": [{"content": "原始产品1输出"}]}
    flushed = []

    async def evaluate(*args, **kwargs):
        current_context().judge_trace_callback("trace.jsonl", copy.deepcopy(raw_record))
        task.execution_control["state"] = "pausing"
        check_pause()

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "flush_web_trace_records", lambda records, result: flushed.extend(records))
    await runner.run_eval(task, cfg)
    assert len(flushed) == 1
    record = flushed[0][1]
    assert record["llm_rounds"] == raw_record["llm_rounds"]
    if reverse:
        assert record["judge_product_order"] == [2, 1]
        assert record["reverse_product_order"] is True
    else:
        assert record == raw_record


@pytest.mark.asyncio
async def test_technical_failure_still_records_order(order_runtime):
    cfg, item, protocol, calls = order_runtime
    task = Task(
        id="order-failure", mode="compare", items=[{**item, "frames1": []}],
        options={"reverse_product_order": True}, evaluation_profile=protocol.id,
        protocol_manifest=protocol.public_metadata(), active_runs=1,
    )
    await runner.run_eval(task, cfg)
    result = latest_results_by_index(task)[0]
    assert result.get("error")
    assert result["reverse_product_order"] is True
    assert result["judge_product_order"] == [2, 1]
    assert not calls


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_order_option_requires_a_real_boolean(value):
    with pytest.raises(ValueError, match="布尔"):
        validate_product_order("compare", [], {"reverse_product_order": value})


@pytest.mark.parametrize("item", [{"product_count": 3}, {"screenshot3": "third"}, {"frames3": ["third"]}])
def test_order_rejects_third_product(item):
    with pytest.raises(ValueError, match="三产品"):
        validate_product_order("compare", [item], {"reverse_product_order": True})
