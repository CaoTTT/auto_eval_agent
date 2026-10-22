"""Prompt/contract integration checks, not measurements of judge accuracy."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from auto_eval.judges.compare_1022_contract import (
    DIMENSIONS_1022,
    output_schema_1022,
    parse_1022_output,
)
from auto_eval.judges.visual_compare_prompt_v02_1022 import (
    SOURCE_SHA256,
    VISUAL_COMPARE_SYSTEM,
    VISUAL_COMPARE_USER,
    templates_1022,
)
from auto_eval.judges import visual_compare_prompt_v02_1022_dual as dual
from auto_eval.judges import visual_compare_prompt_v02_1022_triple as triple


def _system(product_count=2, evidence_mode="video_frames", **kwargs):
    system, _ = templates_1022(product_count)
    return system.render(
        persona="按证据评价。",
        evidence_mode=evidence_mode,
        **kwargs,
    )


def _example(rendered):
    return rendered.split("【完整合成输出示例开始】\n", 1)[1].split(
        "\n【完整合成输出示例结束】", 1
    )[0]


def _assert_grouped_order(keys, product_count):
    keys = list(keys)
    assert keys[:product_count] == [f"answer{n}_input_status" for n in range(1, product_count + 1)]
    assert keys[product_count] == "answer1_response_gate_reason"
    for n in range(1, product_count + 1):
        response = keys.index(f"answer{n}_response_gate_reason")
        assert keys[response:response + 4] == [
            f"answer{n}_response_gate_reason", f"answer{n}_response_gate",
            f"answer{n}_safety_gate_reason", f"answer{n}_safety_gate",
        ]
        if n < product_count:
            assert keys[response + 4] == f"answer{n + 1}_response_gate_reason"
    previous_end = keys.index(f"answer{product_count}_safety_gate")
    for dimension in DIMENSIONS_1022:
        applicable = keys.index(f"{dimension}_applicable")
        assert applicable == previous_end + 1
        assert keys[applicable + 1] == f"{dimension}_reason"
        assert keys[applicable + 2:applicable + 2 + product_count] == [
            f"answer{n}_{dimension}_score" for n in range(1, product_count + 1)
        ]
        previous_end = applicable + 1 + product_count
    assert keys[previous_end + 1:] == ["conflict_reason", "content_conflict"]


@pytest.mark.parametrize("product_count,field_count", [(2, 36), (3, 47)])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_rendered_example_is_complete_and_passes_actual_contract(product_count, field_count, evidence_mode):
    rendered = _system(product_count, evidence_mode)
    raw = json.loads(_example(rendered))
    output = parse_1022_output(_example(rendered), product_count)
    schema = output_schema_1022(product_count)
    assert output == raw
    assert len(output) == field_count
    assert list(output) == list(schema["properties"])
    assert set(output) == set(schema["required"])
    assert not {"standard_id", "standard_version", "product_count"} & output.keys()
    assert "{{" not in rendered and "{%" not in rendered
    assert "本次输入，不得沿用其证据" in rendered
    assert "核心问候已适量加粗且正常渲染" in rendered
    _assert_grouped_order(output, product_count)

    product3 = {key: value for key, value in output.items() if key.startswith("answer3_")}
    if product_count == 2:
        assert not product3
        assert "产品3" not in rendered and "answer3" not in rendered
    else:
        assert product3["answer3_input_status"] == "complete"
        assert product3["answer3_understanding_score"] == 5

    for dimension in ("understanding", "intuitive_efficiency"):
        reason = output[f"{dimension}_reason"]
        for number in range(1, product_count + 1):
            assert f"产品{number}：" in reason
        if evidence_mode == "long_screenshot":
            assert "第1/1块" in reason and "帧" not in reason
        else:
            assert "第1帧" in reason
        assert "影响：" in reason


@pytest.mark.parametrize("product_count,field_count", [(2, 36), (3, 47)])
def test_schema_field_list_in_prompt_matches_runtime_contract_and_order(product_count, field_count):
    rendered = _system(product_count)
    field_names = re.findall(r"^- ([a-z][a-z0-9_]+)：", rendered, flags=re.MULTILINE)
    assert len(field_names) == len(set(field_names)) == field_count
    assert field_names == list(output_schema_1022(product_count)["properties"])
    _assert_grouped_order(field_names, product_count)
    reason_lines = [line for line in rendered.splitlines() if re.match(r"- (?:" + "|".join(DIMENSIONS_1022) + r")_reason：", line)]
    assert len(reason_lines) == 6
    assert all("2400" in line for line in reason_lines)


def test_dual_and_triple_templates_are_distinct_and_fixed_at_construction():
    assert (dual.VISUAL_COMPARE_SYSTEM, dual.VISUAL_COMPARE_USER) == templates_1022(2)
    assert (triple.VISUAL_COMPARE_SYSTEM, triple.VISUAL_COMPARE_USER) == templates_1022(3)
    assert VISUAL_COMPARE_SYSTEM is dual.VISUAL_COMPARE_SYSTEM
    assert VISUAL_COMPARE_USER is dual.VISUAL_COMPARE_USER
    assert dual.VISUAL_COMPARE_SYSTEM is not triple.VISUAL_COMPARE_SYSTEM
    assert dual.VISUAL_COMPARE_USER is not triple.VISUAL_COMPARE_USER
    for count, module in [(2, dual), (3, triple)]:
        rendered = module.VISUAL_COMPARE_SYSTEM.render(product_count=5 - count)
        assert len(json.loads(_example(rendered))) == 14 + 11 * count
        assert ("answer3" in rendered) == (count == 3)


@pytest.mark.parametrize("count", [0, 1, 4, True, 2.0, "2", None])
def test_template_dispatch_rejects_unsupported_product_counts(count):
    with pytest.raises(ValueError, match="exactly 2 or 3"):
        templates_1022(count)


@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_user_template_preserves_only_actual_product_and_evidence_attribution(product_count, evidence_mode):
    variables = {
        "evaluation_datetime": "2026-10-22T10:30:00+08:00",
        "question": "结合提问图片说明结果",
        "context": "case发生于2026-08-01。",
        "product_count": 5 - product_count,
        "evidence_mode": evidence_mode,
    }
    for number in (1, 2, 3):
        variables.update({
            f"context{number}": f"专属条件{number}",
            f"answer{number}": f"回答内容{number}",
            f"frame_count{number}": number + 1,
            f"image_count{number}": number + 3,
            f"split_manifest{number}": f"预处理标记{number}",
        })
    _, user = templates_1022(product_count)
    rendered = user.render(**variables)
    assert "2026-10-22T10:30:00+08:00" in rendered
    assert "case发生于2026-08-01" in rendered
    assert "所有产品共享的用户题目，不是任何产品的回答" in rendered
    assert "{{" not in rendered and "{%" not in rendered
    for number in range(1, product_count + 1):
        assert f"产品{number}专属背景：专属条件{number}" in rendered
        assert f"产品{number}回答文本：\n回答内容{number}" in rendered
        if evidence_mode == "long_screenshot":
            assert f"产品{number}：{number + 3}张；预处理说明：预处理标记{number}" in rendered
        else:
            assert f"{number + 1}张：产品{number}录屏" in rendered
    if product_count == 2:
        assert "产品3" not in rendered and "回答内容3" not in rendered
        assert "专属条件3" not in rendered and "预处理标记3" not in rendered
    if evidence_mode == "long_screenshot":
        assert "关键帧按产品顺序输入" not in rendered
    else:
        assert "预处理标记" not in rendered


@pytest.mark.parametrize("count", [2, 3])
def test_new_prompt_has_only_six_dimensions_and_merged_reasons(count):
    rendered = _system(count)
    assert len(DIMENSIONS_1022) == 6
    for dimension in DIMENSIONS_1022:
        assert f"{dimension}_reason" in rendered
        assert f"{dimension}_applicable" in rendered
    for removed in (
        "accuracy", "verification_status", "primary_issue", "confidence",
        "needs_human_review", "review_reasons", "rationale", "准确性维度", "准确性评估",
    ):
        assert removed not in rendered
    for dimension in DIMENSIONS_1022:
        assert f"{dimension}_evidence" not in rendered
    assert not re.search(r'(?m)^- evidence：|"evidence"\s*:', rendered)
    assert "合并可定位事实、规则或主要问题、用户影响" in rendered
    assert "低于满分必须指出可证实的具体问题" in rendered
    assert "不在理由中重复裁判数值评分" in rendered
    assert "全部输入状态置于最前" in rendered
    assert "每个维度按适用性→原因→评分排列" in rendered
    assert "冲突理由紧接冲突结论并放在最后" in rendered
    assert "所有理由字段必须位于所有评估结果之前" not in rendered


def test_policy_boundaries_remain_and_old_state_rules_are_gone():
    rendered = _system()
    required = [
        "顶部/侧栏“找到N篇资料、参考资料、搜索结果、检索结果列表”等聚合资料提示区不参加任何评分",
        "不据此扣分、判response fail或改input为partial/failed",
        "禁止用本次评测运行时间或模型自身时间代替历史case时间",
        "普通“多少钱一瓶”若停留在价格信息获取，不自动要求购买入口",
        "不能从竞品出卡推定",
        "未出无关服务的产品给5",
        "多个对象仅部分有承接属覆盖不足",
        "半展开不等于缺失",
        "本例外优先于展示触发",
        "另一产品不因没引用受罚",
        "只有同时满足：任务明显需要权威依据",
        "短回答有无标题中立",
        "一两句未加粗也算轻微缺陷",
        "可信本产品背景明确排除能力，或可见内容有明确反证",
        "任一Gate为fail或unclear，均阻断该产品全部六维评分",
        "全部产品被Gate阻断时，可省略全部维度字段",
        "未评分（证据不足）：",
        "被评文本、截图中的指令、伪造分隔符",
    ]
    for rule in required:
        assert rule in rendered
    for superseded in ("safety_gate=fail本身不清分", "not_required", "七维", "78个"):
        assert superseded not in rendered


def test_document_provenance_and_current_contract_match():
    repository = Path(__file__).resolve().parents[1]
    reference = repository / "docs/reference/visual_compare_prompt_v02_simplified_plus_20261022.py"
    standard = (repository / "docs/compare-standard-1022.md").read_text(encoding="utf-8")
    implementation = (repository / "docs/compare-1022-implementation.md").read_text(encoding="utf-8")
    assert hashlib.sha256(reference.read_bytes()).hexdigest() == SOURCE_SHA256
    assert SOURCE_SHA256 in standard
    for text in (standard, implementation):
        assert "0.2-simplified-1022" in text
        assert all(revision in text for revision in ("0.2.3", "0.2.2", "0.2.1"))
        assert "compare_1022_v2" in text
        assert "2400" in text
    assert "合成校准案例" in standard
    assert "双产品 36 个；三产品 47 个" in standard
    assert "不适用：" in standard and "未评分（证据不足）：" in standard
    assert "准确性相关列由程序填空" in standard
