"""Prompt boundary and platform input binding, without calling a model."""
import json

import pytest

from auto_eval.judges.visual_compare_prompt_chitchat_gsb import (
    VISUAL_COMPARE_USER, render_system_prompt,
)


def input_json(user):
    value, _tail = json.JSONDecoder().raw_decode(user[user.index("{"):])
    return value


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_user_binds_only_real_per_product_platform_fields(count, mode):
    context = {
        "question": '用户说："别给建议"\n{{ unsafe }}', "product_count": count,
        "evaluation_datetime": "2026-11-02T17:30:00+08:00", "evidence_mode": mode,
        "trusted_collection_metadata": {"answer1": {"input_status": "complete", "source": "capture"}},
    }
    for n in range(1, 4):
        context.update({f"context{n}": f"P{n}_BACKGROUND", f"answer{n}": f"P{n}_ANSWER",
                        f"image_count{n}": n, f"frame_count{n}": n + 2,
                        f"split_manifest{n}": f"P{n}_SPLIT"})
    rendered = VISUAL_COMPARE_USER.render(**context)
    data = input_json(rendered)
    assert data["query"] == context["question"]
    assert data["trusted_collection_metadata"] == context["trusted_collection_metadata"]
    assert data["evaluation_datetime"] == context["evaluation_datetime"]
    assert data["evidence_mode"] == mode
    assert len(data["products"]) == count
    for n, product in enumerate(data["products"], 1):
        assert product["product_no"] == n
        assert product["platform_context"] == f"P{n}_BACKGROUND"
        assert product["answer"] == f"P{n}_ANSWER"
        assert product["answer_image_count"] == n
        assert product["video_frame_count"] == n + 2
        assert product["split_manifest"] == f"P{n}_SPLIT"
        assert product["context"] == []
        assert all(f"P{other}_BACKGROUND" not in product["platform_context"]
                   for other in range(1, 4) if other != n)
        for field in ("current_time_beijing", "user_info", "user_md", "knowledge", "resultDataList",
                      "intent", "slots", "intent_explain", "slot_explain"):
            assert product[field] == ""
    if count == 2:
        assert "pair_1_3" not in rendered and "P3_BACKGROUND" not in rendered
    else:
        assert all(key in rendered for key in ("pair_1_2", "pair_1_3", "pair_2_3"))


def test_missing_optional_platform_fields_remain_empty_instead_of_invented():
    data = input_json(VISUAL_COMPARE_USER.render(question="你好", product_count=2))
    assert data["evaluation_datetime"] == "" and data["evidence_mode"] == ""
    assert data["trusted_collection_metadata"] == {}
    for product in data["products"]:
        assert product["answer"] == product["platform_context"] == product["split_manifest"] == ""
        assert product["answer_image_count"] == product["video_frame_count"] == 0


@pytest.mark.parametrize("count", [2, 3])
def test_rendered_rubric_fixes_pair_direction_without_absolute_scores(count):
    system = render_system_prompt(product_count=count)
    assert "G=A整体优于B；B=A整体劣于B；S=" in system
    assert "A=产品1，B=产品2" in system
    assert "status、gsb、reason、serious_issue四个字段" in system
    assert "个性化为低优先级辅助项，不单独设权重" in system
    assert "不把冲突两票相抵成S" in system
    assert "不把采集失败当产品回答差" in system
    assert "不输出绝对分数、各维分数、Gate、适用性、胜者或排名" in system
    assert "仅措辞、Emoji、列表或长短略有不同且效果相近，判S" in system
    assert "include_prior_knowledge=false" in system
    assert ("pair_1_3" in system) is (count == 3)
    if count == 3:
        assert "pair_2_3（A=产品2，B=产品3）" in system
        assert "不为凑总排名强制传递胜负" in system


def test_prior_knowledge_switch_does_not_permit_search_or_invented_evidence():
    enabled = render_system_prompt(include_prior_knowledge=True)
    assert "include_prior_knowledge=true" in enabled
    assert "不得自行检索、伪造来源" in enabled
    assert "include_prior_knowledge=false" not in enabled


@pytest.mark.parametrize("arguments", [
    {"product_count": 4}, {"product_count": True}, {"evidence_mode": "audio"},
    {"include_prior_knowledge": "true"},
])
def test_render_helper_rejects_unsupported_requests(arguments):
    with pytest.raises(ValueError):
        render_system_prompt(**arguments)
