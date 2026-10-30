"""Disabled choices remain visible and frozen tasks retain their original protocol."""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.judge_profiles import resolve_new_runtime
from auto_eval.judges.compare_protocols import (
    V03_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
    QA_CHAT_COMPARE_PROTOCOL_ID,
    list_compare_protocols,
    resolve_compare_protocol,
)
from auto_eval.web import server
from auto_eval.web.tasks import Task


DISABLED_IDS = (
    V03_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
    QA_CHAT_COMPARE_PROTOCOL_ID,
)


def compare_item():
    return {"id": "one", "query": "问题", "video1": "a.mp4", "video2": "b.mp4",
            "context1": "产品1背景", "context2": "产品2背景"}


def test_config_keeps_disabled_choices_and_does_not_change_frozen_protocol_metadata(monkeypatch):
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[JudgeConfig(name="judge")]))
    profiles = server.api_config()["evaluation_profiles"]
    assert len(profiles) == 13
    assert {profile["id"] for profile in profiles if not profile["selectable"]} == set(DISABLED_IDS)
    for profile in profiles:
        protocol = resolve_compare_protocol(profile["id"])
        assert {key: value for key, value in profile.items() if key != "selectable"} == protocol.public_metadata()
        assert "selectable" not in protocol.public_metadata()
        assert "selectable" not in protocol.request_metadata()
    assert all(profile.selectable for profile in list_compare_protocols() if profile.id not in DISABLED_IDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_id", DISABLED_IDS)
@pytest.mark.parametrize("endpoint", ["preflight", "upsert"])
async def test_disabled_protocol_cannot_bypass_selection_through_new_task_endpoints(monkeypatch, protocol_id, endpoint):
    app_cfg = AppConfig(judges=[JudgeConfig(name="judge")])
    monkeypatch.setattr(server, "cfg", lambda: app_cfg)
    monkeypatch.setattr(server, "get_task", lambda _task_id: None)
    monkeypatch.setattr(server, "new_task", lambda *args, **kwargs: pytest.fail("disabled choice created a task"))
    with pytest.raises(HTTPException) as error:
        if endpoint == "preflight":
            await server.api_compare_preflight(server.EvalReq(
                mode="compare", items=[compare_item()], evaluation_profile=protocol_id,
            ))
        else:
            await server.api_eval_items(server.EvalItemsReq(
                task_id="disabled-new", mode="compare", items=[compare_item()], evaluation_profile=protocol_id,
            ))
    assert error.value.status_code == 422
    assert "暂不可用于新建任务" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_id", DISABLED_IDS)
async def test_existing_disabled_protocol_task_can_still_append_items(monkeypatch, protocol_id):
    app_cfg = AppConfig(judges=[JudgeConfig(name="judge", model="fake")])
    runtime, _judges = resolve_new_runtime(app_cfg, {})
    protocol = resolve_compare_protocol(protocol_id)
    manifest = protocol.public_metadata()
    frozen_manifest = deepcopy(manifest)
    task = Task(id="disabled-existing", mode="compare", items=[], options={}, status="done",
                evaluation_profile=protocol_id, protocol_manifest=manifest, judge_runtime=runtime)
    monkeypatch.setattr(server, "cfg", lambda: app_cfg)
    monkeypatch.setattr(server, "get_task", lambda _task_id: task)
    monkeypatch.setattr(server, "queue_task_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "spawn_background", lambda coroutine: coroutine.close())
    result = await server.api_eval_items(server.EvalItemsReq(
        task_id=task.id, items=[compare_item()], evaluation_profile=protocol_id,
    ))
    assert result["created"] is False
    assert result["added_ids"] == ["one"]
    assert result["evaluation_profile"] == protocol_id
    assert task.protocol_manifest == frozen_manifest
