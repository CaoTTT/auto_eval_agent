"""The relative contract never turns unknown evidence into a tie or a score."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from auto_eval.judges.chitchat_gsb_schema import (
    CHITCHAT_GSB_COMPARE_PROTOCOL_ID, CHITCHAT_GSB_RESULT_SCHEMA_VERSION,
    CHITCHAT_GSB_STANDARD_ID, ChitchatGSBObservation, active_product_pairs,
    expected_output_keys, finalize_chitchat_gsb_observation, parse_output, validate_output,
)


def pair(gsb="G", *, status="evaluated", serious_issue="none"):
    return {"status": status, "gsb": gsb, "reason": "产品1回应了明确的倾诉需求；产品2主要安排任务，偏离当前目标。",
            "serious_issue": serious_issue}


def payload(count=2):
    return {key: pair() for key in expected_output_keys(count)}


def protocol():
    return SimpleNamespace(
        standard_id=CHITCHAT_GSB_STANDARD_ID,
        standard_version="1.0", id=CHITCHAT_GSB_COMPARE_PROTOCOL_ID,
        bundle_revision="1.0.0", result_schema_version=CHITCHAT_GSB_RESULT_SCHEMA_VERSION,
    )


@pytest.mark.parametrize("count,pairs", [(2, ((1, 2),)), (3, ((1, 2), (1, 3), (2, 3)))])
def test_active_pairs_round_trip_with_no_inactive_placeholders(count, pairs):
    source = payload(count)
    observation = parse_output(json.dumps(source, ensure_ascii=False), count)
    assert active_product_pairs(count) == pairs
    assert observation.product_count == count
    assert observation.model_dump() == source
    assert json.loads(observation.model_dump_json()) == source
    assert set(observation.model_dump()) == set(expected_output_keys(count))


@pytest.mark.parametrize("count", [None, True, False, 2.0, "2", 0, 1, 4])
def test_product_count_must_be_explicit_strict_integer(count):
    with pytest.raises(ValueError):
        parse_output(json.dumps(payload()), count)


@pytest.mark.parametrize("status,gsb", [
    ("evaluated", "G"), ("evaluated", "S"), ("evaluated", "B"),
    ("insufficient_evidence", None), ("needs_review", None),
])
def test_status_and_verdict_have_distinct_meaning(status, gsb):
    source = {"pair_1_2": pair(gsb, status=status)}
    assert validate_output(source, 2).model_dump() == source


@pytest.mark.parametrize("status,gsb", [
    ("evaluated", None), ("insufficient_evidence", "S"),
    ("insufficient_evidence", "B"), ("needs_review", "G"),
    ("evaluated", ""), ("evaluated", "g"), ("evaluated", "NA"),
    ("evaluated", True), ("evaluated", 1), ("unknown", None),
])
def test_missing_evidence_cannot_be_coerced_into_a_tie(status, gsb):
    with pytest.raises(ValueError):
        validate_output({"pair_1_2": pair(gsb, status=status)}, 2)


@pytest.mark.parametrize("bad", [None, {}, [], {"pair_1_2": None}, {"pair_2_1": pair()},
                                 {"pair_1_2": pair(), "pair_1_3": None},
                                 {"pair_1_2": pair(), "score": 5}])
def test_only_complete_active_pair_objects_are_allowed(bad):
    with pytest.raises(ValueError):
        validate_output(bad, 2)


@pytest.mark.parametrize("field", ["status", "gsb", "reason", "serious_issue"])
def test_every_pair_field_is_required(field):
    source = payload()
    source["pair_1_2"].pop(field)
    with pytest.raises(ValueError):
        validate_output(source, 2)


@pytest.mark.parametrize("field,value", [
    ("reason", " "), ("reason", None), ("reason", False), ("reason", 7),
    ("reason", b"text"), ("serious_issue", "product1"),
    ("serious_issue", None), ("score", 5), ("product_a", 1),
])
def test_nested_contract_is_strict_and_does_not_accept_extra_scores(field, value):
    source = payload()
    source["pair_1_2"][field] = value
    with pytest.raises(ValueError):
        validate_output(source, 2)


def test_three_products_require_all_three_pairs_without_imposing_a_ranking():
    source = payload(3)
    source["pair_1_2"]["gsb"] = "G"
    source["pair_1_3"]["gsb"] = "B"
    source["pair_2_3"]["gsb"] = "G"
    assert validate_output(source, 3).model_dump() == source
    source.pop("pair_1_3")
    with pytest.raises(ValueError):
        validate_output(source, 3)


def test_both_bad_and_similar_is_a_valid_relative_tie():
    source = {"pair_1_2": pair("S", serious_issue="both")}
    final = finalize_chitchat_gsb_observation(validate_output(source, 2), protocol())
    assert final["gsb_pairs"]["1_2"]["gsb"] == "S"
    assert final["gsb_pairs"]["1_2"]["serious_issue"] == "both"


@pytest.mark.parametrize("side,gsb", [("A", "G"), ("A", "S"), ("B", "B"), ("B", "S")])
def test_one_sided_serious_problem_cannot_be_ignored_or_offset_by_style(side, gsb):
    with pytest.raises(ValueError, match="仅单方有严重问题"):
        validate_output({"pair_1_2": pair(gsb, serious_issue=side)}, 2)


@pytest.mark.parametrize("side,gsb", [("A", "B"), ("B", "G"), ("both", "G"), ("both", "B")])
def test_serious_problem_direction_is_consistent_and_both_bad_can_differ(side, gsb):
    source = {"pair_1_2": pair(gsb, serious_issue=side)}
    assert validate_output(source, 2).model_dump() == source


@pytest.mark.parametrize("raw", [
    '{"pair_1_2":{},"pair_1_2":{}}',
    '{"pair_1_2":{"status":"evaluated","gsb":"G","gsb":"S","reason":"r","serious_issue":"none"}}',
    '{"pair_1_2":{"status":"evaluated","gsb":"G","reason":"r","serious_issue":"none","x":NaN}}',
    '{"pair_1_2":{"status":"evaluated","gsb":"G","reason":"r","serious_issue":"none","x":Infinity}}',
    '{"pair_1_2":{"status":"evaluated","gsb":"G","reason":"r","serious_issue":"none","x":1e999}}',
    '```json\n{}\n```', '{"pair_1_2":', '', ' ', 'null', '[]',
])
def test_json_parse_rejects_duplicates_nonfinite_values_and_format_repair(raw):
    with pytest.raises(ValueError):
        parse_output(raw, 2)


def test_finalization_preserves_raw_and_does_not_project_absolute_scores():
    source = payload(3)
    observation = validate_output(source, 3)
    final = finalize_chitchat_gsb_observation(observation, protocol())
    assert final["model_pair_results"] == source
    assert observation.model_dump() == source
    assert set(final["gsb_pairs"]) == {"1_2", "1_3", "2_3"}
    assert final["gsb_pairs"]["1_3"] == {**source["pair_1_3"], "product_a": 1, "product_b": 3}
    assert final["needs_review"] is False
    assert final["evaluation_profile"] == CHITCHAT_GSB_COMPARE_PROTOCOL_ID
    assert not any("score" in key or "gate" in key or "winner" in key or "rank" in key for key in final)


def test_trusted_collection_failure_only_excludes_affected_pairs():
    source = payload(3)
    source["pair_1_2"]["serious_issue"] = "both"
    source["pair_2_3"]["serious_issue"] = "A"
    source["pair_2_3"]["gsb"] = "B"
    before = deepcopy(source)
    metadata = {"answer2": {"input_status": "failed", "source": "capture"}}
    observation = ChitchatGSBObservation.model_validate(
        source, context={"product_count": 3, "trusted_collection_metadata": metadata})
    final = finalize_chitchat_gsb_observation(observation, protocol())
    for key in ("1_2", "2_3"):
        assert final["gsb_pairs"][key]["status"] == "insufficient_evidence"
        assert final["gsb_pairs"][key]["gsb"] is None
        assert "模型原理由：" in final["gsb_pairs"][key]["reason"]
    assert final["gsb_pairs"]["1_2"]["serious_issue"] == "A"
    assert final["gsb_pairs"]["2_3"]["serious_issue"] == "none"
    assert final["gsb_pairs"]["1_3"]["gsb"] == "G"
    assert final["gsb_pairs"]["1_3"]["status"] == "evaluated"
    assert final["model_pair_results"] == before
    assert source == before and observation.model_dump() == before
    assert final["needs_human_review"] is True
    assert len(final["normalization_diagnostics"]) == 2


def test_partial_collection_does_not_automatically_cancel_a_supported_verdict():
    source = {"pair_1_2": pair("B", serious_issue="A")}
    metadata = {"answer1": {"input_status": "partial", "source": "capture"}}
    final = finalize_chitchat_gsb_observation(validate_output(source, 2), protocol(),
                                            trusted_collection_metadata=metadata)
    assert final["gsb_pairs"]["1_2"] == {**source["pair_1_2"], "product_a": 1, "product_b": 2}
    assert final["trusted_collection_metadata"] == metadata
    assert final["needs_review"] is False


@pytest.mark.parametrize("metadata", [
    {"answer3": {"input_status": "failed", "source": "capture"}},
    {"answer1": {"input_status": "failed"}},
    {"answer1": {"input_status": "failed", "source": " "}},
    {"answer1": {"input_status": "broken", "source": "capture"}},
])
def test_finalization_validates_only_program_supplied_collection_metadata(metadata):
    with pytest.raises(ValueError):
        finalize_chitchat_gsb_observation(validate_output(payload(), 2), protocol(),
                                         trusted_collection_metadata=metadata)


def test_pending_pair_is_preserved_and_explained_for_review():
    source = payload(3)
    source["pair_1_3"] = pair(None, status="needs_review")
    source["pair_2_3"] = pair(None, status="insufficient_evidence")
    final = finalize_chitchat_gsb_observation(validate_output(source, 3), protocol())
    assert final["gsb_pairs"]["1_2"]["gsb"] == "G"
    assert final["gsb_pairs"]["1_3"]["gsb"] is None
    assert final["gsb_pairs"]["2_3"]["gsb"] is None
    assert len(final["review_reasons"]) == 2
    assert "待复核" in final["review_reason"] and "待补证据" in final["review_reason"]
