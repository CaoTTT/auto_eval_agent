import asyncio

import pytest

from auto_eval.timing import (
    StageTimings, collect_timings, current_timings, record_model_attempt, timing_span,
)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_nested_admission_and_model_times_partition_wall_clock():
    clock = Clock()
    collector = StageTimings(clock=clock)
    with collect_timings(collector):
        with timing_span("media_queue"):
            clock.now += 100
        with timing_span("media"):
            clock.now += 10
        with timing_span("model"):
            with timing_span("request_wait"):
                clock.now += 1000
                with timing_span("request_wait"):
                    clock.now += 10
            record_model_attempt()
            clock.now += 70
        with timing_span("retry_wait"):
            clock.now += 5
        clock.now += 5
    result = collector.snapshot()
    assert result["total_s"] == 1200
    assert result["model_s"] == 70
    assert result["request_wait_s"] == 1010
    assert result["media_s"] == 10
    assert result["media_queue_s"] == 100
    assert result["other_s"] == result["retry_wait_s"] == 5
    assert sum(result[f"{stage}_s"] for stage in (
        "media", "media_queue", "request_wait", "model", "retry_wait", "other",
    )) == result["total_s"]
    assert result["attempts"] == 1
    assert result["active_stage"] is None and result["finished"]
    clock.now += 300
    assert collector.finish()["total_s"] == 1200
    assert current_timings() is None


@pytest.mark.asyncio
async def test_separate_cases_and_thread_context_keep_independent_timings():
    async def case(delay):
        clock = Clock()
        collector = StageTimings(clock=clock)
        with collect_timings(collector):
            with timing_span("media"):
                def work():
                    assert current_timings()["active_stage"] == "media"
                    clock.now += delay
                await asyncio.to_thread(work)
            record_model_attempt()
        return collector.snapshot()

    first, second = await asyncio.gather(case(3), case(8))
    assert first["media_s"] == first["total_s"] == 3
    assert second["media_s"] == second["total_s"] == 8
    assert first["attempts"] == second["attempts"] == 1
    assert current_timings() is None


def test_stage_boundaries_report_live_wait_and_finish_after_cancellation():
    clock, snapshots = Clock(), []
    collector = StageTimings(clock=clock, on_change=snapshots.append, started=False)
    clock.now += 99  # Waiting for case capacity is outside this timer.
    with pytest.raises(asyncio.CancelledError):
        with collect_timings(collector):
            collector.start()
            with timing_span("model"), timing_span("request_wait"):
                clock.now += 1200
                assert collector.snapshot()["active_stage"] == "request_wait"
                assert collector.snapshot()["request_wait_s"] == 1200
                raise asyncio.CancelledError()
    assert collector.snapshot()["total_s"] == 1200
    assert collector.snapshot()["model_s"] == 0
    assert snapshots[-1]["finished"]
    assert any(snapshot["active_stage"] == "request_wait" for snapshot in snapshots)


def test_broken_observer_cannot_abort_evaluation():
    def observer(_):
        raise RuntimeError("synthetic SSE observer error")
    collector = StageTimings(on_change=observer)
    with collect_timings(collector), timing_span("model"):
        record_model_attempt()
    assert collector.snapshot()["attempts"] == 1
    assert current_timings() is None


def test_reconnect_refreshes_long_running_wait_without_mutating_history():
    from auto_eval.web.runner import _track_live_timings, snapshot_item_progress
    from auto_eval.web.tasks import Task

    clock = Clock()
    collector = StageTimings(clock=clock)
    task = Task(id="live-timing", mode="compare", items=[], options={})
    with collect_timings(collector), _track_live_timings(task.id, 0, "request", collector):
        with timing_span("request_wait"):
            task.item_progress["0"] = {"request_id": "request", "timings": collector.snapshot()}
            clock.now += 1200
            live = snapshot_item_progress(task)
            assert live["0"]["timings"]["request_wait_s"] == 1200
            assert task.item_progress["0"]["timings"]["request_wait_s"] == 0
    assert snapshot_item_progress(task)["0"]["timings"]["request_wait_s"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_runner_publishes_and_persists_timings_for_success_and_error(monkeypatch, failure):
    from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
    from auto_eval.web import runner
    from auto_eval.web.tasks import Task

    class Client:
        def __init__(self, cfg):
            self.cfg = cfg

    async def evaluate(*args, **kwargs):
        with timing_span("model"):
            with timing_span("request_wait"):
                await asyncio.sleep(.01)
            record_model_attempt()
            await asyncio.sleep(.01)
        if failure:
            raise ValueError("synthetic result failure")
        return {"latency_s": .01}

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *a: None)
    monkeypatch.setattr(runner, "VisualCompareJudge", lambda *a: None)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **kw: None)
    task = Task(id="timings", mode="compare", items=[{
        "id": "case", "query": "q", "frames1": ["a"], "frames2": ["b"],
    }], options={})
    cfg = AppConfig(judges=[JudgeConfig(name="fake")],
                    visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})})
    one, _ = runner._make_item_evaluator(task, cfg)
    result = await one(0, task.items[0])
    assert bool(result.get("error")) == failure
    assert result["timings"]["finished"]
    assert result["timings"]["attempts"] == 1
    assert result["timings"]["request_wait_s"] >= .009
    assert result["timings"]["model_s"] >= .009
    assert result["total_s"] >= .019
    assert task.item_progress["0"]["timings"]["finished"]
    assert task.results[0]["timings"] == result["timings"]
    assert not any(event.get("timing_update") for event in task.progress_events["0"])
    sequences = [event["sequence"] for event in task.progress_events["0"]]
    assert sequences == sorted(set(sequences))
