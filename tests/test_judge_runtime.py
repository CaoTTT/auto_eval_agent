import copy
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auto_eval.config import AppConfig, JudgeConfig, JudgeModelProfile, RateLimitConfig, VisualModeProfile
from auto_eval.judge_profiles import (
    check_frozen_options, default_profile_id, model_profiles, resolve_new_runtime, restore_runtime, legacy_runtime,
)
from auto_eval.web import server, tasks
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot


def config():
    return AppConfig(judges=[JudgeConfig(
        name="judge_2", display="终端用户", model="qwen3.5-397b-a17b",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key_env="DASHSCOPE_API_KEY",
        vl_high_resolution_images=True,
    )], visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})})


@pytest.mark.parametrize("profile,model", [
    ("bailian_qwen35_397b", "qwen3.5-397b-a17b"), ("bailian_qwen38_flash", "qwen3.8-flash"),
])
@pytest.mark.parametrize("thinking", [False, True])
def test_all_four_combinations_are_isolated_and_frozen(profile, model, thinking, monkeypatch):
    app = config()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-never-persist")
    before = app.model_dump()
    runtime, judges = resolve_new_runtime(app, {"judge_model_profile": profile, "enable_thinking": thinking})
    assert judges[0].model == model
    assert judges[0].enable_thinking is thinking
    assert judges[0].vl_high_resolution_images is True
    assert app.model_dump() == before
    assert "secret-never-persist" not in str(runtime)
    original = Task(id="frozen", mode="rich_content", items=[], options={}, judge_runtime=runtime)
    restored = _task_from_snapshot(snapshot_payload(task_to_snapshot(original)), "frozen")
    app.judges[0].model = "replacement-model"
    app.judges[0].enable_thinking = not thinking
    app.judges[0].base_url = "https://replacement.invalid/v1"
    actual = restore_runtime(app, restored.judge_runtime)[0]
    assert actual.model == model and actual.enable_thinking is thinking
    assert actual.base_url == before["judges"][0]["base_url"]
    assert actual.api_key() == "secret-never-persist"


def test_legacy_bailian_auto_discovers_models_and_retains_default():
    app = config()
    assert [p.model for p in model_profiles(app)] == ["qwen3.5-397b-a17b", "qwen3.8-flash"]
    assert default_profile_id(app) == "bailian_qwen35_397b"
    app.judges[0].model = "qwen3.8-flash"
    assert default_profile_id(app) == "bailian_qwen38_flash"


@pytest.mark.asyncio
async def test_old_flash_runtime_uses_current_quota_without_restricting_new_tasks():
    from auto_eval.request_throttle import shared_throttle

    app = config()
    runtime, _ = resolve_new_runtime(app, {"judge_model_profile": "bailian_qwen38_flash", "enable_thinking": False})
    runtime["judges"][0]["rate_limit"] = RateLimitConfig(
        rpm=60, tpm=100_000, rps=1, max_inflight=4,
    ).model_dump()
    before = copy.deepcopy(runtime)
    restored = restore_runtime(app, runtime)[0]
    fresh = resolve_new_runtime(app, {"judge_model_profile": "bailian_qwen38_flash"})[1][0]
    throttle = shared_throttle(restored)
    assert shared_throttle(fresh) is throttle
    assert throttle.snapshot()["request_budget"] == 30_000
    assert throttle.snapshot()["token_budget"] == 20_000_000
    assert throttle.snapshot()["max_inflight"] == 128
    assert restored.enable_thinking is False
    assert runtime == before


