import json

import pytest

from auto_eval.judges.json_recovery import JsonRecoveryError, recover_json_object


def test_valid_object_keeps_values_order_and_needs_no_recovery():
    expected = {"status": "pass", "score": 5, "reason": "产品1：证据完整", "empty": None}
    result = recover_json_object(json.dumps(expected, ensure_ascii=False))
    assert result.data == expected
    assert list(result.data) == list(expected)
    assert result.steps == ()


@pytest.mark.parametrize("wrap,step", [
    (lambda text: "\ufeff" + text, "strip_bom"),
    (lambda text: "```json\n" + text + "\n```", "strip_json_fence"),
    (lambda text: "```JSON\r\n" + text + "\r\n```", "strip_json_fence"),
    (lambda text: "```\n" + text + "\n```", "strip_json_fence"),
    (lambda text: "以下是JSON结果：\n" + text, "strip_json_prefix"),
    (lambda text: "Here is the JSON:\n" + text + "\nEnd of JSON.", "strip_json_prefix"),
    (lambda text: "<think>不作为评分结果的过程</think>\n" + text, "strip_completed_reasoning_prefix"),
    (lambda text: "<analysis>analysis</analysis>\n```json\n" + text + "\n```", "strip_json_fence"),
])
def test_only_completed_known_transport_wrappers_are_removed(wrap, step):
    expected = {"reason": "文本中的 ``` 和 {大括号} 保留", "score": 4}
    result = recover_json_object(wrap(json.dumps(expected, ensure_ascii=False)))
    assert result.data == expected
    assert step in result.steps


@pytest.mark.parametrize("raw,expected", [
    ('{"score":4,}', {"score": 4}),
    ('{"details":[1,2,],"nested":{"ok":true,},}', {"details": [1, 2], "nested": {"ok": True}}),
    ('{"reason":"逗号,}位于文本中，不能删除",}', {"reason": "逗号,}位于文本中，不能删除"}),
])
def test_trailing_commas_are_removed_only_outside_strings(raw, expected):
    result = recover_json_object(raw)
    assert result.data == expected
    assert "remove_trailing_comma" in result.steps


def test_literal_string_controls_preserve_the_original_string_value():
    result = recover_json_object('{"reason":"第一行\n第二行\t制表\u0001", "score":3}')
    assert result.data == {"reason": "第一行\n第二行\t制表\u0001", "score": 3}
    assert result.steps == ("allow_literal_string_controls",)


@pytest.mark.parametrize("raw,expected", [
    ('{"score":4 "reason":"产品1：完整回答"}', {"score": 4, "reason": "产品1：完整回答"}),
    ('{"gate":"pass""score":5}', {"gate": "pass", "score": 5}),
    ('{"nested":{"ok":true} "score":2}', {"nested": {"ok": True}, "score": 2}),
    ('{"applicable":false\n "score":null}', {"applicable": False, "score": None}),
])
def test_missing_object_member_comma_does_not_modify_fields_or_values(raw, expected):
    result = recover_json_object(raw)
    assert result.data == expected
    assert "insert_missing_object_comma" in result.steps


@pytest.mark.parametrize("raw,expected", [
    ('{"reason":"产品说"附近餐厅"，并给出地址","score":4}',
     {"reason": '产品说"附近餐厅"，并给出地址', "score": 4}),
    ('{"reason":"先说"甲"再说"乙"，均可见"}',
     {"reason": '先说"甲"再说"乙"，均可见'}),
    ('{"reason":"say "hello" clearly","score":3,}',
     {"reason": 'say "hello" clearly', "score": 3}),
])
def test_paired_unescaped_quotes_in_plain_text_can_be_escaped_without_losing_text(raw, expected):
    result = recover_json_object(raw)
    assert result.data == expected
    assert result.steps.count("escape_internal_quote") in (2, 4)


@pytest.mark.parametrize("reason", [
    '该范围以"接近""上下"描述，并未承诺固定结果。',
    '按"试用版""普及版""正式版"三个阶段说明。',
    '先以"大致可行""信息待确认"作为概括，再分别展开。',
    '原文为"左,右"和"`提示`"，标点保留。',
])
def test_adjacent_quoted_phrases_keep_all_literal_quotes_and_punctuation(reason):
    result = recover_json_object('{"reason":"' + reason + '","score":4}')
    assert result.data == {"reason": reason, "score": 4}
    assert result.steps.count("escape_internal_quote") >= 4


