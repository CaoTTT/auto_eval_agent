"""Product backgrounds and task clocks must not leak into shared case inputs."""
import json
from datetime import datetime, timezone

import pytest

from auto_eval.compare_input import ProductContextError
from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.compare_1022_contract import example_1022_output
from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.web.runner import _eval_one


class RecordingClient:
    persona = "测试裁判"
    model = "offline"

    def __init__(self, count):
        self.cfg = JudgeConfig(name="offline")
        self.count = count
        self.calls = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return json.dumps(example_1022_output(self.count), ensure_ascii=False)


def make_judge(count=2, revision=None):
    client = RecordingClient(count)
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    return VisualCompareJudge(client, profile, resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, revision)), client


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("revision", ["0.2.4", "0.2.5", "0.2.6"])
async def test_new_task_time_does_not_change_1022_prompt_or_scores(count, revision):
    judge, client = make_judge(count, revision)
    contexts = {f"context{n}": f"仅产品{n}背景" for n in range(1, count + 1)}
    first = await judge.evaluate(question="你好", product_count=count, context="不应共享的文本",
        evaluation_time=datetime(2026, 10, 22, tzinfo=timezone.utc), **contexts)
    second = await judge.evaluate(question="你好", product_count=count, context="不应共享的文本",
        evaluation_time=datetime(2030, 1, 1, tzinfo=timezone.utc), **contexts)
    assert client.calls[0] == client.calls[1]
    assert first["prompt_sha256"] == second["prompt_sha256"]
    assert first["evaluation_datetime"] != second["evaluation_datetime"]
    system, user, _ = client.calls[0]
    assert "evaluation_datetime" not in system + user
    assert "不应共享的文本" not in system + user
    assert "可信公共背景" not in user
    for n in range(1, count + 1):
        assert user.count(f"仅产品{n}背景") == 1
    assert ("产品3专属背景" in user) == (count == 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["0.2.4", "0.2.5", "0.2.6"])
async def test_legacy_alias_is_used_only_for_first_product(revision):
    judge, client = make_judge(revision=revision)
    await judge.evaluate(question="你好", product_count=2, context="旧字段产品1背景", context2="产品2背景")
    user = client.calls[0][1]
    assert user.count("旧字段产品1背景") == 1
    assert user.index("产品1专属背景") < user.index("旧字段产品1背景") < user.index("—— 产品2 ——")


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["0.2.4", "0.2.5", "0.2.6"])
@pytest.mark.parametrize("contexts", [
    {}, {"context1": "产品1背景"}, {"context1": "", "context": "不能回退", "context2": "产品2背景"},
    {"context1": None, "context": "不能回退", "context2": "产品2背景"},
    {"context1": "产品1背景", "context2": "  "},
])
async def test_missing_context_never_reaches_model(contexts, revision):
    judge, client = make_judge(revision=revision)
    with pytest.raises(ProductContextError):
        await judge.evaluate(question="你好", product_count=2, **contexts)
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["0.2.4", "0.2.5", "0.2.6"])
async def test_triple_product_requires_own_third_context(revision):
    judge, client = make_judge(3, revision)
    with pytest.raises(ProductContextError, match="context3"):
        await judge.evaluate(question="你好", product_count=3, context1="产品1背景", context2="产品2背景")
    assert not client.calls


@pytest.mark.asyncio
async def test_frozen_r023_keeps_optional_product_backgrounds_and_time_without_shared_context():
    judge, client = make_judge(revision="0.2.3")
    await judge.evaluate(question="你好", product_count=2, context="旧版公共背景",
        evaluation_time=datetime(2026, 10, 22, tzinfo=timezone.utc))
    system, user, _ = client.calls[0]
    assert "旧版公共背景" not in system + user
    assert "2026-10-22T00:00:00+00:00" in user


@pytest.mark.asyncio
async def test_runner_normalizes_alias_before_building_judge_arguments():
    class Judge:
        protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)

        async def evaluate(self, **kwargs):
            self.kwargs = kwargs
            return {"product_count": 2}

    judge = Judge()
    await _eval_one("compare", 0, {
        "query": "你好", "context": "旧字段产品1背景", "context2": "产品2背景",
        "product_count": 2, "frames1": ["first.png"], "frames2": ["second.png"],
    }, rich_judges=[], compare_judges=[judge], category_display={})
    assert judge.kwargs["context1"] == "旧字段产品1背景"
    assert judge.kwargs["context2"] == "产品2背景"
    assert "context" not in judge.kwargs


@pytest.mark.asyncio
async def test_runner_rejects_missing_context_before_judge_call():
    class Judge:
        protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)

        async def evaluate(self, **kwargs):
            pytest.fail("Missing product context must never reach the judge")

    with pytest.raises(ProductContextError, match="context2"):
        await _eval_one("compare", 0, {
            "query": "你好", "context1": "产品1背景", "frames1": ["first.png"], "frames2": ["second.png"],
        }, rich_judges=[], compare_judges=[Judge()], category_display={})
