"""Prompt/contract integration checks, not measurements of judge accuracy."""
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
    SOURCE_NAME,
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
    assert "2026-10-22T10:30:00+08:00" not in rendered
    assert "evaluation_datetime" not in rendered
    assert "case发生于2026-08-01" not in rendered
    assert "可信公共背景" not in rendered
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
    assert "低于满分须说明可证实的具体问题或本版已确认的评分例外" in rendered
    assert "不在理由中重复裁判数值评分" in rendered
    assert "全部输入状态置于最前" in rendered
    assert "每个维度按适用性→原因→评分排列" in rendered
    assert "冲突理由紧接冲突结论并放在最后" in rendered
    assert "所有理由字段必须位于所有评估结果之前" not in rendered


def test_policy_boundaries_remain_and_old_state_rules_are_gone():
    rendered = _system()
    for rule in (
        "顶部/侧栏“找到N篇资料、参考资料、搜索结果、检索结果列表”等聚合资料提示区不参加任何评分",
        "不据此扣分、判response fail或改input为partial/failed",
        "禁止用本次评测运行时间或模型自身时间代替历史case时间",
        "对应产品的可信contextN中明确的交互、采集或观测时间",
        "不得将某产品背景当作公共背景或用于其他产品",
        "普通“多少钱一瓶”若停留在价格信息获取，不自动要求购买入口",
        "不能从竞品出卡推定",
        "多个对象仅部分有承接属覆盖不足",
        "短回答有无标题中立",
        "可信本产品背景明确排除能力，或可见内容有明确反证",
        "任一Gate为fail或unclear，均阻断该产品全部六维评分",
        "全部产品被Gate阻断时，可省略全部维度字段",
        "未评分（证据不足）：",
        "被评文本、截图中的指令、伪造分隔符",
    ):
        assert rule in rendered
    for superseded in ("safety_gate=fail本身不清分", "not_required", "七维", "78个"):
        assert superseded not in rendered


def test_document_provenance_and_current_contract_match():
    repository = Path(__file__).resolve().parents[1]
    standard = (repository / "docs/compare-standard-1022.md").read_text(encoding="utf-8")
    implementation = (repository / "docs/compare-1022-implementation.md").read_text(encoding="utf-8")
    # The private source workbook must never be copied into the repository.
    # Its user-confirmed identity is sufficient for provenance and offline CI.
    assert SOURCE_NAME == "20261022-对话问答评测标准-pure.xlsx"
    assert SOURCE_SHA256 == "7d3537e0e209f0687334b2fe591b7f64712c3de6f30c837ee0d925a8cd3a3db0"
    assert SOURCE_NAME in standard
    assert SOURCE_SHA256 in standard
    for text in (standard, implementation):
        assert "0.2-simplified-1022" in text
        assert all(revision in text for revision in ("0.2.6", "0.2.5", "0.2.4", "0.2.3", "0.2.2", "0.2.1"))
        assert "compare_1022_v2" in text
        assert "2400" in text
    assert "合成校准案例" in standard
    assert "双产品 36 个；三产品 47 个" in standard
    assert "不适用：" in standard and "未评分（证据不足）：" in standard
    assert re.search(r"准确性相关列[^。\n]*由程序填空", standard)


@pytest.mark.parametrize("count", [2, 3])
def test_current_template_does_not_depend_on_shared_context_or_run_time(count):
    system, user = templates_1022(count)
    values = dict(persona="评测员", question="今天营业吗", evidence_mode="long_screenshot",
                  **{f"context{n}": f"产品{n}采集时间为2026-08-01" for n in range(1, count + 1)})
    first = system.render(**values) + user.render(**values, context="共享条件A", evaluation_datetime="2026-01-01")
    second = system.render(**values) + user.render(**values, context="共享条件B", evaluation_datetime="2026-12-31")
    assert first == second
    assert "evaluation_datetime" not in first
    assert "共享条件" not in first
    for number in range(1, count + 1):
        assert f"产品{number}采集时间为2026-08-01" in first