@pytest.mark.parametrize("raw", [
    "", " \n\t", "\ufeff", "<think>", "<think>still thinking", "<think>done</think>",
    "<analysis>only analysis</analysis>",
    "```json", '```json\n{"score":4}', '```json\n{"score":4}\n',
    '```json\n{"score":4}\n```\nextra answer',
    'before {"score":4}', '{"score":4} after',
    '{"score":4}{"score":5}', '{"score":4}\n{"other":1}',
    '[{"score":4}]', "[]", '"text"', "5", "true", "null",
    '{"score":', '{"score":4', '{"reason":"unfinished}', '{"a":[1,2}',
    '{"score":4,,}', '{"a":,}', '{,"a":1}', '{"a":[,]}',
    '{"score":4.5.6}', '{"score":4 5}', '{"a":["one" "two"]}',
    '{"a":["one""two"]}', '{"reason":"one" "two"}', '{"reason":"one""two"}',
    '{"reason":"partial "quoted", "score":4}',
    '{"reason":"JSON片段 "score": "5"}',
    '{"reason":"JSON片段 "score": "5", "extra":"value"}',
    '{"score":\'4\'}', '{score:4}',
    '```json\n{"score":4}\n{"score":5}\n```',
    '以下是JSON结果：先解释一下 {"score":4}',
])
def test_ambiguous_incomplete_nonobject_or_extra_output_is_rejected(raw):
    with pytest.raises(JsonRecoveryError):
        recover_json_object(raw)


@pytest.mark.parametrize("raw", [
    '{"score":4,"score":5}',
    '{"nested":{"score":4,"score":5}}',
    '{"score":4,"\\u0073core":5}',
    '{"a":[{"score":4,"score":5}]}',
    '{"score":4 "score":5}',
    '{"score":NaN}', '{"score":Infinity}', '{"score":-Infinity}',
    '{"score":1e400}', '{"a":[NaN]}',
])
def test_duplicate_keys_and_nonfinite_values_are_never_hidden_by_recovery(raw):
    with pytest.raises(JsonRecoveryError):
        recover_json_object(raw)


def test_wrapping_and_multiple_format_issues_keep_all_data_in_order():
    raw = '\ufeff以下是JSON结果：\n```json\n{"gate":"pass" "reason":"产品说"附近餐厅"，可继续办理" "score":4,}\n```'
    result = recover_json_object(raw)
    assert result.data == {"gate": "pass", "reason": '产品说"附近餐厅"，可继续办理', "score": 4}
    assert list(result.data) == ["gate", "reason", "score"]
    assert set(result.steps) == {"strip_bom", "strip_json_prefix", "strip_json_fence",
                                 "insert_missing_object_comma", "escape_internal_quote", "remove_trailing_comma"}


def test_json_like_text_in_an_already_valid_string_is_not_changed():
    expected = {"reason": '引用原话：{"score": 5, "gate": "pass"}，只是文本', "score": 3}
    result = recover_json_object(json.dumps(expected, ensure_ascii=False))
    assert result.data == expected
    assert result.steps == ()


def test_newline_and_escape_characters_are_preserved_with_a_missing_member_comma():
    reason = '产品1：路径 C:\\work\\file；\n引用"原话"；制表\t不丢失'
    raw = '{"reason":' + json.dumps(reason, ensure_ascii=False) + ' "score":4}'
    result = recover_json_object(raw)
    assert result.data == {"reason": reason, "score": 4}
    assert result.steps == ("insert_missing_object_comma",)


def test_non_text_and_excessively_deep_input_fail_as_recovery_errors():
    for raw in (None, {}, 1):
        with pytest.raises(JsonRecoveryError):
            recover_json_object(raw)
    with pytest.raises(JsonRecoveryError):
        recover_json_object('{"a":' + "[" * 2000 + "0" + "]" * 2000 + "}")
