"""Exercise candidate rendering with the current compare input contract."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jinja2 import Environment, StrictUndefined, meta

from auto_eval.judges.knowledge_qa_schema import (
    KNOWLEDGE_QA_DIMENSIONS, KNOWLEDGE_QA_DIMENSION_NAMES,
    KnowledgeQAObservation, expected_output_keys, validate_output,
)
from auto_eval.judges.visual_compare_prompt_knowledge_qa import (
    USER_TEMPLATE_SOURCE, VISUAL_COMPARE_SYSTEM, VISUAL_COMPARE_USER,
    output_example, render_system_prompt,
)
from auto_eval.judges.compare_protocols import list_compare_protocols, resolve_compare_protocol
from auto_eval.judges.conversation_prompt import assemble_conversation
from auto_eval.web.parse_input import parse_jsonl
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_render_keeps_existing_product_data_and_evidence_grouping(count, mode):
    inputs = dict(question="QUERY_SENTINEL", evaluation_datetime="REVIEW_CALL_TIME",
                  product_count=count, evidence_mode=mode)
    for n in (1, 2, 3):
        inputs.update({f"context{n}": f"[产品已知信息]P{n}_BACKGROUND\n[仅供裁判核对]P{n}_REFERENCE",
                       f"answer{n}": f"P{n}_COPIED_TEXT", f"frame_count{n}": n + 10,
                       f"image_count{n}": n + 20, f"split_manifest{n}": f"P{n}_SPLIT"})
    strict_template = Environment(undefined=StrictUndefined).from_string(USER_TEMPLATE_SOURCE)
    user = strict_template.render(**inputs)
    assert user == VISUAL_COMPARE_USER.render(**inputs)
    assert "QUERY_SENTINEL" in user and "REVIEW_CALL_TIME" in user
    for n in (1, 2, 3):
        for sentinel in (f"P{n}_BACKGROUND", f"P{n}_REFERENCE", f"P{n}_COPIED_TEXT"):
            assert (sentinel in user) is (n <= count)
        assert (f"P{n}_SPLIT" in user) is (mode == "long_screenshot" and n <= count)
    assert "{{" not in user
    assert "当前目标轮四维" in user
    assert "not_applicable" not in user  # Data layout does not duplicate scoring rules.


def test_no_new_input_fields_are_required_by_the_user_template():
    env = Environment()
    variables = meta.find_undeclared_variables(env.parse(USER_TEMPLATE_SOURCE))
    expected = {"question", "evaluation_datetime", "product_count", "evidence_mode"}
    expected |= {f"{field}{n}" for n in (1, 2, 3)
                 for field in ("context", "answer", "frame_count", "image_count", "split_manifest")}
    assert variables == expected


@pytest.mark.parametrize("count", [2, 3])
def test_complete_system_contains_exact_valid_active_product_shape(count):
    rendered = render_system_prompt(product_count=count, persona="PERSONA_SENTINEL")
    example = json.loads(output_example(count))
    assert "PERSONA_SENTINEL" in rendered
    assert rendered.rstrip().endswith(output_example(count))
    assert tuple(validate_output(example, count).model_dump()) == expected_output_keys(count)
    assert len(example) == 8 * count + 8
    assert "1.5分" in rendered and "准确性0分不自动清空其他维度" in rendered
    for dimension in KNOWLEDGE_QA_DIMENSIONS:
        assert dimension + "_evidence" in rendered and dimension + "_reason" in rendered
    assert not any(key.endswith(("_gate", "_winner")) for key in example)
    if count == 2:
        assert not any(key.startswith("answer3_") for key in example)


def test_all_scoring_and_state_rules_live_in_system_for_multiturn_replacement():
    system = VISUAL_COMPARE_SYSTEM.render(persona="", product_count=2, evidence_mode="long_screenshot")
    for rule in ("target_turn", "不重评历史", "只对应产品", "仅供裁判核对", "共享用户题图",
                 "不证明产品实际测试日期", "不能代替测试时间", "0也是有效分",
                 "insufficient_evidence", "unverifiable", "not_applicable", "完整输出合同"):
        assert rule in system


@pytest.mark.parametrize("count", [True, 1, 4, "2"])
def test_unsupported_product_counts_fail_before_rendering(count):
    with pytest.raises(ValueError):
        render_system_prompt(product_count=count)


def test_unsupported_evidence_mode_fails_before_rendering():
    with pytest.raises(ValueError):
        render_system_prompt(evidence_mode="mixed")


def test_candidate_cannot_be_selected_through_unchanged_runtime():
    assert all(protocol.standard_id != "knowledge_qa_compare" for protocol in list_compare_protocols())


def test_existing_conversation_adapter_keeps_original_history_and_target(conversation_data):
    # A local test descriptor exercises the existing adapter, without registering
    # this candidate or sending its output through the unsupported legacy judge.
    protocol = replace(
        resolve_compare_protocol(None), id="knowledge_qa_compare@1.0-candidate",
        standard_id="knowledge_qa_compare", standard_version="1.0", bundle_revision="1.0.0",
        system_template=VISUAL_COMPARE_SYSTEM, user_template=VISUAL_COMPARE_USER,
        observation_model=KnowledgeQAObservation, require_response_pass=False,
        score_min=0, score_max=2, dimensions=KNOWLEDGE_QA_DIMENSIONS,
        dimension_names=dict(KNOWLEDGE_QA_DIMENSION_NAMES),
        score_dimensions=KNOWLEDGE_QA_DIMENSIONS, result_schema_version="knowledge-qa-result-1",
    )
    items, preparation = prepare_conversation(conversation_data)
    bundle = preparation.prepare(items[1])
    system, parts, metadata, refs, _ = assemble_conversation(
        render_system_prompt(product_count=2), bundle, protocol,
        evaluation_datetime="2026-10-29T11:00:00+08:00",
    )
    text = "\n".join(part.get("text", "") for part in parts)
    assert "target_turn=2" in text and "问题1" in text and "问题2" in text
    assert "问题3" not in text and "自己的回答3" not in text
    for n in (1, 2):
        for t in (1, 2):
            assert f"产品{n}自己的回答{t}" in text
            assert f"产品{n}第{t}轮背景" in text
    assert metadata and refs
    assert "不证明产品实际测试日期" in system
    assert system.rstrip().endswith(output_example(2))
    assert len({m["asset_id"] for m in metadata}) == len(metadata)


def test_existing_jsonl_import_needs_no_new_fields_for_candidate_inputs():
    sample = dict(id="knowledge-case", query="一个小时等于多少分钟？", product_count=2,
                  context1="[产品已知信息]用户问一般时间换算。\n[仅供裁判核对]一小时等于60分钟。",
                  context2="[产品已知信息]用户问一般时间换算。\n[仅供裁判核对]一小时等于60分钟。",
                  answer1="60分钟。", answer2="60分钟。",
                  screenshot1="/evidence/p1.png", screenshot2="/evidence/p2.png")
    items, errors = parse_jsonl(json.dumps(sample, ensure_ascii=False), "compare")
    assert not errors and len(items) == 1
    item = items[0]
    assert item["query"] == sample["query"]
    for n in (1, 2):
        assert item[f"context{n}"] == sample[f"context{n}"]
        assert item[f"answer{n}"] == sample[f"answer{n}"]
        assert item[f"screenshot{n}"] == sample[f"screenshot{n}"]
    assert item["evidence_mode"] == "long_screenshot"


def test_documented_output_example_is_valid():
    doc = Path(__file__).resolve().parents[1] / "docs" / "knowledge-qa-prompt-v1.md"
    section = doc.read_text(encoding="utf-8").split("<!-- output-example -->", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    assert validate_output(example, 2).model_dump() == example
