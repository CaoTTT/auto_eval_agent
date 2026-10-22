import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID,
    V02_PLUS_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
    QA_CHAT_COMPARE_PROTOCOL_ID,
    QA_CHAT_V2_COMPARE_PROTOCOL_ID,
    TIMELINESS_COMPARE_PROTOCOL_ID,
    V1022_COMPARE_PROTOCOL_ID,
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
        V02_PLUS_COMPARE_PROTOCOL_ID,
        V03_COMPARE_PROTOCOL_ID,
        V02_CALIBRATED_COMPARE_PROTOCOL_ID,
        V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
        QA_CHAT_COMPARE_PROTOCOL_ID,
        QA_CHAT_V2_COMPARE_PROTOCOL_ID,
        TIMELINESS_COMPARE_PROTOCOL_ID,
        V1022_COMPARE_PROTOCOL_ID,
    }
    assert resolve_compare_protocol(V03_COMPARE_PROTOCOL_ID).status == "experimental"
    plus = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID)
    assert plus.status == "stable"
    assert plus.bundle_revision == "0.2.2"
    assert plus.observation_model is VisualCompareObservationV02
    assert plus.require_response_pass is True
    calibrated = resolve_compare_protocol(V02_CALIBRATED_COMPARE_PROTOCOL_ID)
    assert calibrated.status == "experimental"
    assert calibrated.public_metadata()["input_modalities"] == ["text", "text_image"]
    assert calibrated.observation_model is VisualCompareObservationV02
    thinking_exposure = resolve_compare_protocol(V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID)
    assert thinking_exposure.status == "experimental"
    assert thinking_exposure.display == "V0.2 简化版·思考暴露优化（可用）"
    assert thinking_exposure.public_metadata()["input_modalities"] == ["text", "text_image"]
    assert thinking_exposure.observation_model is VisualCompareObservationV02
    assert thinking_exposure.require_response_pass is True
    assert thinking_exposure.system_template is not resolve_compare_protocol(None).system_template
    assert thinking_exposure.user_template is not resolve_compare_protocol(None).user_template
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
    item = {"query": "q", "video1": "a.mp4", "video2": "b.mp4",
            "context1": "产品1测试背景", "context2": "产品2测试背景"}

    default_response = await server_module.api_eval(
        server_module.EvalReq(mode="compare", items=[item])
    )
    assert default_response["evaluation_profile"] == DEFAULT_COMPARE_PROTOCOL_ID
    assert created[-1].protocol_manifest["status"] == "stable"

    plus_response = await server_module.api_eval(
        server_module.EvalReq(mode="compare", items=[item], evaluation_profile=V02_PLUS_COMPARE_PROTOCOL_ID)
    )
    assert plus_response["evaluation_profile"] == V02_PLUS_COMPARE_PROTOCOL_ID
    assert created[-1].protocol_manifest["bundle_revision"] == "0.2.2"
    assert created[-1].protocol_manifest["score_range"] == [1, 5]

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
    assert manifest["bundle_revision"] == "0.2.3"
    assert manifest["score_range"] == [1, 5]
    thinking_exposure_response = await server_module.api_eval(
        server_module.EvalReq(
            mode="compare",
            items=[item],
            evaluation_profile=V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
        )
    )
    assert thinking_exposure_response["evaluation_profile"] == V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID
    manifest = created[-1].protocol_manifest
    assert manifest["standard_version"] == "0.2-simplified-thinking-exposure"
    assert manifest["bundle_revision"] == "0.2.3"
    assert manifest["score_range"] == [1, 5]
    assert manifest["status"] == "experimental"
    public = server_module.api_config()["evaluation_profiles"]
    assert [p["id"] for p in public] == [
        DEFAULT_COMPARE_PROTOCOL_ID, V02_PLUS_COMPARE_PROTOCOL_ID, V03_COMPARE_PROTOCOL_ID,
        V02_CALIBRATED_COMPARE_PROTOCOL_ID,
        V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID,
        QA_CHAT_COMPARE_PROTOCOL_ID,
        QA_CHAT_V2_COMPARE_PROTOCOL_ID,
        TIMELINESS_COMPARE_PROTOCOL_ID,
        V1022_COMPARE_PROTOCOL_ID,
    ]
    assert [p["id"] for p in public if p["status"] == "stable"] == [
        DEFAULT_COMPARE_PROTOCOL_ID, V02_PLUS_COMPARE_PROTOCOL_ID,
    ]
    timeliness_response = await server_module.api_eval(
        server_module.EvalReq(
            mode="compare", items=[item], evaluation_profile=TIMELINESS_COMPARE_PROTOCOL_ID,
        )
    )
    assert timeliness_response["evaluation_profile"] == TIMELINESS_COMPARE_PROTOCOL_ID
    manifest = created[-1].protocol_manifest
    assert manifest["bundle_revision"] == "1.0.2"
    assert manifest["result_schema_version"] == "timeliness-result-3"
    assert manifest["score_range"] == [1, 3]
    assert [dim["key"] for dim in manifest["dimensions"]] == ["understanding", "authority", "timeliness"]

    with pytest.raises(HTTPException) as exc_info:
        await server_module.api_eval(
            server_module.EvalReq(
                mode="compare",
                items=[item],
                evaluation_profile="qa_competitor_compare@9.9",
            )
        )
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("protocol_id,standard_version,revision", [
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2-simplified-calibrated", "0.2.2"),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2-simplified-calibrated", "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2-simplified-thinking-exposure", "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2-simplified-thinking-exposure", "0.2.4"),
])
@pytest.mark.parametrize("response_gate", ["pass", "fail", "unclear"])
def test_experimental_v02_preserves_gates_and_separate_version(
    protocol_id, standard_version, revision, response_gate
):
    protocol = resolve_compare_protocol(protocol_id, revision)
    data = _observation_data(5, response_gate)
    result = visual_compare_result_fields(protocol.observation_model.model_validate(data), protocol)
    assert result["answer1_understanding_score"] == (5 if response_gate == "pass" else None)
    assert result["answer2_understanding_score"] == 3
    assert result["evaluation_profile"] == protocol_id
    assert result["standard_version"] == standard_version
    assert result["bundle_revision"] == revision
    assert result["overall_winner"] is None
    for score in (0, 6):
        with pytest.raises(ValidationError):
            protocol.observation_model.model_validate(_observation_data(score))


