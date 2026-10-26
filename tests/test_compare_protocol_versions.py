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
    assert plus.bundle_revision == "0.2.3"
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
    assert created[-1].protocol_manifest["bundle_revision"] == "0.2.3"
    assert created[-1].protocol_manifest["score_range"] == [1, 5]

    for disabled_id in (
        V03_COMPARE_PROTOCOL_ID, V02_CALIBRATED_COMPARE_PROTOCOL_ID,
        V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, QA_CHAT_COMPARE_PROTOCOL_ID,
    ):
        with pytest.raises(HTTPException) as error:
            await server_module.api_eval(server_module.EvalReq(
                mode="compare", items=[item], evaluation_profile=disabled_id,
            ))
        assert error.value.status_code == 422
        assert "暂不可用于新建任务" in error.value.detail
    assert len(created) == 2
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
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.3"),
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
@pytest.mark.parametrize("revision", ["0.2.1", "0.2.2", "0.2.3"])
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


@pytest.mark.parametrize("protocol_id,revision", [
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.0"),
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.1"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.3"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.0"),
    (V03_COMPARE_PROTOCOL_ID, "0.3.1"),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2.2"),
    (V02_CALIBRATED_COMPARE_PROTOCOL_ID, "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2.3"),
    (V02_THINKING_EXPOSURE_COMPARE_PROTOCOL_ID, "0.2.4"),
])
@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_visual_compare_revisions_use_only_product_contexts(
    protocol_id, revision, product_count, evidence_mode
):
    protocol = resolve_compare_protocol(protocol_id, revision)
    values = dict(persona="test", product_count=product_count, evidence_mode=evidence_mode,
                  evaluation_datetime="2026-10-22T12:00:00+08:00", question="shared query")
    for number in (1, 2, 3):
        values.update({f"context{number}": f"product-context-sentinel-{number}",
                       f"answer{number}": f"answer-{number}", f"frame_count{number}": 1,
                       f"image_count{number}": 1, f"split_manifest{number}": "{}"})
    user = protocol.user_template.render(**values)
    assert user == protocol.user_template.render(**values, context="shared-context-must-not-be-sent")
    system = protocol.system_template.render(**values)
    assert "shared-context-must-not-be-sent" not in system + user
    assert "可信公共" not in system + user
    for number in range(1, product_count + 1):
        assert f"产品{number}背景：product-context-sentinel-{number}" in user
    assert ("product-context-sentinel-3" in user) is (product_count == 3)


