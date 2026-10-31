"""Only lossless local formatting recovery may precede strict chat validation."""
from copy import deepcopy
import json

import pytest

from auto_eval.judges.chitchat_flat_recovery import recover_chitchat_flat_output
from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_DIMENSIONS, ChitchatFlatObservation, expected_output_keys,
)


def sample(count=2):
    result = {}
    for index, dimension in enumerate(CHITCHAT_FLAT_DIMENSIONS):
        result[dimension + "_reason"] = "；".join(
            f"产品{number}：合成证据{index}-{number}，保留原判断🙂"
            for number in range(1, count + 1)
        )
        for number in range(1, count + 1):
            score = (1 if number == 1 else 5) if index < 2 else (index + number) % 5 + 1
            result[f"answer{number}_{dimension}_score"] = score
    result["answer2_style_score"] = None
    return result


def dump(source):
    return json.dumps(source, ensure_ascii=False)


def aliases(source):
    names = {
        f"answer{number}_{dimension}_score": f"{dimension}_answer{number}_score"
        for dimension in CHITCHAT_FLAT_DIMENSIONS for number in (1, 2, 3)
    }
    return {names.get(key, key): value for key, value in source.items()}


def unescape_fragment(source, field, fragment):
    encoded = json.dumps(source[field], ensure_ascii=False)
    escaped_fragment = json.dumps(fragment, ensure_ascii=False)[1:-1]
    assert escaped_fragment in encoded
    return dump(source).replace(encoded, encoded.replace(escaped_fragment, fragment, 1), 1)


def assert_preserved(recovered, expected, count):
    assert isinstance(recovered["observation"], ChitchatFlatObservation)
    assert recovered["observation"].product_count == count
    assert recovered["observation"].model_dump() == expected
    assert tuple(recovered["observation"].model_dump()) == expected_output_keys(count)
    assert isinstance(recovered["raw_key_pairs"], list)
    assert isinstance(recovered["locally_repaired_key_pairs"], list)
    assert isinstance(recovered["local_format_recovery_steps"], list)
    assert all(isinstance(step, str) and step.strip() for step in recovered["local_format_recovery_steps"])


@pytest.mark.parametrize("count", [2, 3])
def test_legal_output_is_unchanged_and_has_no_recovery_steps(count):
    source = sample(count)
    source["style_reason"] = '产品1：合法转义"原话"，路径C:\\notes；产品2：保留\\与🙂'
    raw = dump(source)
    before = deepcopy(source)
    recovered = recover_chitchat_flat_output(raw, count)
    assert_preserved(recovered, before, count)
    assert dict(recovered["raw_key_pairs"]) == before
    assert recovered["local_format_recovery_steps"] == []
    assert recovered["locally_repaired_key_pairs"] == []
    assert source == before and raw == dump(before)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("fragment", ['"合成引文"', '"第一处"与"第二处"'])
def test_unescaped_internal_quotes_preserve_every_reason_and_score(count, fragment):
    source = sample(count)
    source["understanding_reason"] = f"产品1：可见{fragment}后承接；产品2：保持独立判断"
    if count == 3:
        source["understanding_reason"] += "；产品3：同样完整"
    raw = unescape_fragment(source, "understanding_reason", fragment)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    recovered = recover_chitchat_flat_output(raw, count)
    assert_preserved(recovered, source, count)
    assert recovered["local_format_recovery_steps"]
    assert recovered["raw_key_pairs"] == []
    assert dict(recovered["locally_repaired_key_pairs"]) == source


@pytest.mark.parametrize("count", [2, 3])
def test_unpaired_internal_quote_is_rejected_instead_of_guessing_a_string_boundary(count):
    source = sample(count)
    source["understanding_reason"] = '产品1：可见5"屏幕字样；产品2：保持独立判断'
    raw = unescape_fragment(source, "understanding_reason", '5"屏幕')
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(raw, count)