@pytest.mark.parametrize("revision", ["0.2.0", "0.2.1", "0.2.4", "0.3.0", "0.3.1", "unknown"])
def test_calibrated_cannot_restore_another_protocols_revision(revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(V02_CALIBRATED_COMPARE_PROTOCOL_ID, revision)


@pytest.mark.parametrize("revision", ["0.2.0", "0.2.1", "0.2.2", "0.3.0", "0.3.1", "unknown"])
def test_thinking_exposure_cannot_restore_another_protocols_revision(revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, revision)


@pytest.mark.parametrize("protocol_id", [DEFAULT_COMPARE_PROTOCOL_ID, V03_COMPARE_PROTOCOL_ID])
@pytest.mark.parametrize("revision", ["0.2.3", "0.2.4"])
def test_existing_protocols_cannot_restore_thinking_exposure_revision(protocol_id, revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(protocol_id, revision)


@pytest.mark.parametrize("protocol_id,revision", [
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.0"),
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.1"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.0"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.1"),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2.2"),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2.4"),
    (QA_CHAT_COMPARE_PROTOCOL_ID, "1.0.0"),
    (QA_CHAT_V2_COMPARE_PROTOCOL_ID, "1.1.0"),
])
def test_existing_protocol_revisions_still_restore(protocol_id, revision):
    restored = resolve_compare_protocol(protocol_id, revision)
    assert restored.id == protocol_id
    assert restored.bundle_revision == revision


@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_thinking_exposure_restores_real_frozen_templates(product_count, evidence_mode):
    from auto_eval.judges.visual_compare_prompt_v02_thinking_exposure import (
        VISUAL_COMPARE_SYSTEM as frozen_system,
        VISUAL_COMPARE_USER as frozen_user,
    )

    latest = resolve_compare_protocol(V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID)
    frozen = resolve_compare_protocol(V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2.4")
    assert latest.bundle_revision == "0.2.3"
    assert frozen.bundle_revision == "0.2.4"
    assert frozen.system_template is frozen_system
    assert frozen.user_template is frozen_user
    assert frozen.observation_model is latest.observation_model
    assert frozen.require_response_pass is latest.require_response_pass
    assert frozen.public_metadata()["input_modalities"] == ["text", "text_image"]
    kwargs = dict(persona="test", product_count=product_count, evidence_mode=evidence_mode)
    marker = "定位候选→语义分类→最终保留取证→Gate决策"
    assert marker not in latest.system_template.render(**kwargs)
    assert marker in frozen.system_template.render(**kwargs)
    assert marker not in latest.user_template.render(**kwargs)
    assert marker in frozen.user_template.render(**kwargs)
    assert resolve_compare_protocol(V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID) is latest


def test_thinking_exposure_ui_only_exposes_r023():
    from auto_eval.judges.visual_compare_prompt_v02_thinking_exposure_r023 import (
        VISUAL_COMPARE_SYSTEM, VISUAL_COMPARE_USER,
    )
    profiles = [p for p in list_compare_protocols() if p.id == V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID]
    assert len(profiles) == 1
    active = profiles[0]
    assert active.public_metadata()["bundle_revision"] == "0.2.3"
    assert active.system_template is VISUAL_COMPARE_SYSTEM
    assert active.user_template is VISUAL_COMPARE_USER
    assert resolve_compare_protocol(active.id, "0.2.3") is active
    archived = resolve_compare_protocol(active.id, "0.2.4")
    assert archived not in list_compare_protocols()


@pytest.mark.parametrize("response_gate", ["pass", "fail", "unclear"])
@pytest.mark.parametrize("revision", ["0.2.1", "0.2.2"])
def test_stable_plus_preserves_existing_schema_and_gate_policy(response_gate, revision):
    protocol = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, revision)
    observation = protocol.observation_model.model_validate(_observation_data(5, response_gate))
    result = visual_compare_result_fields(observation, protocol)
    assert result["answer1_understanding_score"] == (5 if response_gate == "pass" else None)
    assert result["answer2_understanding_score"] == 3
    assert result["standard_version"] == "0.2-simplified-plus"
    assert result["evaluation_profile"] == V02_PLUS_COMPARE_PROTOCOL_ID
    assert result["bundle_revision"] == revision
    assert result["overall_winner"] is None


@pytest.mark.parametrize("revision", ["0.2.0", "0.2.3", "0.2.4", "0.3.0", "1.0.0", "1.1.0", "unknown"])
def test_stable_plus_does_not_restore_another_protocol_revision(revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, revision)


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "cad4deb0a5db837ac161cd50af994ad58d058cc474aa31eeed7f8e8da176eb1a"),
    (2, "long_screenshot", "c6c7a1e7b6b26d95e7ab3274aab6b7d0f9e66e322f0744cc18b3ead42d2a1b94"),
    (3, "video_frames", "9c9f7c9389b67413302cdcce01afd76eb3c0b3a83e4cf5b0ea26bf0720858603"),
    (3, "long_screenshot", "fb367a01fd72bcae3266946cbc05da714a4b21faf3ba32f09934ad2e40693cbb"),
])
def test_stable_plus_prompt_bytes_remain_frozen(product_count, evidence_mode, expected_sha256):
    """Freeze the original stable-plus source and rendered 0.2.1 request.

    Anchors were captured from competitive_assessment 7dbe69c, before the
    qa_chat implementation merge. A scoring change requires a new revision.
    """
    import hashlib
    from pathlib import Path
    from auto_eval.judges import visual_compare_prompt_v02_simplified_plus_r021 as prompt

    source = Path(prompt.__file__).read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(source).hexdigest() == "f436ea425624ec57a47c81401a9812a37eb401d60f3749a24ef365fbb24b3e9b"
    protocol = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1")
    assert protocol.system_template is prompt.VISUAL_COMPARE_SYSTEM
    assert protocol.user_template is prompt.VISUAL_COMPARE_USER
    values = dict(persona="frozen-plus-regression", product_count=product_count,
                  evidence_mode=evidence_mode, evaluation_datetime="2026-10-15T12:00:00+08:00",
                  question="frozen query", context="frozen context", context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=2, frame_count2=2, frame_count3=2,
                  image_count1=2, image_count2=2, image_count3=2,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    request = protocol.system_template.render(**values) + "\0" + protocol.user_template.render(**values)
    assert hashlib.sha256(request.encode("utf-8")).hexdigest() == expected_sha256
    assert protocol.request_metadata() == {
        "id": V02_PLUS_COMPARE_PROTOCOL_ID, "standard_id": "qa_competitor_compare",
        "standard_version": "0.2-simplified-plus", "bundle_revision": "0.2.1",
        "display": "V0.2 简化版（稳定plus）·1-5分", "status": "stable", "score_range": [1, 5],
        "modes": ["compare"], "input_modalities": ["text", "text_image"],
        "conversation_adapter_version": "compare-conversation-1",
    }
    assert protocol.public_metadata()["score_dimensions"] == list(protocol.score_dimensions)


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "6eda7cb297d89045a115e3d34fb41a58b4cc1115e2a5f1ec5be4f84f8ac16950"),
    (2, "long_screenshot", "f11aa6874e78850f41a059a3f7015c3c1be8c7e96678e0f32bc4c4b153790644"),
    (3, "video_frames", "be4a83308d7ace04e10d35ea67123292e7c2b4b215d166d1b7b6444952a40f49"),
    (3, "long_screenshot", "a4f02fbf1e34bd2476cb3d27edda31829bca3360eb3ff892c10d129aeae239a8"),
])
def test_stable_plus_uploaded_prompt_is_frozen_and_preserves_output_contract(
    product_count, evidence_mode, expected_sha256
):
    """Freeze the uploaded 0.2.2 source while retaining the 0.2.1 I/O shape."""
    import hashlib
    import json
    from pathlib import Path
    from auto_eval.judges import visual_compare_prompt_v02_simplified_plus as prompt

    # Git may check out LF source as CRLF on Windows; freeze source content.
    assert hashlib.sha256(Path(prompt.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "5c774f5bf795f8b9a1047a535186a5c30044c767d2adcc3123a9f1aa7b92a7ad"
    )
    active = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID)
    frozen = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1")
    assert active.bundle_revision == "0.2.2"
    assert resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2") is active
    assert active.system_template is prompt.VISUAL_COMPARE_SYSTEM
    assert active.user_template is prompt.VISUAL_COMPARE_USER
    assert frozen.system_template is not active.system_template
    assert frozen not in list_compare_protocols()
    assert active.observation_model is frozen.observation_model is VisualCompareObservationV02
    assert active.require_response_pass is frozen.require_response_pass is True
    assert active.dimensions == frozen.dimensions
    assert active.score_dimensions == frozen.score_dimensions
    assert active.request_metadata() == {**frozen.request_metadata(), "bundle_revision": "0.2.2"}
    values = dict(persona="frozen-plus-regression", product_count=product_count,
                  evidence_mode=evidence_mode, evaluation_datetime="2026-10-15T12:00:00+08:00",
                  question="frozen query", context="frozen context", context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=2, frame_count2=2, frame_count3=2,
                  image_count1=2, image_count2=2, image_count3=2,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system = active.system_template.render(**values)
    user = active.user_template.render(**values)
    request = system + "\0" + user
    assert hashlib.sha256(request.encode("utf-8")).hexdigest() == expected_sha256
    assert user == frozen.user_template.render(**values)

    def output_example(rendered_system):
        output = rendered_system.split("【二十二、输出JSON结构】", 1)[1]
        return json.loads(output[output.index("{"):output.rindex("}") + 1])

    original_example = output_example(frozen.system_template.render(**values))
    updated_example = output_example(system)
    assert len(updated_example) == 81
    assert list(updated_example) == list(original_example)
    assert updated_example == original_example


@pytest.mark.parametrize("profile_id,version,revision,schema", [
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2-simplified-plus", "0.2.1", None),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2-simplified-plus", "0.2.2", None),
    (QA_CHAT_COMPARE_PROTOCOL_ID, "1.0", "1.0.0", "qa-chat-result-1"),
    (QA_CHAT_V2_COMPARE_PROTOCOL_ID, "1.1", "1.1.0", "qa-chat-result-2"),
])
def test_merge_keeps_plus_and_chat_revisions_separate_through_snapshot(profile_id, version, revision, schema):
    """Merging chat V2 cannot reinterpret either older frozen result family."""
    protocol = resolve_compare_protocol(profile_id, revision)
    task = Task(id="merged-profile", mode="compare", items=[], options={},
                evaluation_profile=profile_id, protocol_manifest=protocol.public_metadata())
    restored = _task_from_snapshot(snapshot_payload(task_to_snapshot(task)), task.id)
    assert restored.evaluation_profile == profile_id
    assert restored.protocol_manifest["standard_id"] == protocol.standard_id
    assert restored.protocol_manifest["standard_version"] == version
    assert restored.protocol_manifest["bundle_revision"] == revision
    assert restored.protocol_manifest["result_schema_version"] == schema
    assert resolve_compare_protocol(restored.evaluation_profile, revision).observation_model is protocol.observation_model
    other_revisions = ({"1.0.0", "1.1.0"} if profile_id == V02_PLUS_COMPARE_PROTOCOL_ID
                       else {"0.2.1", "0.2.2", "1.0.0", "1.1.0"} - {revision})
    for other in other_revisions:
        with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
            resolve_compare_protocol(profile_id, other)


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "3830efbd894849cf6eb570fdd7c03d43a96cf026be221aeb415d41d89aad0c64"),
    (2, "long_screenshot", "6b45295c375708671d7a762a37eb268e2eff2536bb6480d8ce67c1874ecbdf27"),
    (3, "video_frames", "935371dbf3c1c354b6fc034100789bdff1c50749ffbbc0156118c3622a6741e1"),
    (3, "long_screenshot", "c94ee5de809737824a8e0976b664bf3af593da1a3eaa7d8ad52d80fbeb21512b"),
])
def test_1022_r023_preserves_prompt_bytes_when_new_revision_removes_shared_context(
    product_count, evidence_mode, expected_sha256
):
    import hashlib
    from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID
    from auto_eval.judges import visual_compare_prompt_v02_1022_r023 as frozen_prompt

    active = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)
    frozen = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.3")
    assert active.bundle_revision == "0.2.4"
    assert frozen.system_template is frozen_prompt.VISUAL_COMPARE_SYSTEM
    assert frozen.user_template is frozen_prompt.VISUAL_COMPARE_USER
    assert frozen not in list_compare_protocols()
    assert frozen.observation_model is active.observation_model
    assert frozen.public_metadata() == {**active.public_metadata(), "bundle_revision": "0.2.3"}
    values = dict(persona="frozen-1022-r023", evaluation_datetime="2026-10-22T10:30:00+08:00",
                  question="frozen query", context="frozen shared context", product_count=product_count,
                  evidence_mode=evidence_mode, context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=1, frame_count2=2, frame_count3=3,
                  image_count1=1, image_count2=2, image_count3=3,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system, user = frozen.prompt_templates(product_count)
    rendered = system.render(**values) + "\0" + user.render(**values)
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == expected_sha256
    current_system, current_user = active.prompt_templates(product_count)
    current = current_system.render(**values) + "\0" + current_user.render(**values)
    assert "frozen shared context" in rendered and "evaluation_datetime" in rendered
    assert "frozen shared context" not in current and "evaluation_datetime" not in current
    assert frozen.prompt_templates(product_count) == frozen_prompt.templates_1022(product_count)
