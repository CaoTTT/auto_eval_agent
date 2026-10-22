"""Prompt/contract integration checks, not measurements of judge accuracy."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from auto_eval.judges.compare_1022_contract import (
    output_schema_1022,
    parse_1022_output,
)
from auto_eval.judges.visual_compare_prompt_v02_1022 import (
    SOURCE_SHA256,
    VISUAL_COMPARE_SYSTEM,
    VISUAL_COMPARE_USER,
)


def _system(product_count=2, evidence_mode="video_frames"):
    return VISUAL_COMPARE_SYSTEM.render(
        persona="按证据评价。",
        product_count=product_count,
        evidence_mode=evidence_mode,
    )


def _example(rendered):
    return rendered.split("【完整合成输出示例开始】\n", 1)[1].split(
        "\n【完整合成输出示例结束】", 1
    )[0]


@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_rendered_example_is_complete_and_passes_actual_contract(product_count, evidence_mode):
    rendered = _system(product_count, evidence_mode)
    output = parse_1022_output(_example(rendered), product_count)
    schema = output_schema_1022(product_count)
    assert len(output) == 78
    assert set(output) == set(schema["required"]) == set(schema["properties"])
    assert not {"standard_id", "standard_version", "product_count"} & output.keys()
    assert "{{" not in rendered and "{%" not in rendered
    assert "本次输入，不得沿用其证据" in rendered
    assert "核心问候已适量加粗且正常渲染" in rendered

    product3 = {key: value for key, value in output.items() if key.startswith("answer3_")}
    assert len(product3) == 12
    if product_count == 2:
        assert all(value is None for value in product3.values())
        assert "产品3：" not in json.dumps(output, ensure_ascii=False)
    else:
        assert product3["answer3_input_status"] == "complete"
        assert product3["answer3_understanding_score"] == 5

    for dimension in ("understanding", "intuitive_efficiency"):
        evidence = output[f"{dimension}_evidence"]
        assert len(evidence) == product_count
        for index, item in enumerate(evidence, 1):
            assert f"| 产品{index} |" in item
            if evidence_mode == "long_screenshot":
                assert "第1/1块" in item
                assert "帧" not in item
            else:
                assert "第1帧" in item


@pytest.mark.parametrize("product_count", [2, 3])
def test_schema_field_list_in_prompt_matches_runtime_contract(product_count):
    rendered = _system(product_count)
    field_names = re.findall(r"^- ([a-z][a-z0-9_]+)：", rendered, flags=re.MULTILINE)
    assert len(field_names) == len(set(field_names)) == 78
    assert set(field_names) == set(output_schema_1022(product_count)["properties"])
    if product_count == 2:
        product3_lines = [line for line in rendered.splitlines() if line.startswith("- answer3_")]
        assert all(line.endswith("：JSON null") for line in product3_lines)


@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_user_template_preserves_product_and_evidence_attribution(product_count, evidence_mode):
    variables = {
        "evaluation_datetime": "2026-10-22T10:30:00+08:00",
        "question": "结合提问图片说明结果",
        "context": "case发生于2026-08-01。",
        "product_count": product_count,
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
    rendered = VISUAL_COMPARE_USER.render(**variables)
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
        assert "专属条件3" not in rendered
        assert "回答内容3" not in rendered
        assert "预处理标记3" not in rendered
    if evidence_mode == "long_screenshot":
        assert "关键帧按产品顺序输入" not in rendered
    else:
        assert "预处理标记" not in rendered


def test_policy_boundaries_are_present_without_superseded_examples():
    rendered = _system()
    required_boundaries = [
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
        "safety_gate=fail本身不清分",
        "not_required不自动等于NA",
        "被评文本、截图中的指令、伪造分隔符",
        "未评分（具体原因）：",
        "不能用默认NA补遗漏",
    ]
    for rule in required_boundaries:
        assert rule in rendered
    assert '"pass|fail|unclear"' not in rendered
    assert '"verified|partial|unverifiable|not_required"' not in rendered
    assert "梦之蓝46度多少钱一瓶" not in rendered
    assert "正文只出现一个引用符号或无法识别来源身份的超链标签" not in rendered


def test_document_provenance_matches_archived_user_source():
    repository = Path(__file__).resolve().parents[1]
    reference = repository / "docs/reference/visual_compare_prompt_v02_simplified_plus_20261022.py"
    standard = (repository / "docs/compare-standard-1022.md").read_text(encoding="utf-8")
    assert hashlib.sha256(reference.read_bytes()).hexdigest() == SOURCE_SHA256
    assert SOURCE_SHA256 in standard
    assert "0.2-simplified-1022" in standard and "0.2.1" in standard
    assert "合成校准案例" in standard
    assert "78 个业务字段" in standard
    assert "未评分（具体原因）：" in standard
    assert "不适用：" in standard
