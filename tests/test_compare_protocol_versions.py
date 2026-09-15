import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    VisualCompareObservationV02,
    list_compare_protocols,
    resolve_compare_protocol,
)
from auto_eval.judges.visual_compare_judge import visual_compare_result_fields
from auto_eval.schema import VisualCompareObservation
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot
from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.web import server as server_module


def _observation_data(score: int, response_gate: str = "pass") -> dict:
    return {
        "product_count": 2,
        "answer1_response_gate": response_gate,
        "answer2_response_gate": "pass",
        "answer1_safety_gate": "pass",
        "answer2_safety_gate": "pass",
        "understanding_applicable": True,
        "understanding_verification_status": "not_required",
        "answer1_understanding_score": score,
        "answer2_understanding_score": 3,
    }


def test_registry_defaults_to_stable_v02_and_exposes_experimental_versions():
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    assert {profile.id for profile in list_compare_protocols()} == {
        DEFAULT_COMPARE_PROTOCOL_ID,
        V03_COMPARE_PROTOCOL_ID,
        V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    }
    assert resolve_compare_protocol(V03_COMPARE_PROTOCOL_ID).status == "experimental"
    calibrated = resolve_compare_protocol(V02_CALIBRATED_COMPARE_PROTOCOL_ID)
    assert calibrated.status == "experimental"
    assert calibrated.public_metadata()["input_modalities"] == ["text", "text_image"]
    assert calibrated.observation_model is VisualCompareObservationV02
    with pytest.raises(ValueError, match="未知评测协议"):
        resolve_compare_protocol("qa_competitor_compare@9.9")


def test_each_protocol_uses_its_own_score_schema_and_gate_rules():
    v02 = VisualCompareObservationV02.model_validate(_observation_data(5, "unclear"))
    v02_result = visual_compare_result_fields(v02, DEFAULT_COMPARE_PROTOCOL_ID)
    assert v02_result["standard_version"] == "0.2-simplified"
    assert v02_result["answer1_understanding_score"] is None

    with pytest.raises(ValidationError):
        VisualCompareObservation.model_validate(_observation_data(5))

    v03 = VisualCompareObservation.model_validate(_observation_data(3, "unclear"))
    v03_result = visual_compare_result_fields(v03, V03_COMPARE_PROTOCOL_ID)
    assert v03_result["standard_version"] == "0.3"
    assert v03_result["answer1_understanding_score"] == 3


def test_task_snapshot_freezes_protocol_and_legacy_v03_is_inferred():
    protocol = resolve_compare_protocol(DEFAULT_COMPARE_PROTOCOL_ID)
    task = Task(
        id="versioned-task",
        mode="compare",
        items=[],
        options={},
        evaluation_profile=protocol.id,
        protocol_manifest=protocol.public_metadata(),
    )
    payload = snapshot_payload(task_to_snapshot(task))
    assert payload["evaluation_profile"] == DEFAULT_COMPARE_PROTOCOL_ID
    assert payload["protocol_manifest"]["standard_version"] == "0.2-simplified"

    restored = _task_from_snapshot(
        {
            "task_id": "legacy-v03",
            "mode": "compare",
            "items": [],
            "options": {},
            "results": [{"index": 0, "standard_version": "0.3"}],
        },
        "legacy-v03",
    )
    assert restored.evaluation_profile == V03_COMPARE_PROTOCOL_ID


@pytest.mark.asyncio
async def test_eval_api_freezes_selected_protocol_on_new_task(monkeypatch):
    app_cfg = AppConfig(judges=[JudgeConfig(name="judge")])
    monkeypatch.setitem(server_module._state, "cfg", app_cfg)
    created = []

    def fake_new_task(mode, items, options, dataset_name="", **kwargs):
        task = Task(
            id=f"task-{len(created)}",
            mode=mode,
            items=items,
            options=options,
            dataset_name=dataset_name,
            evaluation_profile=kwargs.get("evaluation_profile", ""),
            protocol_manifest=kwargs.get("protocol_manifest") or {},
        )
        created.append(task)
        return task

    monkeypatch.setattr(server_module, "new_task", fake_new_task)
    monkeypatch.setattr(server_module.EVAL_SCHEDULER, "enqueue", lambda *_: 1)
    item = {"query": "q", "video1": "a.mp4", "video2": "b.mp4"}

    default_response = await server_module.api_eval(
        server_module.EvalReq(mode="compare", items=[item])
    )
    assert default_response["evaluation_profile"] == DEFAULT_COMPARE_PROTOCOL_ID
    assert created[-1].protocol_manifest["status"] == "stable"

    v03_response = await server_module.api_eval(
        server_module.EvalReq(
            mode="compare",
            items=[item],
            evaluation_profile=V03_COMPARE_PROTOCOL_ID,
        )
    )
    assert v03_response["evaluation_profile"] == V03_COMPARE_PROTOCOL_ID
    assert created[-1].protocol_manifest["score_range"] == [0, 3]

    calibrated_response = await server_module.api_eval(
        server_module.EvalReq(
            mode="compare",
            items=[item],
            evaluation_profile=V02_CALIBRATED_COMPARE_PROTOCOL_ID,
        )
    )
    assert calibrated_response["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
    manifest = created[-1].protocol_manifest
    assert manifest["standard_version"] == "0.2-simplified-calibrated"
    assert manifest["bundle_revision"] == "0.2.2"
    assert manifest["score_range"] == [1, 5]
    public = server_module.api_config()["evaluation_profiles"]
    assert [p["id"] for p in public] == [
        DEFAULT_COMPARE_PROTOCOL_ID, V03_COMPARE_PROTOCOL_ID,
        V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    ]
    assert [p["id"] for p in public if p["status"] == "stable"] == [DEFAULT_COMPARE_PROTOCOL_ID]

    with pytest.raises(HTTPException) as exc_info:
        await server_module.api_eval(
            server_module.EvalReq(
                mode="compare",
                items=[item],
                evaluation_profile="qa_competitor_compare@9.9",
            )
        )
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("response_gate", ["pass", "fail", "unclear"])
def test_calibrated_preserves_v02_gates_and_separate_version(response_gate):
    protocol = resolve_compare_protocol(V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2.2")
    data = _observation_data(5, response_gate)
    result = visual_compare_result_fields(protocol.observation_model.model_validate(data), protocol)
    assert result["answer1_understanding_score"] == (5 if response_gate == "pass" else None)
    assert result["answer2_understanding_score"] == 3
    assert result["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
    assert result["standard_version"] == "0.2-simplified-calibrated"
    assert result["bundle_revision"] == "0.2.2"
    assert result["overall_winner"] is None
    for score in (0, 6):
        with pytest.raises(ValidationError):
            protocol.observation_model.model_validate(_observation_data(score))


@pytest.mark.parametrize("revision", ["0.2.0", "0.2.1", "0.3.0", "0.3.1", "unknown"])
def test_calibrated_cannot_restore_another_protocols_revision(revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(V02_CALIBRATED_COMPARE_PROTOCOL_ID, revision)


@pytest.mark.parametrize("protocol_id,revision", [
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.0"),
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.1"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.0"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.1"),
])
def test_existing_protocol_revisions_still_restore(protocol_id, revision):
    restored = resolve_compare_protocol(protocol_id, revision)
    assert restored.id == protocol_id
    assert restored.bundle_revision == revision