def test_multiple_reason_fields_can_be_recovered_without_cross_field_changes():
    source = sample()
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        source[dimension + "_reason"] = f'产品1：引用"{dimension}第一段"；产品2：引用"{dimension}第二段"'
    raw = dump(source).replace(r'\"', '"')
    recovered = recover_chitchat_flat_output(raw, 2)
    assert_preserved(recovered, source, 2)
    assert recovered["raw_key_pairs"] == []
    assert dict(recovered["locally_repaired_key_pairs"]) == source


def test_escaped_quotes_backslashes_and_unescaped_quotes_can_coexist():
    source = sample()
    source["style_reason"] = '产品1：保留"已转义部分"、路径C:\\notes、换行\n及"待恢复部分"；产品2：原文不改'
    raw = unescape_fragment(source, "style_reason", '"待恢复部分"')
    assert r'\"已转义部分\"' in raw and '"待恢复部分"' in raw
    recovered = recover_chitchat_flat_output(raw, 2)
    assert_preserved(recovered, source, 2)
    assert recovered["observation"].style_reason == source["style_reason"]


@pytest.mark.parametrize("count", [2, 3])
def test_whitelisted_score_aliases_map_all_active_fields_without_touching_values(count):
    source = sample(count)
    wire = aliases(source)
    recovered = recover_chitchat_flat_output(dump(wire), count)
    assert_preserved(recovered, source, count)
    assert dict(recovered["raw_key_pairs"]) == wire
    assert dict(recovered["locally_repaired_key_pairs"]) == source
    steps = "\n".join(recovered["local_format_recovery_steps"])
    assert steps
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        for number in range(1, count + 1):
            assert f"{dimension}_answer{number}_score" in steps
            assert f"answer{number}_{dimension}_score" in steps


def test_quote_recovery_and_alias_mapping_are_composable_and_audited():
    source = sample()
    source["emotional_expression_reason"] = '产品1：引用"甲"与"乙"；产品2：保留原文'
    wire = aliases(source)
    raw = unescape_fragment(wire, "emotional_expression_reason", '"甲"与"乙"')
    recovered = recover_chitchat_flat_output(raw, 2)
    assert_preserved(recovered, source, 2)
    assert recovered["raw_key_pairs"] == []
    assert dict(recovered["locally_repaired_key_pairs"]) == source
    assert len(recovered["local_format_recovery_steps"]) >= 2
    assert "understanding_answer1_score" in "\n".join(recovered["local_format_recovery_steps"])


def test_alias_looking_text_in_reasons_is_never_renamed():
    source = sample()
    source["understanding_reason"] = '产品1：原文字段understanding_answer1_score及"style_answer2_score"；产品2：不改文字'
    wire = aliases(source)
    recovered = recover_chitchat_flat_output(dump(wire), 2)
    assert_preserved(recovered, source, 2)


@pytest.mark.parametrize("fragment", ['"字段":5', '{"字段":"原文"}', '"answer1_style_score":1'])
def test_json_looking_unescaped_fragments_are_rejected_as_ambiguous(fragment):
    source = sample()
    source["style_reason"] = f"产品1：原文{fragment}后继续；产品2：保持独立判断"
    raw = unescape_fragment(source, "style_reason", fragment)
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(raw, 2)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("kind", ["canonical_then_alias", "alias_then_canonical", "duplicate_canonical", "duplicate_alias"])
def test_duplicate_or_alias_collision_is_rejected_even_when_values_are_equal(count, kind):
    source = sample(count)
    canonical, alias = "answer1_understanding_score", "understanding_answer1_score"
    value = source[canonical]
    if kind == "canonical_then_alias":
        raw = dump(source)[:-1] + f', "{alias}": {value}}}'
    elif kind == "alias_then_canonical":
        raw = dump(aliases(source))[:-1] + f', "{canonical}": {value}}}'
    elif kind == "duplicate_canonical":
        raw = dump(source)[:-1] + f', "{canonical}": {value}}}'
    else:
        raw = dump(aliases(source))[:-1] + f', "{alias}": {value}}}'
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(raw, count)