@pytest.mark.parametrize("explicit_rate", [None, RateLimitConfig(rpm=120, tpm=200_000, rps=2, max_inflight=16)])
def test_restore_uses_current_admin_quota_even_after_profile_rename(explicit_rate):
    from auto_eval.request_throttle import pacing_config

    app = config()
    runtime, _ = resolve_new_runtime(app, {"judge_model_profile": "bailian_qwen38_flash", "enable_thinking": False})
    app.judge_model_profiles = [JudgeModelProfile(
        id="renamed_flash", display="Flash", judge_name="judge_2", model="qwen3.8-flash",
        supports_thinking=True, default_enable_thinking=True, rate_limit=explicit_rate,
    )]
    app.judges[0].temperature = .7
    restored = restore_runtime(app, runtime)[0]
    assert restored.rate_limit == explicit_rate
    assert pacing_config(restored)["tpm"] == (explicit_rate.tpm if explicit_rate else 20_000_000)
    assert restored.enable_thinking is False and restored.temperature == 0


@pytest.mark.parametrize("change", ["model", "base_url", "api_key_env"])
def test_restore_does_not_apply_an_unrelated_connection_or_model_quota(change):
    app = config()
    runtime, _ = resolve_new_runtime(app, {"judge_model_profile": "bailian_qwen38_flash"})
    profiles = model_profiles(app)
    app.judge_model_profiles = profiles
    flash = profiles[1]
    flash.rate_limit = RateLimitConfig(rpm=120, tpm=200_000, rps=2, max_inflight=16)
    if change == "model":
        flash.model = "another-model"
    else:
        setattr(app.judges[0], change, "changed")
    restored = restore_runtime(app, runtime)[0]
    assert restored.model_dump() == runtime["judges"][0]


def test_admin_default_is_honored_by_legacy_judge_only_clients():
    app = config()
    app.default_judge_model_profile = "bailian_qwen38_flash"
    runtime, _ = resolve_new_runtime(app, {"judges": ["judge_2"]})
    assert runtime["profile_id"] == "bailian_qwen38_flash"


def test_unrelated_provider_keeps_current_config():
    app = config()
    app.judges[0].base_url = "https://api.siliconflow.cn/v1"
    app.judges[0].model = "Qwen/Qwen3.5-397B-A17B"
    profiles = model_profiles(app)
    assert len(profiles) == 1 and not profiles[0].supports_thinking
    runtime, judges = resolve_new_runtime(app, {})
    assert judges[0].enable_thinking is None
    assert runtime["judges"][0]["model"] == app.judges[0].model


@pytest.mark.parametrize("options", [
    {"judge_model_profile": "not-approved"}, {"enable_thinking": "false"},
    {"enable_thinking": None}, {"enable_thinking": 0}, {"judges": ["missing"]},
    {"judges": ["judge_2", "judge_2"]}, {"judges": "judge_2"},
])
def test_invalid_selection_fails_before_execution(options):
    with pytest.raises(ValueError):
        resolve_new_runtime(config(), options)


@pytest.mark.parametrize("override", [
    {"judge_model_profile": "bailian_qwen38_flash"}, {"enable_thinking": False}, {"judges": []},
])
def test_original_task_inference_options_are_immutable(override):
    runtime, _ = resolve_new_runtime(config(), {})
    task = Task(id="frozen", mode="compare", items=[], options={}, judge_runtime=runtime)
    before = copy.deepcopy(task)
    with pytest.raises(ValueError, match="不可变"):
        check_frozen_options(task, override)
    assert task.judge_runtime == before.judge_runtime
    check_frozen_options(task, {"concurrency": 8, "enable_thinking": True})


def test_unknown_legacy_thinking_cannot_be_overridden():
    task = Task(id="legacy", mode="rich_content", items=[], options={})
    with pytest.raises(ValueError, match="旧任务"):
        check_frozen_options(task, {"enable_thinking": True})
    assert not task.judge_runtime


def test_config_references_are_validated():
    app = config()
    app.judge_model_profiles = [JudgeModelProfile(id="a", display="A", model="m", judge_name="missing")]
    with pytest.raises(ValueError, match="不存在"):
        model_profiles(app)


