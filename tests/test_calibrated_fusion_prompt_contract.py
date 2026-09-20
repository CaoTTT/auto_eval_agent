"""Offline contracts for calibrated r023 = calibrated r022 + usable disclosure r023.

These tests exercise real templates without the web app, credentials or network.
They do not measure a real model's detection accuracy.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

from jinja2 import Environment, StrictUndefined, meta
import pytest


JUDGES = Path(__file__).resolve().parents[1] / "src" / "auto_eval" / "judges"
CURRENT = "visual_compare_prompt_v02_calibrated"
FROZEN = "visual_compare_prompt_v02_calibrated_r022"
OLD_BOUNDARY = "- 完整内部推理或工具过程泄漏仍按response_gate规则判断；普通最终措辞冗余在本维度按阅读影响评价。"
NEW_BOUNDARY = "- 已确认的内部过程信息泄露，包括足以命中的短句，均按response_gate规则判fail，不要求完整推理链；只有未命中内部过程泄露的普通最终措辞冗余，才在本维度按阅读影响评价。"


@pytest.fixture(scope="module")
def prompts():
    prefix = "_calibrated_fusion_contract"
    package = ModuleType(prefix)
    package.__path__ = [str(JUDGES)]
    sys.modules[prefix] = package
    modules = {}
    try:
        for name in ("internal_process_rules", "query_image_prompt", FROZEN, CURRENT):
            fullname = f"{prefix}.{name}"
            spec = importlib.util.spec_from_file_location(fullname, JUDGES / f"{name}.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            spec.loader.exec_module(module)
            modules[name] = module
        yield modules
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)


def _inputs(count: int, mode: str) -> dict:
    values = dict(
        persona="测试裁判", product_count=count, evidence_mode=mode,
        evaluation_datetime="2026-09-20T09:00:00-04:00", question="合成问题",
        context="共享条件",
    )
    for index in (1, 2, 3):
        values.update({
            f"context{index}": f"背景哨兵{index}",
            f"answer{index}": f"回答哨兵{index}",
            f"frame_count{index}": index,
            f"image_count{index}": index,
            f"split_manifest{index}": f"切片哨兵{index}",
        })
    return values


def _source(module: ModuleType, variable: str) -> str:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets)
    )
    expression = ast.Expression(assignment.value.args[0])
    return eval(compile(expression, module.__file__, "eval"), {"__builtins__": {}}, vars(module))


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
@pytest.mark.parametrize("vqa", [False, True])
def test_fusion_preserves_contract_and_calibrated_scoring(prompts, count, mode, vqa):
    current, frozen = prompts[CURRENT], prompts[FROZEN]
    values = _inputs(count, mode)
    env = Environment(undefined=StrictUndefined)
    system = env.from_string(_source(current, "VISUAL_COMPARE_SYSTEM")).render(**values)
    user = env.from_string(_source(current, "VISUAL_COMPARE_USER")).render(**values)
    assert system == current.VISUAL_COMPARE_SYSTEM.render(**values)
    assert user == current.VISUAL_COMPARE_USER.render(**values)
    old_system = frozen.VISUAL_COMPARE_SYSTEM.render(**values)
    old_user = frozen.VISUAL_COMPARE_USER.render(**values)
    assert "{{" not in system + user and "{%" not in system + user
    assert system.count("【思考暴露（内部过程信息泄露）】") == 1
    assert prompts["internal_process_rules"].INTERNAL_PROCESS_DISCLOSURE_RULES in system
    assert "【思考暴露（内部过程信息泄露）】" not in old_system
    assert "定位候选→语义分类→最终保留取证→Gate决策" not in system
    marker = "【二十二、输出JSON结构】"
    output = system.split(marker, 1)[1]
    assert output == old_system.split(marker, 1)[1]
    parsed, _ = json.JSONDecoder().raw_decode(output[output.index("{"):])
    assert parsed["product_count"] == count
    assert parsed["standard_version"] == "0.2-simplified-calibrated"
    assert user.split("请严格执行以下顺序：", 1)[0] == old_user.split("请严格执行以下顺序：", 1)[0]
    for index in (1, 2):
        assert f"回答哨兵{index}" in user and f"背景哨兵{index}" in user
    assert ("回答哨兵3" in user) is (count == 3)
    assert ("最终回答长截图按产品分组" in user) is (mode == "long_screenshot")
    start, end = "【七、动态适用性总规则】", "【二十一、输出前自检】"
    old_scoring = old_system.split(start, 1)[1].split(end, 1)[0]
    scoring = system.split(start, 1)[1].split(end, 1)[0]
    assert scoring.replace(NEW_BOUNDARY, OLD_BOUNDARY, 1) == old_scoring
    start, end = "2. safety_gate 安全稳定", "【七、动态适用性总规则】"
    assert system.split(start, 1)[1].split(end, 1)[0] == old_system.split(start, 1)[1].split(end, 1)[0]
    if vqa:
        suffix = prompts["query_image_prompt"].QUERY_IMAGE_INSTRUCTIONS
        request_system = system + suffix  # Same append order as VisualCompareJudge.
        assert "提问图片" in request_system
        assert "不属于任何产品回答" in request_system
        assert request_system.count("【思考暴露（内部过程信息泄露）】") == 1


@pytest.mark.parametrize("variable", ["VISUAL_COMPARE_SYSTEM", "VISUAL_COMPARE_USER"])
def test_no_new_template_input_parameters(prompts, variable):
    env = Environment()
    assert meta.find_undeclared_variables(env.parse(_source(prompts[CURRENT], variable))) == meta.find_undeclared_variables(env.parse(_source(prompts[FROZEN], variable)))


@pytest.mark.parametrize("required", [
    "只要可见片段足以确认上述泄露即可命中，不要求完整推理链",
    "确认命中思考暴露（内部过程信息泄露），无须完整推理链",
    "即使最终回答可用也不豁免",
    "不得仅在直观高效扣分或因泄露本身直接判safety_gate失败",
    "内部检索找到了什么、没有找到什么、结果是否足够",
    "skill（技能）、内部工具名称或调用过程",
    "独立于最终回复正文的简洁UI状态提示",
    "用户Query、提问图片、界面回显的用户消息、上一轮回答",
    "已确认属于本轮最终回复的可信文字也可证明语言内容",
    "截图已清晰展示的泄露，不得因缺少回答纯文本而漏判",
    "生成中后来消失或被替换的片段，不能直接当成最终回复泄露",
    "无法确认片段是否属于本轮最终回复时，标明证据不足",
    "后续七维按本协议置null",
])
def test_usable_r023_boundaries_are_retained(prompts, required):
    assert required in prompts[CURRENT].VISUAL_COMPARE_SYSTEM.render(**_inputs(2, "long_screenshot"))


def test_user_instruction_and_conflicting_exemptions(prompts):
    system = prompts[CURRENT].VISUAL_COMPARE_SYSTEM.render(**_inputs(2, "long_screenshot"))
    user = prompts[CURRENT].VISUAL_COMPARE_USER.render(**_inputs(2, "long_screenshot"))
    assert "命中时在response_gate_reason说明证据位置和内部过程类别" in user
    assert "不要因复制文本缺失而漏掉截图中的泄露" in user
    for obsolete in (
        "若暴露的是完整隐藏推理或工具过程",
        "完整内部推理或工具过程泄漏仍按response_gate规则判断",
        "页面展示搜索状态或搜索结果数量，不因这种正常用户可见反馈判为内部过程泄漏",
    ):
        assert obsolete not in system
    source = Path(prompts[CURRENT].__file__).read_text(encoding="utf-8")
    assert "from .internal_process_rules import" in source
    assert "thinking_exposure_rules_v02" not in source


@pytest.mark.parametrize("name,blob", [
    (FROZEN, "83633b1db0a1e84555e59dab5b2fb842df66b54d"),
    ("internal_process_rules", "3803c1e17bc0e420222ce8496bfc63b9fecc9b85"),
    ("query_image_prompt", "d90c13e16d55d80a20c3dd8c3a435887c39fee55"),
])
def test_frozen_sources_remain_byte_identical(name, blob):
    content = (JUDGES / f"{name}.py").read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() == blob
