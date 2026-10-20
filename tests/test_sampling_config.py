"""Sampling validation and historical task isolation, without model calls."""
import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from auto_eval.config import AppConfig, JudgeConfig, load_config
from auto_eval.judge_profiles import legacy_runtime, resolve_new_runtime, restore_runtime
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import server
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot


SAMPLING = {
    "temperature": 0,
    "top_p": 1,
    "top_k": 1,
    "seed": 0,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "max_tokens": 8192,
    "n": 1,
}
OPTIONAL_SAMPLING = set(SAMPLING) - {"temperature"}


def app_config(**sampling):
    return AppConfig(judges=[JudgeConfig(
        name="judge_2", model="test-model", base_url="https://model.invalid/v1",
        api_key_env="TEST_MODEL_API_KEY", enable_thinking=False, **sampling,
    )])


def test_optional_sampling_defaults_preserve_old_gateway_requests():
    judge = JudgeConfig(name="old-gateway")
    assert judge.temperature == 0
    assert all(getattr(judge, field) is None for field in OPTIONAL_SAMPLING)


@pytest.mark.parametrize("field,value", [
    ("temperature", -0.1), ("temperature", 2.1), ("temperature", float("nan")),
    ("top_p", -0.1), ("top_p", 1.1), ("top_p", float("inf")),
    ("top_k", 0), ("top_k", 1.5), ("top_k", True),
    ("seed", 1.5), ("seed", True), ("seed", "42"),
    ("frequency_penalty", -2.1), ("frequency_penalty", 2.1),
    ("presence_penalty", -2.1), ("presence_penalty", 2.1),
    ("max_tokens", 0), ("max_tokens", -1), ("max_tokens", 1.5),
    ("n", 0), ("n", 2), ("n", True),
])
def test_invalid_sampling_is_rejected_before_a_request(field, value):
    with pytest.raises(ValidationError):
        JudgeConfig(name="invalid", **{field: value})


@pytest.mark.parametrize("temperature,top_p,penalty", [(0, 0, -2), (2, 1, 2)])
def test_sampling_boundaries_and_zero_seed_are_valid(temperature, top_p, penalty):
    judge = JudgeConfig(name="valid", **{
        **SAMPLING, "temperature": temperature, "top_p": top_p,
        "frequency_penalty": penalty, "presence_penalty": penalty,
    })
    assert judge.seed == 0
    assert judge.top_p == top_p
    assert judge.frequency_penalty == penalty and judge.presence_penalty == penalty


def test_deployment_defaults_use_stable_sampling_and_explicit_thinking_mode():
    config_dir = Path(__file__).resolve().parents[1] / "config"
    judge = load_config(config_dir).judges[0]
    expected = {**SAMPLING, "seed": 42, "presence_penalty": None}
    assert {field: getattr(judge, field) for field in expected} == expected
    assert judge.enable_thinking is False

    example = yaml.safe_load((config_dir / "judges.bailian.example.yaml").read_text(encoding="utf-8"))
    bailian = JudgeConfig.model_validate(example["judges"][0])
    assert bailian.top_k == 1 and bailian.seed == 42
    assert bailian.presence_penalty == 0 and bailian.max_tokens == 8192
    assert bailian.frequency_penalty is None
    assert bailian.enable_thinking is True


def test_sampling_survives_task_serialization_and_current_config_changes():
    app = app_config(**SAMPLING)
    runtime, _ = resolve_new_runtime(app, {})
    task = Task(id="sampling", mode="compare", items=[], options={}, judge_runtime=runtime)
    restored_task = _task_from_snapshot(snapshot_payload(task_to_snapshot(task)), task.id)
    before = copy.deepcopy(restored_task.judge_runtime)
    app.judges[0] = JudgeConfig.model_validate({
        **app.judges[0].model_dump(), "temperature": 1, "top_p": .5, "top_k": 20,
        "seed": 42, "frequency_penalty": 1, "presence_penalty": 1,
        "max_tokens": 1024, "n": None, "enable_thinking": True,
    })
    actual = restore_runtime(app, restored_task.judge_runtime)[0]
    assert {field: getattr(actual, field) for field in SAMPLING} == SAMPLING
    assert actual.enable_thinking is False
    assert restored_task.judge_runtime == before


def test_old_runtime_snapshot_does_not_inherit_new_sampling_defaults():
    app = app_config(**SAMPLING)
    runtime = {"version": 1, "profile_id": "configured_judge_2", "judges": [{
        "name": "judge_2", "model": "test-model", "temperature": .25,
        "base_url": "https://model.invalid/v1", "api_key_env": "TEST_MODEL_API_KEY",
    }]}
    actual = restore_runtime(app, runtime)[0]
    assert actual.temperature == .25
    assert all(getattr(actual, field) is None for field in OPTIONAL_SAMPLING)


@pytest.mark.parametrize("recorded", [{}, SAMPLING])
def test_legacy_migration_preserves_recorded_sampling_and_omits_unknown_fields(recorded):
    app = app_config(**{**SAMPLING, "temperature": 1, "seed": 42})
    task = Task(id="legacy-sampling", mode="compare", items=[], options={}, protocol_manifest={
        "judges": [{"name": "judge_2", "model": "test-model", **recorded}],
    })
    actual = legacy_runtime(app, task)["judges"][0]
    expected = recorded or {"temperature": 0, **dict.fromkeys(OPTIONAL_SAMPLING)}
    assert {field: actual[field] for field in SAMPLING} == expected
    assert actual["enable_thinking"] is None


def test_protocol_manifest_records_effective_frozen_sampling():
    app = app_config(**SAMPLING)
    _, resolved = resolve_new_runtime(app, {})
    app.judges[0].top_k = 20
    app.judges[0].max_tokens = 1024
    manifest = server._protocol_manifest(resolve_compare_protocol(None), app, {}, resolved)
    actual = manifest["judges"][0]
    assert {field: actual[field] for field in SAMPLING} == SAMPLING
    assert actual["enable_thinking"] is False
    current_manifest = server._protocol_manifest(resolve_compare_protocol(None), app, {})
    assert current_manifest != manifest
