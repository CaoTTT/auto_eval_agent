"""Public context is ignored; product conditions and original history survive."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from PIL import Image

from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.compare_1022_contract import example_1022_output
from auto_eval.judges.compare_protocols import DIMENSIONS, V1022_COMPARE_PROTOCOL_ID, list_compare_protocols
from auto_eval.judges.conversation_prompt import assemble_conversation
from auto_eval.judges.rich_content_judge import RichContentJudge
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot
from auto_eval.web import runner
from auto_eval.web.conversation_prepare import ConversationPreparation
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_qa_chat_protocol import observation_data
from test_qa_chat_v2_protocol import payload as chat_v2_payload
from test_timeliness_protocol import payload as timeliness_payload


PUBLIC_CONTEXT = "SHARED_CONTEXT_MUST_NOT_REACH_THE_MODEL"
PROTOCOLS = list_compare_protocols()
EVALUATION_TIME = datetime(2026, 10, 22, tzinfo=timezone.utc)


def response_data(protocol, count):
    if protocol.standard_id == "chitchat_evidence_first_flat":
        data = {}
        for dimension in protocol.dimensions:
            data[dimension + "_reason"] = "产品1、产品2" + ("、产品3" if count == 3 else "") + "：本轮表现符合标准。"
            data.update({f"answer{n}_{dimension}_score": 5 for n in range(1, count + 1)})
        return data
    if protocol.standard_id == "knowledge_qa_compare":
        from auto_eval.judges.visual_compare_prompt_knowledge_qa import output_example
        return json.loads(output_example(count))
    if protocol.id == V1022_COMPARE_PROTOCOL_ID:
        return example_1022_output(count)
    if protocol.standard_id == "timeliness_compare":
        return timeliness_payload(count)
    if protocol.result_schema_version == "qa-chat-result-2":
        return chat_v2_payload(count)
    if protocol.result_schema_version == "qa-chat-result-1":
        return observation_data(count)
    data = {"product_count": count, **{f"{d}_applicable": False for d in DIMENSIONS}}
    for n in range(1, count + 1):
        data.update({f"answer{n}_input_status": "complete",
                     f"answer{n}_response_gate": "pass", f"answer{n}_safety_gate": "pass"})
    return data


class Client:
    persona = "test"
    model = "fake"
    cfg = JudgeConfig(name="test", model="fake")

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return json.dumps(self.response, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.id)
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
async def test_compare_call_ignores_public_context_and_keeps_product_inputs(tmp_path, protocol, count, mode):
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    kwargs = dict(question="QUERY_SENTINEL", product_count=count, evidence_mode=mode,
                  evaluation_time=EVALUATION_TIME)
    metas = []
    for n in range(1, count + 1):
        path = tmp_path / f"p{n}.png"
        Image.new("RGB", (64, 96), (n * 40, 20, 60)).save(path)
        kwargs[f"context{n}"] = f"PRODUCT_CONTEXT_{n}"
        kwargs[f"answer{n}"] = f"PRODUCT_ANSWER_{n}"
        if mode == "long_screenshot":
            meta = prepare_long_screenshot(path, tmp_path / str(n), profile.long_screenshot)
            metas.append(meta)
            kwargs[f"frames{n}"] = [part["path"] for part in meta["slices"]]
        else:
            kwargs[f"frames{n}"] = [str(path)]
    if metas:
        kwargs["screenshot_metas"] = metas
    client = Client(response_data(protocol, count))
    judge = VisualCompareJudge(client, profile, protocol)
    first = await judge.evaluate(context=PUBLIC_CONTEXT, **kwargs)
    second = await judge.evaluate(context="DIFFERENT_IGNORED_CONTEXT", **kwargs)
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]
    system, user, sent = client.calls[0]
    assert PUBLIC_CONTEXT not in system + user
    assert "QUERY_SENTINEL" in user
    for n in range(1, count + 1):
        assert f"PRODUCT_CONTEXT_{n}" in user and f"PRODUCT_ANSWER_{n}" in user
    images = sent.get("user_images") or [p for p in sent["content_parts"] if p["type"] == "image_url"]
    assert len(images) == count
    assert first["product_count"] == count
    assert first["prompt_sha256"] == second["prompt_sha256"]
    if "input_manifest_sha256" in first:
        assert first["input_manifest_sha256"] == second["input_manifest_sha256"]


def test_multiturn_keeps_product_history_and_ignores_public_context(conversation_data):
    source, _, _ = conversation_data
    for turn in source:
        turn["context"] = PUBLIC_CONTEXT
        for n in (1, 2):
            turn[f"context{n}"] = f"P{n}_BACKGROUND_T{turn['turn_index']}"
    items, service = prepare_conversation(conversation_data)
    target = items[1]
    bundle = service.prepare(target)
    changed_items = deepcopy(items)
    for turn in changed_items:
        turn["context"] = "DIFFERENT_IGNORED_CONTEXT"
    changed_service = ConversationPreparation(changed_items, "same-history", conversation_data[1], service.limits)
    changed_bundle = changed_service.prepare(changed_items[1])
    assert target["history_prefix_sha256"] == changed_items[1]["history_prefix_sha256"]
    for protocol in PROTOCOLS:
        system = protocol.system_template.render(persona="test", product_count=2, evidence_mode="long_screenshot")
        request = assemble_conversation(system, bundle, protocol, evaluation_datetime=EVALUATION_TIME.isoformat())
        changed = assemble_conversation(system, changed_bundle, protocol, evaluation_datetime=EVALUATION_TIME.isoformat())
        assert request[0] == changed[0]  # System text.
        assert request[1] == changed[1]  # Actual text and image content sent to the model.
        assert request[-1] == changed[-1]  # Evidence identity excludes storage paths.
        text = request[0] + "\n" + "\n".join(p.get("text", "") for p in request[1])
        assert PUBLIC_CONTEXT not in text and "DIFFERENT_IGNORED_CONTEXT" not in text
        assert "问题1" in text and "问题2" in text and "问题3" not in text
        for turn in (1, 2):
            for n in (1, 2):
                assert f"P{n}_BACKGROUND_T{turn}" in text
                assert f"产品{n}自己的回答{turn}" in text
    changed_items[0]["context1"] = "CHANGED_PRODUCT_BACKGROUND"
    product_service = ConversationPreparation(changed_items, "same-history", conversation_data[1], service.limits)
    product_service.prepare(changed_items[1])
    assert target["history_prefix_sha256"] != changed_items[1]["history_prefix_sha256"]


@pytest.mark.asyncio
async def test_rich_content_ignores_legacy_public_context(tmp_path):
    path = tmp_path / "answer.png"
    Image.new("RGB", (64, 96), "white").save(path)
    client = Client({"visual_description": "无挂卡", "cards": [], "superlinks": []})
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    judge = RichContentJudge(client, profile)
    args = dict(question="QUERY_SENTINEL", answer_text="ANSWER_SENTINEL", frames=[str(path)])
    await judge.evaluate(context=PUBLIC_CONTEXT, **args)
    await judge.evaluate(**args)
    assert client.calls[0] == client.calls[1]
    assert PUBLIC_CONTEXT not in client.calls[0][0] + client.calls[0][1]
    assert "QUERY_SENTINEL" in client.calls[0][1] and "ANSWER_SENTINEL" in client.calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rich_content", "compare"])
async def test_runner_does_not_forward_public_context(mode):
    calls = []

    class Judge:
        async def evaluate(self, **kwargs):
            calls.append(kwargs)
            return {}

    item = dict(query="q", context=PUBLIC_CONTEXT, frames=["a.png"],
                frames1=["a.png"], frames2=["b.png"], context1="p1", context2="p2")
    kwargs = {"rich_judges" if mode == "rich_content" else "compare_judges": [Judge()]}
    await runner._eval_one(mode, 0, item, **kwargs)
    assert len(calls) == 1 and "context" not in calls[0]
    if mode == "compare":
        assert calls[0]["context1"] == "p1" and calls[0]["context2"] == "p2"
