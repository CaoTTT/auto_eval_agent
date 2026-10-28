"""Comparison prompts retain the task's clock through the full Web lifecycle."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges import visual_compare_judge
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import runner
from auto_eval.web.history import task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot, latest_results_by_index


BUSINESS_TIMEZONE = timezone(timedelta(hours=8))
CREATED_AT = datetime(2026, 10, 20, 0, 30, tzinfo=BUSINESS_TIMEZONE)


@pytest.fixture
def evaluation_runtime(tmp_path, monkeypatch):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (40, 40), "white").save(frame)
    protocol = resolve_compare_protocol(None)
    cfg = AppConfig(
        judges=[JudgeConfig(name="judge", model="fake")],
        visual_modes={"rich_content": VisualModeProfile(
            name="rich_content", extraction={"algorithm_version": "test"},
        )},
    )
    item = {
        "id": "q1", "query": "今天的推荐是什么？", "product_count": 2,
        "frames1": [str(frame)], "frames2": [str(frame)],
        "answer1": "推荐一", "answer2": "推荐二",
    }
    calls, requests = [], []

    class Clock(datetime):
        current = datetime(2026, 10, 21, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    class Client:
        def __init__(self, config):
            self.cfg, self.model, self.persona = config, config.model, "测试裁判"

        async def complete(self, system, user, **kwargs):
            requests.append((system, user))
            return json.dumps({
                "product_count": 2,
                "answer1_input_status": "complete", "answer2_input_status": "complete",
                "answer1_response_gate": "pass", "answer2_response_gate": "pass",
                "answer1_safety_gate": "pass", "answer2_safety_gate": "pass",
                "understanding_applicable": True,
                "understanding_verification_status": "not_required",
                "answer1_understanding_score": protocol.score_max,
                "answer2_understanding_score": protocol.score_max - 1,
            })

        async def aclose(self):
            pass

    class Judge(visual_compare_judge.VisualCompareJudge):
        async def evaluate(self, **kwargs):
            calls.append(kwargs.get("evaluation_time"))
            return await super().evaluate(**kwargs)

    async def persist(_task):
        return True

    monkeypatch.setattr(runner, "datetime", Clock)
    monkeypatch.setattr(visual_compare_judge, "datetime", Clock)
    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "VisualCompareJudge", Judge)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "retire_task", lambda _task: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", lambda value, **kwargs: {
        **value, "frames1": [str(frame)], "frames2": [str(frame)],
    })
    return cfg, item, protocol, Clock, calls, requests


def make_task(item, protocol, *, task_id="frozen-time", created_at=CREATED_AT):
    return Task(
        id=task_id, mode="compare", items=[copy.deepcopy(item)], options={},
        evaluation_profile=protocol.id, protocol_manifest=protocol.public_metadata(),
        created_at=created_at.timestamp(), active_runs=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["run", "resume", "retry", "update"])
async def test_compare_clock_and_prompt_survive_restore_and_rerun(evaluation_runtime, path):
    cfg, item, protocol, clock, calls, requests = evaluation_runtime
    original = make_task(item, protocol)
    await runner.run_eval(original, cfg)
    assert not latest_results_by_index(original)[0].get("error")

    snapshot = copy.deepcopy(task_to_snapshot(original))
    assert snapshot["results"][0]["evaluation_datetime"] == CREATED_AT.isoformat()
    restored = _task_from_snapshot(snapshot, original.id)
    restored.active_runs = 1
    clock.current += timedelta(days=365)

    if path == "run":
        await runner.run_eval(restored, cfg)
    elif path == "resume":
        restored.execution_control = {
            "state": "queued", "pending_indexes": [0],
            "resumes": [{"id": "resume_time", "status": "queued", "indexes": [0],
                         "concurrency": 1, "completed": 0, "failed": 0}],
        }
        await runner.run_resume(restored, cfg)
    elif path == "retry":
        restored.results = [{"index": 0, "item_id": "q1", "error": "original failure"}]
        restored.retry_runs["retry_time"] = {
            "retry_id": "retry_time", "status": "queued", "indexes": [0],
            "reasons": {"0": "failed"}, "options": {}, "total": 1,
            "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0,
            "items": {"0": {"status": "queued"}},
        }
        await runner.run_retry(restored, cfg, "retry_time")
    else:
        await runner.run_update_batch(restored, cfg, [(0, restored.items[0])], options={})

    result = latest_results_by_index(restored)[0]
    assert not result.get("error"), result
    assert calls == [CREATED_AT, CREATED_AT]
    assert all(value.utcoffset() == timedelta(hours=8) for value in calls)
    assert len(requests) == 2 and requests[0] == requests[1]
    assert result["evaluation_datetime"] == CREATED_AT.isoformat()


@pytest.mark.asyncio
async def test_new_task_uses_its_own_creation_time(evaluation_runtime):
    cfg, item, protocol, clock, calls, requests = evaluation_runtime
    later = CREATED_AT + timedelta(days=1)
    first = make_task(item, protocol)
    second = make_task(item, protocol, task_id="new-time", created_at=later)
    await runner.run_eval(first, cfg)
    clock.current += timedelta(days=365)
    await runner.run_eval(second, cfg)
    assert calls == [CREATED_AT, later]
    assert len(requests) == 2 and requests[0] != requests[1]
    assert latest_results_by_index(second)[0]["evaluation_datetime"] == later.isoformat()


@pytest.mark.asyncio
async def test_automatic_item_retry_keeps_the_same_clock_and_prompt(evaluation_runtime, monkeypatch):
    cfg, item, protocol, clock, calls, requests = evaluation_runtime

    class FlakyClient(runner.JudgeClient):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            if len(requests) == 1:
                clock.current += timedelta(days=1)
                raise ConnectionError("temporary connection failure")
            return response

    monkeypatch.setattr(runner, "JudgeClient", FlakyClient)
    monkeypatch.setattr(runner, "is_retriable_llm_error", lambda error: isinstance(error, ConnectionError))
    task = make_task(item, protocol)
    await runner.run_eval(task, cfg)
    assert not latest_results_by_index(task)[0].get("error")
    assert calls == [CREATED_AT, CREATED_AT]
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen_sampling", [{}, {"temperature": 0.7, "seed": 7, "top_p": 0.8}])
async def test_legacy_manifest_does_not_inherit_new_sampling(monkeypatch, frozen_sampling):
    captured = []

    class Client:
        def __init__(self, config):
            captured.append(config)

        async def aclose(self):
            pass

    current = JudgeConfig(
        name="judge", model="fake", temperature=0.3, top_p=1.0, top_k=1, seed=42,
        frequency_penalty=0.0, presence_penalty=0.0, max_tokens=1024, n=1,
    )
    task = Task(
        id="legacy-sampling", mode="compare", items=[], options={},
        protocol_manifest={"input_schema_version": "1.1", "judges": [
            {"name": "judge", "model": "old-model", **frozen_sampling},
        ]},
    )
    monkeypatch.setattr(runner, "JudgeClient", Client)
    _one, clients = runner._make_item_evaluator(task, AppConfig(judges=[current]))
    await runner._aclose_judge_clients(clients)
    assert captured[0].model == "old-model"
    for field in ("top_p", "top_k", "seed", "frequency_penalty", "presence_penalty", "max_tokens", "n"):
        assert getattr(captured[0], field) == frozen_sampling.get(field)
    assert captured[0].temperature == frozen_sampling.get("temperature", 0.0)