@pytest.mark.parametrize("revision", ["0.2.0", "0.2.4", "0.3.0", "1.0.0", "1.1.0", "unknown"])
def test_stable_plus_does_not_restore_another_protocol_revision(revision):
    with pytest.raises(ValueError, match="无法恢复任务冻结的实现版本"):
        resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, revision)


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "53100a9c03b5749d2f1a8d417686a9b53d49b4d00a8c59298103b1ff6667af0b"),
    (2, "long_screenshot", "c51ec7c7eb6bf6c40ea3e17e8587312fbc27f014cd373700f8792fe20fc14d5f"),
    (3, "video_frames", "a3061b98d8263605925a113cf463b0dde567622020d1d38cdaa9df8f60b85aab"),
    (3, "long_screenshot", "93211f3261ade148d31907a24aaf739cd2640ed3785775a2f3a0973120465ddc"),
])
def test_legacy_stable_plus_preserves_contract_without_shared_context(product_count, evidence_mode, expected_sha256):
    """Retain 0.2.1 contracts after explicitly removing shared context from legacy prompts."""
    import hashlib
    from pathlib import Path
    from auto_eval.judges import visual_compare_prompt_v02_simplified_plus_r021 as prompt

    source = Path(prompt.__file__).read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(source).hexdigest() == "35afb710f23e3d3d190b890dc03cc7f4dea2d1dc77bd9218a61d3b84cf66a5f8"
    protocol = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1")
    assert protocol.system_template is prompt.VISUAL_COMPARE_SYSTEM
    assert protocol.user_template is prompt.VISUAL_COMPARE_USER
    values = dict(persona="frozen-plus-regression", product_count=product_count,
                  evidence_mode=evidence_mode, evaluation_datetime="2026-10-15T12:00:00+08:00",
                  question="frozen query", context1="c1", context2="c2", context3="c3",
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
    (2, "video_frames", "77f21b013e3eee4d4400a1d09e1d1ed8d589a6f2bbdd7918025b27921ebbd6a7"),
    (2, "long_screenshot", "616ffb66c23dbaf1a4ac93a37bd902eb73e13875f554d64ce368d42475367af9"),
    (3, "video_frames", "f265565df3f62edccf6a9a91d430076b32efbc2043519077b7d18f825fe9eb88"),
    (3, "long_screenshot", "947618eb5fda05eb2499df3f188f3e59f95299b06c5514041e9fd161dfb010bf"),
])
def test_stable_plus_r022_remains_frozen_and_preserves_output_contract(
    product_count, evidence_mode, expected_sha256
):
    """Freeze the uploaded 0.2.2 source while retaining the 0.2.1 I/O shape."""
    import hashlib
    import json
    from pathlib import Path
    from auto_eval.judges import visual_compare_prompt_v02_simplified_plus_r022 as prompt

    # Git may check out LF source as CRLF on Windows; freeze source content.
    assert hashlib.sha256(Path(prompt.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "4c73319f7de911e6c42ca2ba1eb7874bae70502483445d172c0057835426d24e"
    )
    frozen_r022 = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2")
    frozen_r021 = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.1")
    assert frozen_r022.bundle_revision == "0.2.2"
    assert frozen_r022 not in list_compare_protocols()
    assert frozen_r022.system_template is prompt.VISUAL_COMPARE_SYSTEM
    assert frozen_r022.user_template is prompt.VISUAL_COMPARE_USER
    assert frozen_r021.system_template is not frozen_r022.system_template
    assert frozen_r021 not in list_compare_protocols()
    assert frozen_r022.observation_model is frozen_r021.observation_model is VisualCompareObservationV02
    assert frozen_r022.require_response_pass is frozen_r021.require_response_pass is True
    assert frozen_r022.dimensions == frozen_r021.dimensions
    assert frozen_r022.score_dimensions == frozen_r021.score_dimensions
    assert frozen_r022.request_metadata() == {**frozen_r021.request_metadata(), "bundle_revision": "0.2.2"}
    values = dict(persona="frozen-plus-regression", product_count=product_count,
                  evidence_mode=evidence_mode, evaluation_datetime="2026-10-15T12:00:00+08:00",
                  question="frozen query", context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=2, frame_count2=2, frame_count3=2,
                  image_count1=2, image_count2=2, image_count3=2,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system = frozen_r022.system_template.render(**values)
    user = frozen_r022.user_template.render(**values)
    request = system + "\0" + user
    assert hashlib.sha256(request.encode("utf-8")).hexdigest() == expected_sha256
    assert "可信公共背景" not in user

    def output_example(rendered_system):
        output = rendered_system.split("【二十二、输出JSON结构】", 1)[1]
        return json.loads(output[output.index("{"):output.rindex("}") + 1])

    original_example = output_example(frozen_r021.system_template.render(**values))
    updated_example = output_example(system)
    assert len(updated_example) == 81
    assert list(updated_example) == list(original_example)
    assert updated_example == original_example


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "57d6d634b399b6c4f035125a8f8d43e052a0b126c1977dde7295cc6894a95b73"),
    (2, "long_screenshot", "0e3deafd70fbb4f65aeb08525b8cbfe88b20a6f0546430d3d49796517ab2cf1d"),
    (3, "video_frames", "1ed2f32b5d41d6d54fcc612d5e70187a32d81a229b616c48c1f73dbf1ed2f897"),
    (3, "long_screenshot", "b30a18e7b9060a17416b1ac8bf18b2830a02fe09a9a8476be380808ba4865a2f"),
])
def test_stable_plus_service_revision_preserves_inputs_output_and_frozen_history(
    product_count, evidence_mode, expected_sha256
):
    import hashlib
    import json
    from pathlib import Path
    from auto_eval.judges import visual_compare_prompt_v02_simplified_plus as prompt
    from auto_eval.web.protocol_identity import require_snapshot_protocol

    assert hashlib.sha256(Path(prompt.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "5bf5378e66ba98b437a537a08ef29226ac95b5196021d62c474c743727a28c54"
    )
    active = resolve_compare_protocol(V02_PLUS_COMPARE_PROTOCOL_ID)
    assert active.bundle_revision == "0.2.3"
    assert resolve_compare_protocol(active.id, "0.2.3") is active
    assert [p for p in list_compare_protocols() if p.id == active.id] == [active]
    assert active.system_template is prompt.VISUAL_COMPARE_SYSTEM
    assert active.user_template is prompt.VISUAL_COMPARE_USER
    values = dict(persona="frozen-plus-regression", product_count=product_count,
                  evidence_mode=evidence_mode, evaluation_datetime="2026-10-15T12:00:00+08:00",
                  question="frozen query", context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=2, frame_count2=2, frame_count3=2,
                  image_count1=2, image_count2=2, image_count3=2,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system = active.system_template.render(**values)
    user = active.user_template.render(**values)
    assert hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest() == expected_sha256
    marker = "【服务必要性三分类·评分前必须先判定】"
    assert marker in system

    def output_example(rendered_system):
        output = rendered_system.split("【二十二、输出JSON结构】", 1)[1]
        return json.loads(output[output.index("{"):output.rindex("}") + 1])

    current_example = output_example(system)
    assert len(current_example) == 81
    for revision in ("0.2.1", "0.2.2"):
        frozen = resolve_compare_protocol(active.id, revision)
        task = Task(id=f"frozen-plus-{revision}", mode="compare", items=[], options={},
                    evaluation_profile=frozen.id, protocol_manifest=frozen.public_metadata())
        snapshot = snapshot_payload(task_to_snapshot(task))
        restored = require_snapshot_protocol(snapshot)
        assert restored.bundle_revision == revision
        assert restored.system_template is frozen.system_template
        assert restored.user_template is frozen.user_template
        old_system = restored.system_template.render(**values)
        assert marker not in old_system
        if revision == "0.2.2":
            assert restored.user_template.render(**values) == user
        old_example = output_example(old_system)
        assert list(current_example) == list(old_example)
        assert current_example == old_example
        assert active.observation_model is frozen.observation_model is VisualCompareObservationV02
        assert active.require_response_pass is frozen.require_response_pass is True
        assert active.request_metadata() == {**frozen.request_metadata(), "bundle_revision": "0.2.3"}


@pytest.mark.parametrize("profile_id,version,revision,schema", [
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2-simplified-plus", "0.2.1", None),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2-simplified-plus", "0.2.2", None),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2-simplified-plus", "0.2.3", None),
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
                       else {"0.2.1", "0.2.2", "0.2.3", "1.0.0", "1.1.0"} - {revision})
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
    assert active.bundle_revision == "0.2.6"
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


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "aa57f37c8754507a72211f3a82b0c4d589640c94504c6e6759fc7fbefa792562"),
    (2, "long_screenshot", "d5d69fa20a334b2892b2936cb29efee38d7a2da40b4fc8faa31047e9e050bdd2"),
    (3, "video_frames", "713943002f7aa0f7f514cb1cb7ee72c14ca3ae67e59aee751c8db9fc22942cab"),
    (3, "long_screenshot", "bd4a5b98b66fe9fa958d7f6bb045015efcd3d3cfb6fc3541f85badf28cacef44"),
])
def test_1022_r024_restores_frozen_rules_after_v4_merge(product_count, evidence_mode, expected_sha256, monkeypatch):
    import hashlib
    import importlib
    from pathlib import Path
    from auto_eval.judges import compare_1022_contract as live_contract
    from auto_eval.judges import visual_compare_prompt_v02_1022_r024 as frozen_prompt

    def changed_live_helper(*args, **kwargs):
        raise AssertionError("Historical r024 rendering must not use current contract helpers")

    monkeypatch.setattr(live_contract, "example_1022_output", changed_live_helper)
    monkeypatch.setattr(live_contract, "output_field_instructions_1022", changed_live_helper)
    frozen_prompt = importlib.reload(frozen_prompt)
    assert hashlib.sha256(Path(frozen_prompt.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "3427b3b71173295cf1786b18579dc913962a95d97488a7314615eb1fcf1eb4f9"
    )
    active = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)
    frozen = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.4")
    assert active.bundle_revision == "0.2.6"
    assert frozen.system_template is frozen_prompt.VISUAL_COMPARE_SYSTEM
    assert frozen.user_template is frozen_prompt.VISUAL_COMPARE_USER
    assert frozen.prompt_templates(product_count) == frozen_prompt.templates_1022(product_count)
    assert frozen not in list_compare_protocols()
    assert frozen.observation_model is active.observation_model
    assert frozen.public_metadata() == {**active.public_metadata(), "bundle_revision": "0.2.4"}

    task = Task(id="frozen-1022-r024", mode="compare", items=[], options={},
                evaluation_profile=frozen.id, protocol_manifest=frozen.public_metadata())
    restored = _task_from_snapshot(snapshot_payload(task_to_snapshot(task)), task.id)
    restored_protocol = resolve_compare_protocol(restored.evaluation_profile, restored.protocol_manifest["bundle_revision"])
    assert restored_protocol.prompt_templates(product_count) == frozen.prompt_templates(product_count)
    values = dict(persona="frozen-1022-r024", evaluation_datetime="2026-10-22T10:30:00+08:00",
                  question="frozen query", context="frozen shared context", product_count=product_count,
                  evidence_mode=evidence_mode, context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=1, frame_count2=2, frame_count3=3,
                  image_count1=1, image_count2=2, image_count3=3,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system, user = restored_protocol.prompt_templates(product_count)
    rendered = system.render(**values) + "\0" + user.render(**values)
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == expected_sha256
    latest_system, latest_user = active.prompt_templates(product_count)
    assert rendered != latest_system.render(**values) + "\0" + latest_user.render(**values)
    assert "frozen shared context" not in rendered and "evaluation_datetime" not in rendered


@pytest.mark.parametrize("product_count,evidence_mode,expected_sha256", [
    (2, "video_frames", "e12ce6c7bbd46dbaf77beca03324c1d06a2d7ce02fca66b059386de7d0c2057f"),
    (2, "long_screenshot", "4ee2078bd75f9378034e96ca6c4097072030a4f89da71a04932536c9b5856e7e"),
    (3, "video_frames", "11c6c068b6e612b8a693eaabeb2accde4465cdd777f54f32fc29ec90fa3d4251"),
    (3, "long_screenshot", "9692faef02ed8d768d774d56b6546db011ba6f4b2036b0a7dbf0ce13dbf787cb"),
])
def test_1022_r025_remains_frozen_after_latest_workbook_revision(product_count, evidence_mode, expected_sha256, monkeypatch):
    import hashlib
    import importlib
    from pathlib import Path
    from auto_eval.judges import compare_1022_contract as live_contract
    from auto_eval.judges import visual_compare_prompt_v02_1022_r025 as frozen_prompt

    def changed_live_helper(*args, **kwargs):
        raise AssertionError("Historical r025 rendering must not use current contract helpers")

    monkeypatch.setattr(live_contract, "example_1022_output", changed_live_helper)
    monkeypatch.setattr(live_contract, "output_field_instructions_1022", changed_live_helper)
    frozen_prompt = importlib.reload(frozen_prompt)
    assert hashlib.sha256(Path(frozen_prompt.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "ea8347f8d9569e214d65f10f3a6d38811a7e2af25a6e5d1af3fc23838e85a3d1"
    )
    active = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)
    frozen = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.5")
    assert active.bundle_revision == "0.2.6"
    assert frozen.system_template is frozen_prompt.VISUAL_COMPARE_SYSTEM
    assert frozen.user_template is frozen_prompt.VISUAL_COMPARE_USER
    assert frozen.prompt_templates(product_count) == frozen_prompt.templates_1022(product_count)
    assert frozen not in list_compare_protocols()
    assert frozen.observation_model is active.observation_model
    task = Task(id="frozen-1022-r025", mode="compare", items=[], options={},
                evaluation_profile=frozen.id, protocol_manifest=frozen.public_metadata())
    restored = _task_from_snapshot(snapshot_payload(task_to_snapshot(task)), task.id)
    restored_protocol = resolve_compare_protocol(restored.evaluation_profile, restored.protocol_manifest["bundle_revision"])
    values = dict(persona="frozen-1022-r025", question="frozen query", context="shared",
                  evaluation_datetime="2026-10-22T10:30:00+08:00", product_count=product_count,
                  evidence_mode=evidence_mode, context1="c1", context2="c2", context3="c3",
                  answer1="a1", answer2="a2", answer3="a3", frame_count1=1, frame_count2=2, frame_count3=3,
                  image_count1=1, image_count2=2, image_count3=3,
                  split_manifest1="{}", split_manifest2="{}", split_manifest3="{}")
    system, user = restored_protocol.prompt_templates(product_count)
    rendered = system.render(**values) + "\0" + user.render(**values)
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == expected_sha256
    assert "shared" not in rendered and "evaluation_datetime" not in rendered
    latest_system, latest_user = active.prompt_templates(product_count)
    assert rendered != latest_system.render(**values) + "\0" + latest_user.render(**values)


@pytest.mark.parametrize("product_count,expected_schema_hash", [
    (2, "f83c4160954d4066780d8e83fc7a2277dbad0f1df4a049a9505173c6fe99b635"),
    (3, "a2c705c9a47594d6f9d0cbafaf6aea196abfd4793ccc835bf066f322f02ed682"),
])
def test_latest_workbook_revision_preserves_exact_json_schema(product_count, expected_schema_hash):
    import hashlib
    import json
    from auto_eval.judges.compare_1022_contract import output_schema_1022

    schema = output_schema_1022(product_count)
    digest = hashlib.sha256(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert digest == expected_schema_hash