@pytest.mark.parametrize("field,value", [
    ("answer1_understanding_score", True),
    ("answer1_understanding_score", "3"),
    ("answer1_understanding_score", 3.0),
    ("answer1_understanding_score", -1),
    ("answer1_understanding_score", 6),
    ("answer1_response_experience_score", 2),
    ("answer2_safety_stability_score", 4),
    ("understanding_reason", " \n "),
    ("understanding_reason", 12),
    ("understanding_reason", {"text": "不能展平"}),
])
@pytest.mark.parametrize("use_alias", [False, True])
def test_local_recovery_never_coerces_invalid_scores_or_reasons(field, value, use_alias):
    source = sample()
    source[field] = value
    wire = aliases(source) if use_alias else source
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(dump(wire), 2)


@pytest.mark.parametrize("kind", [
    "missing_reason", "missing_score", "unknown_dimension", "unknown_field", "wrong_alias_suffix",
    "wrong_product", "extra_reason", "out_of_count_alias", "out_of_count_canonical",
])
def test_missing_unknown_or_out_of_count_keys_are_not_invented_or_discarded(kind):
    source = sample()
    if kind == "missing_reason":
        del source["understanding_reason"]
    elif kind == "missing_score":
        del source["answer2_style_score"]
    else:
        field = {
            "unknown_dimension": "personalization_answer1_score",
            "unknown_field": "raw_model_output",
            "wrong_alias_suffix": "understanding_answer1_rating",
            "wrong_product": "understanding_answer01_score",
            "extra_reason": "answer1_understanding_reason",
            "out_of_count_alias": "understanding_answer3_score",
            "out_of_count_canonical": "answer3_understanding_score",
        }[kind]
        source[field] = 5
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(dump(source), 2)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_numbers_are_rejected_before_and_after_alias_mapping(constant):
    for wire in (sample(), aliases(sample())):
        score = "answer1_understanding_score" if "answer1_understanding_score" in wire else "understanding_answer1_score"
        raw = dump(wire).replace(f'"{score}": {wire[score]}', f'"{score}": {constant}', 1)
        with pytest.raises(ValueError):
            recover_chitchat_flat_output(raw, 2)


@pytest.mark.parametrize("kind", [
    "empty", "truncated_object", "truncated_reason", "second_object", "prefix", "suffix",
    "known_prefix", "known_suffix", "reasoning_prefix", "bom", "markdown", "array",
    "single_quotes", "trailing_comma", "missing_comma", "bad_escape", "unescaped_newline",
])
def test_no_truncation_wrapper_or_unrelated_json_syntax_repair(kind):
    source = sample()
    raw = dump(source)
    raw = {
        "empty": "",
        "truncated_object": raw[:-1],
        "truncated_reason": raw[:raw.index('"answer1_understanding_score"') - 5],
        "second_object": raw + "\n" + raw,
        "prefix": "模型结果如下：" + raw,
        "suffix": raw + "额外说明",
        "known_prefix": "以下是 JSON 结果：" + raw,
        "known_suffix": raw + "以上为JSON结果。",
        "reasoning_prefix": "<think>合成思考文本</think>" + raw,
        "bom": "\ufeff" + raw,
        "markdown": "```json\n" + raw + "\n```",
        "array": "[" + raw + "]",
        "single_quotes": repr(source),
        "trailing_comma": raw[:-1] + ",}",
        "missing_comma": raw.replace(', "answer1_understanding_score"', ' "answer1_understanding_score"', 1),
        "bad_escape": raw.replace("合成证据", r"错误\q转义", 1),
        "unescaped_newline": raw.replace("合成证据", "非法\n换行", 1),
    }[kind]
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(raw, 2)


@pytest.mark.parametrize("count", [None, True, 2.0, "2", 1, 4])
def test_product_count_validation_remains_strict(count):
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(dump(sample()), count)


@pytest.mark.parametrize("raw", [None, 12, {}, []])
def test_non_text_input_is_rejected(raw):
    with pytest.raises(ValueError):
        recover_chitchat_flat_output(raw, 2)


def test_legal_json_whitespace_does_not_count_as_a_recovery():
    source = sample()
    recovered = recover_chitchat_flat_output(" \t\r\n" + dump(source) + "\r\n\t ", 2)
    assert_preserved(recovered, source, 2)
    assert recovered["local_format_recovery_steps"] == []
    assert recovered["locally_repaired_key_pairs"] == []
