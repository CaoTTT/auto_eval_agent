"""A frozen comparison protocol survives restore/retry and cannot be switched in place."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
    resolve_compare_protocol,
)
from auto_eval.web import runner, server
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot, latest_results_by_index


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_id", [
    DEFAULT_COMPARE_PROTOCOL_ID, V03_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
])
async def test_real_retry_uses_frozen_protocol_after_restore(tmp_path, monkeypatch, protocol_id):
    protocol = resolve_compare_protocol(protocol_id)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (40, 40), "white").save(frame)
    profile = VisualModeProfile(name="rich_content", extraction={"algorithm_version": "test"})
    app_cfg = AppConfig(
        judges=[JudgeConfig(name="judge", model="fake")],
        visual_modes={"rich_content": profile},
    )
    item = {"id": "q1", "query": "结构核验", "product_count": 2,
            "video1": "a.mp4", "video2": "b.mp4",
            "frames1": [str(frame)], "frames2": [str(frame)]}
    original = Task(
        id="frozen-retry", mode="compare", items=[item], options={}, status="done",
        evaluation_profile=protocol.id, protocol_manifest=protocol.public_metadata(),
        results=[{"index": 0, "item_id": "q1", "error": "original failure"}],
    )
    restored = _task_from_snapshot(snapshot_payload(task_to_snapshot(original)), original.id)
    retry_id = "retry_protocol"
    # A retry option cannot override the task's selected protocol.
    attempted_override = (
        DEFAULT_COMPARE_PROTOCOL_ID
        if protocol_id == V02_CALIBRATED_COMPARE_PROTOCOL_ID
        else V02_CALIBRATED_COMPARE_PROTOCOL_ID
    )
    restored.retry_runs[retry_id] = {
        "retry_id": retry_id, "status": "queued", "indexes": [0],
        "reasons": {"0": "failed"},
        "options": {"evaluation_profile": attempted_override},
        "total": 1, "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0,
        "items": {"0": {"status": "queued"}},
    }
    restored.active_runs = 1
    calls = []
    closed = []
    prepared = []

    def prepare(item, **kwargs):
        # This protocol test uses synthetic frames, not actual video files.
        prepared.append(item["id"])
        return {**item, "frames1": [str(frame)], "frames2": [str(frame)]}

    class Client:
        def __init__(self, cfg):
            self.cfg, self.model, self.persona = cfg, cfg.model, "测试裁判"

        async def complete(self, system, user, **kwargs):
            calls.append((system, user))
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
            closed.append(True)

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", prepare)
    monkeypatch.setattr(runner, "save_task", lambda _task: True)
    monkeypatch.setattr(runner, "retire_task", lambda _task: None)
    await runner.run_retry(restored, app_cfg, retry_id)

    retry = restored.retry_runs[retry_id]
    assert retry["status"] == "completed", retry
    assert retry["succeeded"] == 1
    assert prepared == ["q1"]
    assert len(calls) == len(closed) == 1
    assert f"qa_competitor_compare/{protocol.standard_version} 标准" in calls[0][0]
    assert ("【思考暴露（内部过程信息泄露）】" in calls[0][0]) == (
        protocol_id == V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID
    )
    result = latest_results_by_index(restored)[0]
    assert "error" not in result
    assert result["standard_version"] == protocol.standard_version
    assert result["evaluation_profile"] == protocol.id
    assert result["bundle_revision"] == protocol.bundle_revision
    assert result["answer1_understanding_score"] == protocol.score_max
    assert result["understanding_rank_groups"] == [["product1"], ["product2"]]
    assert len(result["prompt_sha256"]) == 64
    assert restored.protocol_manifest["bundle_revision"] == protocol.bundle_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen_id,requested_id", [
    (DEFAULT_COMPARE_PROTOCOL_ID, V02_CALIBRATED_COMPARE_PROTOCOL_ID),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, DEFAULT_COMPARE_PROTOCOL_ID),
    (DEFAULT_COMPARE_PROTOCOL_ID, V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, DEFAULT_COMPARE_PROTOCOL_ID),
])
async def test_existing_task_rejects_switching_comparison_profile(monkeypatch, frozen_id, requested_id):
    task = Task(id="frozen", mode="compare", items=[], options={}, status="done",
                evaluation_profile=frozen_id)

    monkeypatch.setattr(server, "get_task", lambda _task_id: task)
    monkeypatch.setattr(server, "cfg", lambda: SimpleNamespace())
    with pytest.raises(HTTPException) as error:
        await server.api_eval_items(
            server.EvalItemsReq(
                task_id=task.id,
                mode="compare", items=[{"id": "q", "query": "q", "video1": "a", "video2": "b"}],
                evaluation_profile=requested_id,
            ),
        )
    assert error.value.status_code == 422
    assert "已有任务的评测协议不可变" in error.value.detail