@pytest.fixture
def api(monkeypatch):
    app = config()
    monkeypatch.setattr(server, "cfg", lambda: app)
    monkeypatch.setattr(tasks, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "EVAL_SCHEDULER", SimpleNamespace(enqueue=lambda *a: 1))
    monkeypatch.setattr(server, "spawn_background", lambda coroutine: coroutine.close())
    monkeypatch.setattr(tasks, "TASKS", OrderedDict())
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rich_content", "compare"])
@pytest.mark.parametrize("upsert", [False, True])
async def test_all_creation_paths_save_effective_runtime(api, mode, upsert):
    item = {"id": "one", "query": "test"}
    if mode == "compare":
        item.update(answer1="A", answer2="B", video1="a.mp4", video2="b.mp4")
    options = {"judge_model_profile": "bailian_qwen38_flash", "enable_thinking": False}
    if upsert:
        response = await server.api_eval_items(server.EvalItemsReq(task_id="created", mode=mode, items=[item], options=options))
    else:
        response = await server.api_eval(server.EvalReq(mode=mode, items=[item], options=options))
    task = tasks.TASKS[response["task_id"]]
    frozen = task.judge_runtime["judges"][0]
    assert frozen["model"] == "qwen3.8-flash" and frozen["enable_thinking"] is False
    if mode == "compare":
        assert task.protocol_manifest["judges"][0]["model"] == frozen["model"]


@pytest.mark.asyncio
async def test_update_rejects_override_before_items_mutation(api):
    response = await server.api_eval(server.EvalReq(mode="rich_content", items=[{"id": "a", "query": "old"}]))
    task = tasks.TASKS[response["task_id"]]
    task.status = "done"
    original = copy.deepcopy(task.items)
    with pytest.raises(HTTPException) as exc:
        await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[{"id": "a", "query": "new"}], options={"enable_thinking": False}))
    assert exc.value.status_code == 422
    assert task.items == original


def test_public_config_has_no_credentials(api):
    public = server.api_config()
    assert len(public["judge_model_profiles"]) == 2
    assert "DASHSCOPE_API_KEY" not in str(public)
    assert "base_url" not in str(public)


def test_known_legacy_model_does_not_inherit_new_model_or_thinking():
    app = config()
    app.judges[0].model = "qwen3.8-flash"
    app.judges[0].enable_thinking = False
    task = Task(id="legacy", mode="compare", items=[], options={}, protocol_manifest={
        "judges": [{"name": "judge_2", "model": "qwen3.5-397b-a17b", "temperature": 0}],
    })
    recovered = legacy_runtime(app, task)
    judge = recovered["judges"][0]
    assert judge["model"] == "qwen3.5-397b-a17b"
    assert judge["enable_thinking"] is None
    assert recovered["migrated_legacy"]


def test_unknown_legacy_model_is_not_guessed_from_current_config():
    task = Task(id="legacy", mode="rich_content", items=[], options={})
    with pytest.raises(ValueError, match="未记录"):
        legacy_runtime(config(), task)


def test_legacy_recorded_null_model_preserves_original_name_fallback():
    task = Task(id="legacy", mode="compare", items=[], options={}, protocol_manifest={
        "judges": [{"name": "judge_2", "model": None}],
    })
    assert legacy_runtime(config(), task)["judges"][0]["model"] == "judge_2"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "retry", "update"])
async def test_legacy_continuation_requires_recorded_model(api, monkeypatch, action):
    task = Task(id="old", mode="rich_content", items=[{"id": "a", "query": "old"}], options={},
                status="done", results=[{"index": 0, "error": "failed"}])
    tasks.TASKS[task.id] = task
    async def get(_):
        return task
    monkeypatch.setattr(server, "get_task_async", get)
    with pytest.raises(HTTPException) as exc:
        if action == "resume":
            await server.api_resume(task.id, server.ResumeReq(concurrency=4, include_failed=True))
        elif action == "retry":
            await server.api_retry_failed(task.id, server.RetryReq())
        else:
            await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[{"id": "a", "query": "new"}]))
    assert exc.value.status_code == 422 and "未记录" in exc.value.detail
    assert task.items[0]["query"] == "old" and not task.judge_runtime
