"""Execution-wall-clock accounting survives polling, history edits and process loss."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from auto_eval.web import history, runner, server, tasks
from auto_eval.web.tasks import Task, _task_from_snapshot


@pytest.fixture
def clock(monkeypatch):
    state = {"wall": 1000.0, "mono": 50.0}
    monkeypatch.setattr(tasks, "time", SimpleNamespace(
        time=lambda: state["wall"], monotonic=lambda: state["mono"],
    ))
    return state


def task():
    return Task(id="timing", mode="compare", items=[], options={}, created_at=1_700_000_000.0)


def test_execution_clock_excludes_queue_and_is_not_sum_of_case_durations(clock):
    value = task()
    value.status = "queued"
    assert value.timing_snapshot()["elapsed_s"] is None
    value.start_timing()
    clock.update(wall=1012.5, mono=62.5)
    value.results = [{"latency_s": 9.0}, {"latency_s": 10.0}]
    assert value.timing_snapshot()["elapsed_s"] == 12.5
    assert value.timing_snapshot()["started_at"] == 1000.0
    # Wall clock corrections must not distort elapsed time.
    clock.update(wall=900.0, mono=65.0)
    value.finish_timing()
    assert value.timing_snapshot()["elapsed_s"] == 15.0
    assert value.timing_snapshot()["running"] is False


def test_finished_history_and_note_save_keep_original_duration(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    value = task()
    value.start_timing()
    clock.update(wall=1012.5, mono=62.5)
    value.finish_timing()
    value.status = "done"
    assert history.save_task(value)
    stored = history.load_snapshot(value.id)
    restored = _task_from_snapshot(stored, value.id)
    clock.update(wall=99999.0, mono=99999.0)
    restored.note = "Updated long after completion"
    # The background writer passes a frozen SimpleNamespace, not a live Task.
    frozen = SimpleNamespace(**history.task_to_snapshot(restored), id=restored.id)
    assert history.save_task(frozen)
    payload = history.snapshot_payload(history.load_snapshot(value.id))
    assert payload["task_timing"]["elapsed_s"] == 12.5
    assert payload["task_timing"]["finished_at"] == 1012.5
    assert history.list_snapshots()[0]["task_timing"]["elapsed_s"] == 12.5


def test_interrupted_snapshot_freezes_last_measurement_without_counting_downtime(clock):
    value = task()
    value.status = "running"
    value.start_timing()
    clock.update(wall=1030.0, mono=80.0)
    stored = history.task_to_snapshot(value)
    clock.update(wall=9000.0, mono=8050.0)
    restored = _task_from_snapshot(stored, value.id)
    timing = restored.timing_snapshot()
    assert restored.status == "error"
    assert timing["elapsed_s"] == 30.0
    assert timing["finished_at"] == 1030.0
    assert timing["running"] is False
    assert timing["incomplete"] is True


def test_legacy_history_does_not_infer_execution_time_from_created_or_updated(clock):
    stored = {"task_id": "legacy", "status": "done", "created_at": 100.0, "updated_at": 9999.0}
    assert _task_from_snapshot(stored, "legacy").timing_snapshot()["elapsed_s"] is None
    assert history.snapshot_payload(stored)["task_timing"]["elapsed_s"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["done", "error", "cancel"])
async def test_runner_freezes_end_time_for_all_terminal_paths(monkeypatch, clock, outcome):
    value = task()
    value.active_runs = 1
    events = value.subscribe()
    saved = []

    async def persist(current):
        saved.append(current.timing_snapshot())

    async def evaluate(current, cfg):
        clock.update(wall=1020.0, mono=70.0)
        if outcome == "error":
            raise RuntimeError("synthetic failure")
        if outcome == "cancel":
            raise asyncio.CancelledError()

    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "_run", evaluate)
    monkeypatch.setattr(runner, "retire_task", lambda _: None)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner.run_eval(value, None)
    else:
        await runner.run_eval(value, None)
    assert saved[-1]["elapsed_s"] == 20.0
    assert saved[-1]["running"] is False
    assert value.status == ("done" if outcome == "done" else "error")
    messages = []
    while not events.empty():
        messages.append(events.get_nowait())
    assert messages[0]["data"]["task_timing"]["running"] is True
    assert messages[-1]["data"]["task_timing"]["elapsed_s"] == 20.0
    assert messages[-1]["data"]["task_timing"]["running"] is False


@pytest.mark.asyncio
async def test_retry_keeps_original_execution_duration(monkeypatch, clock):
    value = task()
    value.start_timing()
    clock.update(wall=1010.0, mono=60.0)
    value.finish_timing()
    value.status = "done"
    value.active_runs = 1
    value.retry_runs["retry"] = {"indexes": [], "items": {}}

    async def persist(_):
        clock.update(wall=2000.0, mono=1050.0)

    monkeypatch.setattr(runner, "_persist_task", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "_make_item_evaluator", lambda *a, **kw: (None, []))
    monkeypatch.setattr(runner, "retire_task", lambda _: None)
    await runner.run_retry(value, None, "retry")
    assert value.retry_runs["retry"]["status"] == "completed"
    assert value.timing_snapshot()["elapsed_s"] == 10.0
    assert value.timing_snapshot()["finished_at"] == 1010.0
    assert value.timing_snapshot()["running"] is False


@pytest.mark.asyncio
async def test_compact_stream_restores_running_status_and_frozen_done_timing(monkeypatch, clock):
    value = task()
    value.status = "running"
    value.active_runs = 1
    value.start_timing()
    clock.update(wall=1010.0, mono=60.0)
    monkeypatch.setattr(server, "peek_task", lambda *_args, **_kwargs: value)

    def parse(chunk):
        lines = chunk.decode().splitlines() if isinstance(chunk, bytes) else chunk.splitlines()
        return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))

    response = await server.api_stream(value.id, compact=True)
    stream = response.body_iterator
    try:
        event, replay = parse(await anext(stream))
        assert event == "replay_state"
        assert replay["status"] == "running", "a missed start event must not leave the UI queued"
        assert replay["task_timing"] == history.task_to_snapshot(value)["task_timing"]
        assert replay["task_timing"]["elapsed_s"] == 10.0
        assert replay["task_timing"]["running"] is True
        clock.update(wall=1020.0, mono=70.0)
        value.finish_timing()
        value.status = "done"
        value.active_runs = 0
        event, terminal = parse(await anext(stream))
        assert event == "done"
        assert terminal["task_timing"]["elapsed_s"] == 20.0
        assert terminal["task_timing"]["running"] is False
    finally:
        await stream.aclose()

    # Refreshing a finished task replays the same frozen duration as its done event.
    response = await server.api_stream(value.id, compact=True)
    replayed = [parse(chunk) async for chunk in response.body_iterator]
    assert [event for event, _ in replayed] == ["replay_state", "done"]
    assert replayed[0][1]["status"] == "done"
    assert replayed[0][1]["task_timing"] == replayed[1][1]["task_timing"] == terminal["task_timing"]
    assert not value.subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [None, 1])
async def test_history_api_uses_live_timing_and_keeps_legacy_duration_unknown(tmp_path, monkeypatch, clock, page):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    registry = tasks.OrderedDict()
    monkeypatch.setattr(tasks, "TASKS", registry)
    monkeypatch.setattr(server, "TASKS", registry)
    value = task()
    value.status = "running"
    value.active_runs = 1
    value.start_timing()
    clock.update(wall=1005.0, mono=55.0)
    assert history.save_task(value)
    registry[value.id] = value
    (tmp_path / "legacy.json").write_text(json.dumps({
        "task_id": "legacy", "mode": "compare", "status": "done", "items": [],
        "created_at": 1_700_000_000, "updated_at": 1_700_009_999,
    }), encoding="utf-8")
    disk_row = next(row for row in history.list_snapshots() if row["task_id"] == value.id)
    assert disk_row["task_timing"]["elapsed_s"] == 5.0
    assert disk_row["task_timing"]["incomplete"] is True

    clock.update(wall=1020.0, mono=70.0)
    rows = {row["task_id"]: row for row in (await server.api_history(page=page))["items"]}
    live = rows[value.id]
    assert live["status"] == "running"
    assert live["task_timing"]["elapsed_s"] == 20.0
    assert live["task_timing"]["running"] is True
    assert live["task_timing"]["incomplete"] is False
    assert live["task_timing"] == server.api_history_detail(value.id)["task_timing"]
    assert live["task_timing"] == history.task_to_snapshot(value)["task_timing"]
    assert rows["legacy"]["task_timing"]["elapsed_s"] is None
    assert server.api_history_detail("legacy")["task_timing"]["elapsed_s"] is None