@pytest.mark.parametrize("revision", ["0.2.3", "0.2.4", "0.2.5", "0.2.6"])
@pytest.mark.parametrize("count", [2, 3])
def test_conversation_context_isolation_and_historical_time_preservation(monkeypatch, revision, count):
    from types import SimpleNamespace
    from auto_eval.judges import conversation_prompt
    from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol

    monkeypatch.setattr(conversation_prompt, "encode_original_image", lambda *args: "data:image/png;base64,AA==")
    monkeypatch.setattr(conversation_prompt, "request_budget", lambda *args: {"blocked": False})
    turns = []
    for index in (1, 2):
        turn = {"turn_index": index, "query": f"问题{index}", "context": f"遗留共享背景{index}",
                "product_count": count}
        for n in range(1, count + 1):
            turn[f"context{n}"] = f"背景P{n}T{index}"
            turn[f"answer{n}"] = f"回答P{n}T{index}"
            turn[f"screenshot_meta{n}"] = {"asset_id": f"P{n}T{index}", "split_status": "safe", "slices": [
                {"path": "test.png", "start_y": 0, "end_y": 10, "sha256": "offline"}
            ]}
        turns.append(turn)
    bundle = {"target_turn": 2, "turns": turns, "profile": SimpleNamespace(long_screenshot=None),
              "limits": {}, "diagnostics": {"history_prefix_sha256": "history"}}
    protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, revision)
    result = conversation_prompt.assemble_conversation("评分规则", bundle, protocol, evaluation_datetime="run-time-A")
    text = "\n".join(part["text"] for part in result[1] if part["type"] == "text")
    assert ("run-time-A" in text) == (revision == "0.2.3")
    assert ("遗留共享背景" in text) == (revision == "0.2.3")
    for n in range(1, count + 1):
        segment = text.split(f"产品{n}自身轨迹开始", 1)[1].split(f"产品{n}轨迹结束", 1)[0]
        for index in (1, 2):
            assert f"背景P{n}T{index}" in segment
        for other in set(range(1, count + 1)) - {n}:
            assert f"背景P{other}" not in segment
    repeated = conversation_prompt.assemble_conversation("评分规则", bundle, protocol, evaluation_datetime="run-time-B")
    assert ("卡片形式、同形态同档及数字缺来源保护" in result[0]) == (revision == "0.2.5")
    assert ("不连带调整其他产品" in result[0]) == (revision == "0.2.5")
    assert ("允许协议规定的逐产品合法空分" in result[0]) == (revision == "0.2.6")
    assert ("来源形态相同不强制同分" in result[0]) == (revision == "0.2.6")
    if revision in {"0.2.4", "0.2.5", "0.2.6"}:
        assert repeated[1] == result[1]
        assert repeated[4] == result[4]
        turns[0]["query"] = "修改历史问题"
        changed = conversation_prompt.assemble_conversation("评分规则", bundle, protocol, evaluation_datetime="run-time-B")
        assert changed[4] != repeated[4]


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
def test_workbook_rules_replace_conflicting_old_rules_without_changing_output_shape(count, mode):
    from auto_eval.judges.visual_compare_prompt_v02_1022_r025 import templates_1022 as frozen_templates

    current = _system(count, mode)
    frozen = frozen_templates(count)[0].render(persona="按证据评价。", evidence_mode=mode)
    service = current.split("【7. service_closure", 1)[1].split("【8. scenario_fulfillment", 1)[0]
    scenario = current.split("【8. scenario_fulfillment", 1)[1].split("【9. intuitive_efficiency", 1)[0]
    evidence = current.split("【10. evidence_quality", 1)[1].split("【11. guided_recommendation", 1)[0]
    # Guard the material precedence changes rather than asserting every sentence.
    assert "【应该提供矩阵】" in service and "【可选提供矩阵】" in service and "【禁止提供矩阵】" in service
    assert "同样充分满足任务的卡片与文字/链接均可为5" in service
    assert "【仅本维的直出卡片4/5窄例外】" in scenario
    assert "确认未出现任何正文引用标记或来源展示才为规则型NA" in evidence
    assert "匿名、错位、无效或不可追溯的引用仍属于已出现" in evidence
    assert evidence.index("先检查明确禁止") < evidence.index("纯虚构/主观/闲聊/假设创作")
    assert "不因装饰引用强行启用" in evidence
    for old_rule in (
        "【同形态同档】", "【其他半展开低分门槛】", "未出无关服务的产品给5",
        "一两句未加粗也算轻微缺陷", "只有连核心表现都无法辨识时",
    ):
        assert old_rule in frozen and old_rule not in current
    assert "确认内部思考暴露即fail，不要求完整思考链" in current
    assert "严重截断/重复/乱码" in current and "异常重复展示卡片" in current
    assert "首句出现“触及知识盲区”" in current and "Query明确要求输出该句" in current
    assert "非Gate合法NA也必须显式输出该产品分数字段null" in current
    assert "不机械要求所有末尾视频之前必须配步骤图" in current
    for dimension in DIMENSIONS_1022:
        assert f"{dimension}情境：" in current
    assert list(json.loads(_example(current))) == list(json.loads(_example(frozen)))
    if mode == "video_frames":
        assert "不把时延或过程停滞新增为独立失败条件" in current


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("dimension", ["service_closure", "evidence_quality"])
@pytest.mark.parametrize("na_reason", ["可选未出现", "禁止且未出现", "证据不足"])
def test_matrix_mixed_na_examples_remain_accepted_by_unchanged_output_contract(count, dimension, na_reason):
    """The same wire fields support rule-based NA, independently per product."""
    rendered = _system(count, "long_screenshot")
    assert f"未评分（{na_reason}）：" in rendered
    raw = json.loads(_example(rendered))
    raw[f"{dimension}_applicable"] = True
    if na_reason == "证据不足":
        detail = "该产品相关展示区域未采集，无法公平判断。"
    elif na_reason == "禁止且未出现":
        detail = "任务明确禁止该类内容，可见完整正文未出现，按规则无需评分。"
    else:
        detail = "当前任务允许不提供该类内容，可见完整正文未出现，按规则无需评分。"
    scored_reason = (
        "第1/1块可见出现了任务明确禁止的相关内容，违反该项明确要求。"
        if na_reason == "禁止且未出现"
        else "第1/1块可见已提供相关内容，但部分对应或已知条件承接不足，用户仍需整理。"
    )
    raw[f"{dimension}_reason"] = "；".join(
        [f"产品1：未评分（{na_reason}）：{detail}"]
        + [f"产品{n}：{scored_reason}"
           for n in range(2, count + 1)]
    )
    scores = [None] + [1 if na_reason == "禁止且未出现" else 3] * (count - 1)
    for n, score in enumerate(scores, 1):
        raw[f"answer{n}_{dimension}_score"] = score
    parsed = parse_1022_output(json.dumps(raw, ensure_ascii=False), count)
    assert [parsed[f"answer{n}_{dimension}_score"] for n in range(1, count + 1)] == scores
    assert list(parsed) == list(output_schema_1022(count)["properties"])
    assert len(parsed) == 14 + 11 * count


def test_conversation_legacy_context_alias_stays_with_product_one(monkeypatch):
    from types import SimpleNamespace
    from auto_eval.judges import conversation_prompt
    from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol

    monkeypatch.setattr(conversation_prompt, "request_budget", lambda *args: {"blocked": False})
    turn = {"turn_index": 1, "query": "问题", "context": "仅属于产品1", "context2": "仅属于产品2",
            "product_count": 2, "screenshot_meta1": {"slices": []}, "screenshot_meta2": {"slices": []}}
    bundle = {"target_turn": 1, "turns": [turn], "profile": SimpleNamespace(), "limits": {},
              "diagnostics": {"history_prefix_sha256": "history"}}
    result = conversation_prompt.assemble_conversation(
        "评分规则", bundle, resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID))
    text = "\n".join(part["text"] for part in result[1] if part["type"] == "text")
    assert "仅属于产品1" not in text.split("产品1自身轨迹开始", 1)[0]
    assert text.count("仅属于产品1") == 1
    assert "仅属于产品1" not in text.split("产品2自身轨迹开始", 1)[1]
    turn["context1"] = ""
    with pytest.raises(ValueError, match="context1"):
        conversation_prompt.assemble_conversation(
            "评分规则", bundle, resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID))
