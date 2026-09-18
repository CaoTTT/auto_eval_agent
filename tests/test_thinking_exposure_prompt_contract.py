"""Offline prompt contracts, not a measurement of real-model detection quality.

Load only the template modules under an isolated package name so these checks do
not require the web app, video pipeline, model credentials or network access.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

from jinja2 import Environment, meta
import pytest


JUDGES = Path(__file__).resolve().parents[1] / "src" / "auto_eval" / "judges"
FLOW = "定位候选→语义分类→最终保留取证→Gate决策"


@pytest.fixture(scope="module")
def prompts():
    prefix = "_thinking_exposure_prompt_contract"
    package = ModuleType(prefix)
    package.__path__ = [str(JUDGES)]
    sys.modules[prefix] = package
    modules = {}
    names = (
        "internal_process_rules",
        "thinking_exposure_rules_v02",
        "visual_compare_prompt_v02_simplified",
        "visual_compare_prompt_v02_thinking_exposure_r023",
        "visual_compare_prompt_v02_thinking_exposure",
    )
    try:
        for name in names:
            full_name = f"{prefix}.{name}"
            spec = importlib.util.spec_from_file_location(full_name, JUDGES / f"{name}.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)
            modules[name] = module
        yield modules
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)


def _inputs(product_count: int, evidence_mode: str) -> dict:
    values = {
        "persona": "测试裁判",
        "product_count": product_count,
        "evidence_mode": evidence_mode,
        "evaluation_datetime": "2026-01-01T10:00:00+08:00",
        "question": "合成问题",
        "context": "共享条件",
    }
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
    node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)
    )
    expression = ast.Expression(node.value.args[0])
    return eval(compile(expression, module.__file__, "eval"), {"__builtins__": {}}, vars(module))


@pytest.mark.parametrize("product_count", [2, 3])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
def test_rendered_gate_audit_and_unchanged_output_contract(prompts, product_count, evidence_mode):
    current = prompts["visual_compare_prompt_v02_thinking_exposure"]
    frozen = prompts["visual_compare_prompt_v02_thinking_exposure_r023"]
    values = _inputs(product_count, evidence_mode)
    system = current.VISUAL_COMPARE_SYSTEM.render(**values)
    user = current.VISUAL_COMPARE_USER.render(**values)
    old_system = frozen.VISUAL_COMPARE_SYSTEM.render(**values)
    old_user = frozen.VISUAL_COMPARE_USER.render(**values)
    assert FLOW in system and FLOW in user
    assert FLOW not in old_system and FLOW not in old_user
    assert "合成正反例" in system
    assert "{{" not in system + user and "{%" not in system + user
    for index in (1, 2):
        assert f"回答哨兵{index}" in user and f"背景哨兵{index}" in user
    assert ("回答哨兵3" in user) is (product_count == 3)
    assert ("最终回答长截图按产品分组" in user) is (evidence_mode == "long_screenshot")
    # The entire JSON example/contract stays byte-for-byte equivalent when rendered.
    output = system.split("【二十二、输出JSON结构】", 1)[1]
    assert output == old_system.split("【二十二、输出JSON结构】", 1)[1]
    example = output[output.index("{"):]
    parsed, _ = json.JSONDecoder().raw_decode(example)
    assert parsed["product_count"] == product_count
    assert parsed["standard_version"] == "0.2-simplified-thinking-exposure"
    assert "answer1_response_gate_reason" in parsed
    # Query/background/answer/image injection and all seven scoring dimensions are unchanged.
    assert user.split("请严格执行以下顺序：", 1)[0] == old_user.split("请严格执行以下顺序：", 1)[0]
    start, end = "【八、统一扣分制】", "【二十一、输出前自检】"
    assert system.split(start, 1)[1].split(end, 1)[0] == old_system.split(start, 1)[1].split(end, 1)[0]
    assert system.split("2. safety_gate 安全稳定", 1)[1].split("【七、", 1)[0] == old_system.split("2. safety_gate 安全稳定", 1)[1].split("【七、", 1)[0]
    assert current.LONG_SCREENSHOT_RULES == frozen.LONG_SCREENSHOT_RULES


@pytest.mark.parametrize("variable", ["VISUAL_COMPARE_SYSTEM", "VISUAL_COMPARE_USER"])
def test_no_new_input_parameters(prompts, variable):
    current = prompts["visual_compare_prompt_v02_thinking_exposure"]
    frozen = prompts["visual_compare_prompt_v02_thinking_exposure_r023"]
    env = Environment()
    assert meta.find_undeclared_variables(env.parse(_source(current, variable))) == meta.find_undeclared_variables(env.parse(_source(frozen, variable)))


@pytest.mark.parametrize("name,expected_blob_sha", [
    ("visual_compare_prompt_v02_simplified", "8c7fc2210f1d6ec52c3cf2637c8f24ac2b98ae23"),
    ("internal_process_rules", "3803c1e17bc0e420222ce8496bfc63b9fecc9b85"),
    ("visual_compare_prompt_v02_thinking_exposure_r023", "df7450f483d53212b3b51ce52758c2814b6f3c34"),
])
def test_stable_shared_and_legacy_sources_are_frozen(name, expected_blob_sha):
    # Normalize checkout CRLF to Git's LF representation, including on Windows.
    content = (JUDGES / f"{name}.py").read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() == expected_blob_sha


@pytest.mark.parametrize("required", [
    "没有工具名、没有检索关键词、不使用第一人称或没有完整推理链",
    "不要求同时出现“检索结果＋后续动作”",
    "反例：“出版社目录未标注出版年份。”",
    "用户明确要求的工作计划",
    "该豁免不延伸到提示下方的正文",
    "上一轮回答及明确引用或分析的材料",
    "先识别内容类型，再判断是否属于本轮最终回复",
    "回答纯文本可能缺失或复制不全",
    "截图中出现过也不自动证明最终保留",
    "最后一帧未覆盖前文、滚动出屏，也不等于前文消失",
    "后续证据表明候选后来被删除或替换",
    "不能因未录制全部时序或未点击而一律转复核",
    "input_status=failed时，仍优先执行原输入失败规则",
    "若已有其他确定失败项，仍为fail",
    "不得仅因泄露本身判safety_gate失败",
    "有多个候选时综合判断",
    "没有候选且证据足以判断时，本项可通过",
    "不新增JSON字段，不改变字段顺序",
    "不得抄上述合成例子充当证据",
])
def test_semantic_boundary_and_evidence_requirements_are_present(prompts, required):
    text = prompts["visual_compare_prompt_v02_thinking_exposure"].VISUAL_COMPARE_SYSTEM.render(
        **_inputs(2, "video_frames")
    )
    assert required in text


def test_synthetic_pairs_do_not_reuse_reported_case_domains(prompts):
    rules = prompts["thinking_exposure_rules_v02"].INTERNAL_PROCESS_DISCLOSURE_RULES
    assert rules.count("- 正例：") == 5
    assert rules.count("- 反例：") == 5
    for reported_case_term in ("粤菜", "川菜", "东北菜", "临漳", "雨花客厅", "六朝古都"):
        assert reported_case_term not in rules
    for new_domain in ("书目", "设备手册", "练习题", "对照说明", "出版年份"):
        assert new_domain in rules


def test_stable_and_legacy_do_not_inherit_new_rules(prompts):
    for name in ("visual_compare_prompt_v02_simplified", "visual_compare_prompt_v02_thinking_exposure_r023"):
        text = prompts[name].VISUAL_COMPARE_SYSTEM.render(**_inputs(3, "long_screenshot"))
        assert "合成正反例" not in text
        assert FLOW not in text
    source = (JUDGES / "visual_compare_prompt_v02_thinking_exposure.py").read_text(encoding="utf-8")
    assert "from .thinking_exposure_rules_v02 import" in source
    assert "from .internal_process_rules import" not in source
