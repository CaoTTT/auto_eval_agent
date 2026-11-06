import pytest
from fastapi import HTTPException

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.web import server
from auto_eval.web.tasks import Task


def compare_case(case_id: str, **updates) -> dict:
    item = {
        "id": case_id,
        "query": "question",
        "product_count": 2,
        "context1": "product 1 context",
        "context2": "product 2 context",
        "screenshot1": f"{case_id}-1.png",
        "screenshot2": f"{case_id}-2.png",
    }
    item.update(updates)
    return item


@pytest.fixture
def api(monkeypatch):
    config = AppConfig(judges=[JudgeConfig(name="offline", model="offline")])
    created = []
    runtime = {"profile_id": "offline", "judges": [{"name": "offline", "model": "offline"}]}

    async def source_files(*_args, **_kwargs):
        return []

    def new_task(mode, items, options, **kwargs):
        task = Task(
            id=kwargs["task_id"],
            mode=mode,
            items=items,
            options=options,
            evaluation_profile=kwargs["evaluation_profile"],
            protocol_manifest=kwargs["protocol_manifest"],
            judge_runtime=kwargs["judge_runtime"],
        )
        created.append(task)
        return task

    monkeypatch.setattr(server, "cfg", lambda: config)
    monkeypatch.setattr(server, "_request_source_files", source_files)
    monkeypatch.setattr(
        server,
        "_new_judge_runtime",
        lambda app_cfg, options: (runtime, app_cfg.judges),
    )
    monkeypatch.setattr(server, "new_task", new_task)
    monkeypatch.setattr(server.EVAL_SCHEDULER, "enqueue", lambda *_args: 1)
    return created


@pytest.mark.asyncio
async def test_eval_accepts_valid_cases_and_returns_rejected_indexes(api):
    response = await server.api_eval(server.EvalReq(
        mode="compare",
        items=[
            compare_case("good-1"),
            {"id": "bad", "query": "question", "context1": "one", "context2": "two"},
            compare_case("good-2"),
        ],
    ))

    assert response["accepted_indexes"] == [0, 2]
    assert response["accepted_count"] == 2
    assert response["rejected_count"] == 1
    assert response["rejected_items"][0]["index"] == 1
    assert response["rejected_items"][0]["position"] == 2
    assert response["rejected_items"][0]["id"] == "bad"
    assert "screenshot1" in response["rejected_items"][0]["error"]
    assert "video1" in response["rejected_items"][0]["error"]
    assert [item["id"] for item in api[0].items] == ["good-1", "good-2"]
    assert all(item["evidence_mode"] == "long_screenshot" for item in api[0].items)


@pytest.mark.asyncio
async def test_eval_rejects_all_only_when_every_case_is_invalid(api, monkeypatch):
    monkeypatch.setattr(
        server,
        "_new_judge_runtime",
        lambda *_args, **_kwargs: pytest.fail("must not initialize runtime for an all-invalid request"),
    )
    with pytest.raises(HTTPException) as caught:
        await server.api_eval(server.EvalReq(
            mode="compare",
            items=[
                {"id": "bad-1", "query": "question"},
                {"id": "bad-2", "query": "question"},
            ],
        ))

    assert caught.value.status_code == 422
    assert "没有可提交的条目" in caught.value.detail
    assert "context1" in caught.value.detail
    assert api == []


@pytest.mark.asyncio
async def test_eval_rejects_invalid_multiturn_session_without_dropping_other_cases(api):
    turn1 = compare_case(
        "turn-1",
        session_id="session-a",
        turn_index=1,
        screenshot_scope="current_turn",
    )
    turn2 = {
        "id": "turn-2",
        "query": "question",
        "context1": "one",
        "context2": "two",
        "session_id": "session-a",
        "turn_index": 2,
        "screenshot_scope": "current_turn",
    }
    response = await server.api_eval(server.EvalReq(
        mode="compare",
        items=[turn1, turn2, compare_case("independent")],
    ))

    assert response["accepted_indexes"] == [2]
    assert [item["index"] for item in response["rejected_items"]] == [0, 1]
    assert all("session-a" in item["error"] or "multiturn_evidence_missing" in item["error"]
               for item in response["rejected_items"])
    assert [item["id"] for item in api[0].items] == ["independent"]
