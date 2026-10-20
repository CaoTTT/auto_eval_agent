"""Timeliness contract and state precedence; no real judge requests."""
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID, list_compare_protocols, resolve_compare_protocol,
)
from auto_eval.judges.timeliness_schema import (
    TIMELINESS_COMPARE_PROTOCOL_ID as PROFILE, TIMELINESS_DIMENSIONS as DIMS,
    TimelinessObservation, allowed_output_keys, expected_output_keys, output_key_errors,
)
from auto_eval.judges.timeliness_schema_r100 import TimelinessObservation as FrozenObservation
from auto_eval.judges.timeliness_schema_r101 import TimelinessObservation as FrozenCompactObservation
from auto_eval.judges.visual_compare_judge import visual_compare_result_fields
from auto_eval.judges.visual_compare_prompt_timeliness import (
    SOURCE_ANCHORS, output_example, render_system_prompt,
)
from auto_eval.judges.visual_compare_prompt_timeliness_r100 import (
    SOURCE_ANCHORS as FROZEN_SOURCE_ANCHORS, output_example as frozen_output_example,
    render_system_prompt as render_frozen_system_prompt,
)
from auto_eval.judges.visual_compare_prompt_timeliness_r101 import (
    output_example as frozen_compact_output_example,
    render_system_prompt as render_frozen_compact_system_prompt,
)


def payload(count=2):
    return json.loads(output_example(count))


def finalize(data, count=2, trusted=None):
    observation = TimelinessObservation.model_validate(
        data, context={"product_count": count, "trusted_collection_metadata": trusted})
    before = observation.model_dump()
    result = visual_compare_result_fields(observation, PROFILE)
    assert observation.model_dump() == before, "normalization must preserve the raw observation"
    return result


def test_registry_preserves_default_and_pins_new_protocol():
    protocol = resolve_compare_protocol(PROFILE, "1.0.2")
    assert protocol.display == "时效性评估V1·1-3分（候选）"
    assert protocol.observation_model is TimelinessObservation
    assert protocol.score_dimensions == DIMS
    assert protocol.result_schema_version == "timeliness-result-3"
    assert resolve_compare_protocol(PROFILE) == protocol
    assert sum(p.id == PROFILE for p in list_compare_protocols()) == 1
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    with pytest.raises(ValueError):
        resolve_compare_protocol(PROFILE, "2.0.0")


@pytest.mark.parametrize("count", [2, 3])
def test_legacy_revision_retains_old_model_prompt_and_results(count):
    protocol = resolve_compare_protocol(PROFILE, "1.0.0")
    assert protocol.observation_model is FrozenObservation
    assert protocol.result_schema_version == "timeliness-result-1"
    data = json.loads(frozen_output_example(count))
    assert len(data) == 13 * count + 6
    observation = protocol.observation_model.model_validate(data, context={"product_count": count})
    result = visual_compare_result_fields(observation, protocol)
    assert result["answer1_authority_verification_status"] == "verified"
    assert result["bundle_revision"] == "1.0.0"
    assert result["result_schema_version"] == "timeliness-result-1"
    assert protocol.system_template.render(product_count=count) == render_frozen_system_prompt(product_count=count)
    assert SOURCE_ANCHORS == FROZEN_SOURCE_ANCHORS
    with pytest.raises(ValidationError):
        TimelinessObservation.model_validate(data, context={"product_count": count})
    with pytest.raises(ValidationError):
        FrozenObservation.model_validate(payload(count), context={"product_count": count})


@pytest.mark.parametrize("count", [2, 3])
def test_frozen_compact_revision_retains_its_own_contract(count):
    protocol = resolve_compare_protocol(PROFILE, "1.0.1")
    assert protocol.observation_model is FrozenCompactObservation
    assert protocol.result_schema_version == "timeliness-result-2"
    data = json.loads(frozen_compact_output_example(count))
    assert len(data) == 7 * count + 6
    observation = protocol.observation_model.model_validate(data, context={"product_count": count})
    result = visual_compare_result_fields(observation, protocol)
    assert result["bundle_revision"] == "1.0.1"
    assert result["result_schema_version"] == "timeliness-result-2"
    assert "has_conflict" not in result and "conflict_reason" not in result
    assert protocol.system_template.render(product_count=count) == render_frozen_compact_system_prompt(
        product_count=count)
    with pytest.raises(ValidationError, match="缺少字段"):
        TimelinessObservation.model_validate(data, context={"product_count": count})
    with pytest.raises(ValidationError, match="多余字段"):
        FrozenCompactObservation.model_validate(payload(count), context={"product_count": count})


@pytest.mark.parametrize("name,original_sha256", [
    ("timeliness_schema", "18d43bacf8d098f63b15d2e70143015c81ad5e5235d8d300638d7cabb4417682"),
    ("visual_compare_prompt_timeliness", "08c3b1ed4f701a9338531e2b746cd8d89ecd43f5ec24f31ed3c40b1e1d92f941"),
])
def test_frozen_compact_sources_match_pre_conflict_revision(name, original_sha256):
    # Pins the exact pre-upgrade sources without requiring Git in deployed tests.
    source = Path(f"src/auto_eval/judges/{name}_r101.py").read_text(encoding="utf-8")
    source = source.replace("from .timeliness_schema_r101 import (", "from .timeliness_schema import (")
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == original_sha256


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
def test_exact_single_response_shape_and_reason_order(count, mode):
    data = payload(count)
    assert tuple(data) == expected_output_keys(count)
    assert len(data) == 7 * count + 8
    assert len(allowed_output_keys(count)) == 9 * count + 8
    assert list(data)[-2:] == ["conflict_reason", "has_conflict"]
    system = render_system_prompt(product_count=count, evidence_mode=mode)
    assert json.loads(system[system.index('{\n  "answer1_response_gate_reason"'):]) == data
    assert "R20" in system and "C20" in system and "S3" in system
    result = finalize(data, count)
    for dim in DIMS:
        for p in range(1, count + 1):
            assert list(data).index(dim + "_reason") < list(data).index(f"answer{p}_{dim}_score")
            assert result[f"answer{p}_{dim}_score_status"] == "scored"
            assert result[f"answer{p}_{dim}_verification_status"] is None
            assert result[f"answer{p}_{dim}_verification_status_source"] == "not_collected"
            assert f"answer{p}_{dim}_na_reason" not in data
            assert f"answer{p}_{dim}_na_reason" not in result
    assert not any("winner" in k or "rank" in k or k == "total_score" for k in result)
    assert ("answer3_understanding_score" in result) == (count == 3)


@pytest.mark.parametrize("state", ["yes", "no", "unclear"])
@pytest.mark.parametrize("count", [2, 3])
def test_conflict_status_retains_observation_without_changing_scores(state, count):
    data = {**payload(count), "has_conflict": state}
    data["conflict_reason"] = "产品1与产品2：同一政策日期的可见证据需按原话核对。"
    result = finalize(data, count)
    assert result["has_conflict"] == state
    assert result["conflict_reason"] == data["conflict_reason"]
    assert result["needs_human_review"] is (state == "unclear")
    assert bool(result["review_reasons"]) is (state == "unclear")
    for product in range(1, count + 1):
        for dimension in DIMS:
            key = f"answer{product}_{dimension}_score"
            assert result[key] == data[key] == 3
    assert not any("conflict_score" in key for key in result)


@pytest.mark.parametrize("value", [None, True, False, "YES", "NA", "", 1, "unknown"])
def test_conflict_status_is_strict_enum(value):
    with pytest.raises(ValidationError):
        finalize({**payload(), "has_conflict": value})


@pytest.mark.parametrize("value", [None, "", " ", "\n\t", 1, True, []])
def test_conflict_reason_requires_nonblank_strict_text(value):
    with pytest.raises(ValidationError):
        finalize({**payload(), "conflict_reason": value})


@pytest.mark.parametrize("status", ["yes", "no", "unclear"])
def test_trusted_capture_failure_requests_review_without_rewriting_group_conflict(status):
    data = payload(3)
    reasons = {
        "yes": "产品2与产品3：同日、同一园区分别明确称09:00和08:00开园。",
        "no": "产品2与产品3：同日、同一园区均明确称09:00开园；产品1没有可比陈述。",
        "unclear": "产品2与产品3开园时间不同，但产品3没有明确测试日期，不能排除日期不同。",
    }
    data.update(conflict_reason=reasons[status],
                has_conflict=status)
    trusted = {"answer1": {"input_status": "failed", "source": "test collector"}}
    result = finalize(data, count=3, trusted=trusted)
    assert result["has_conflict"] == status
    assert result["conflict_reason"] == data["conflict_reason"]
    assert result["needs_human_review"]
    assert "产品1可信采集状态为failed" in result["review_reasons"]
    for dimension in DIMS:
        assert result[f"answer1_{dimension}_score"] is None
        assert result[f"answer2_{dimension}_score"] == result[f"answer3_{dimension}_score"] == 3


@pytest.mark.parametrize("score", [0, 4, True, False, 2.0, "2", -1])
def test_invalid_scores_rejected_even_when_blocked(score):
    data = payload()
    data.update(answer1_response_gate="fail", answer1_understanding_score=score)
    with pytest.raises(ValidationError):
        finalize(data)


@pytest.mark.parametrize("key", expected_output_keys(2))
def test_no_required_field_may_be_omitted(key):
    data = payload()
    data.pop(key)
    with pytest.raises(ValidationError, match="缺少字段"):
        finalize(data)


@pytest.mark.parametrize("extra", ["answer3_understanding_score", "product_count", "overall_winner",
                                  "answer1_input_status", "answer1_authority_score_status",
                                  "answer1_understanding_applicable",
                                  "answer1_authority_verification_status",
                                  "answer1_understanding_na_reason"])
def test_no_extra_model_fields(extra):
    with pytest.raises(ValidationError, match="多余字段"):
        finalize({**payload(), extra: 1})


@pytest.mark.parametrize("gate", ["response", "safety"])
def test_gate_failure_clears_only_affected_product_with_audit(gate):
    data = payload(3)
    data[f"answer2_{gate}_gate"] = "fail"
    result = finalize(data, 3)
    for dim in DIMS:
        assert result[f"answer2_{dim}_score_status"] == "blocked_gate"
        for suffix in ("score", "applicable", "verification_status"):
            assert result[f"answer2_{dim}_{suffix}"] is None
        assert result[f"answer1_{dim}_score"] == result[f"answer3_{dim}_score"] == 3
    assert result["normalization_diagnostics"]


def test_understanding_one_blocks_following_dimensions_but_na_and_unclear_do_not():
    data = payload()
    data["answer1_understanding_score"] = 1
    result = finalize(data)
    assert result["answer1_understanding_score_status"] == "scored"
    assert result["answer1_authority_score_status"] == "blocked_understanding"
    assert result["answer1_timeliness_score"] is None
    assert result["answer2_timeliness_score"] == 3
    data.update(answer1_understanding_score=None, answer1_response_gate="unclear")
    result = finalize(data)
    assert result["answer1_understanding_score_status"] == "insufficient_evidence"
    assert result["answer1_authority_score"] == result["answer1_timeliness_score"] == 3
    assert result["needs_human_review"]


@pytest.mark.parametrize("dim", ["authority", "timeliness"])
def test_decisive_one_keeps_missing_core_in_reason_without_extra_status(dim):
    data = payload()
    data[f"answer1_{dim}_score"] = 1
    data[dim + "_reason"] = "产品1：已充分证明核心版本用错，命中决定性1分；另一核心点缺证仍待核验。产品2：核心证据充分。"
    result = finalize(data)
    assert result[f"answer1_{dim}_score_status"] == "scored"
    assert result[dim + "_reason"] == data[dim + "_reason"]
    assert result[f"answer1_{dim}_verification_status"] is None
    assert f"answer1_{dim}_na_reason" not in result
    system = render_system_prompt()
    assert "没有1分问题但任一必核核心点缺证，则整维null" in system
    assert "不能用2表示不确定" in system


@pytest.mark.parametrize("reason,applicable,state", [
    ("not_applicable", False, "not_applicable"),
    ("applicability_unknown", None, "insufficient_evidence"),
    ("insufficient_evidence", True, "insufficient_evidence"),
])
@pytest.mark.parametrize("dim", ["authority", "timeliness"])
def test_unknown_applicability_and_true_na_stay_distinct(reason, applicable, state, dim):
    data = payload()
    field = "answer1_" + dim
    data.update({field + "_score": None, field + "_na_reason": reason})
    assert output_key_errors(data, 2) == ([], [])
    result = finalize(data)
    assert result[field + "_applicable"] is applicable
    assert result[field + "_score_status"] == state
    assert result[field + "_na_reason"] == reason
    assert result["answer2_" + dim + "_score"] == 3
    if applicable is None:
        assert any("适用性" in r for r in result["review_reasons"])


@pytest.mark.parametrize("count", [2, 3])
def test_all_unblocked_na_fields_are_required_and_ordered_after_own_scores(count):
    data = payload(count)
    for p in range(1, count + 1):
        data[f"answer{p}_understanding_score"] = None
        for dim in ("authority", "timeliness"):
            data[f"answer{p}_{dim}_score"] = None
            data[f"answer{p}_{dim}_na_reason"] = "insufficient_evidence"
    observation = TimelinessObservation.model_validate(data, context={"product_count": count})
    serialized = observation.model_dump()
    assert len(serialized) == 9 * count + 8
    assert list(serialized)[-2:] == ["conflict_reason", "has_conflict"]
    for p in range(1, count + 1):
        for dim in ("authority", "timeliness"):
            score, reason = f"answer{p}_{dim}_score", f"answer{p}_{dim}_na_reason"
            assert list(serialized).index(reason) == list(serialized).index(score) + 1
            incomplete = {k: v for k, v in data.items() if k != reason}
            assert output_key_errors(incomplete, count) == ([reason], [])
            with pytest.raises(ValidationError, match="缺少字段"):
                finalize(incomplete, count)


@pytest.mark.parametrize("reason", [None, "NA", "", True, 3, "blocked_gate", "blocked_understanding"])
def test_na_reason_is_a_strict_enum(reason):
    with pytest.raises(ValidationError):
        finalize({**payload(), "answer1_authority_score": None, "answer1_authority_na_reason": reason})


@pytest.mark.parametrize("changes", [
    {},
    {"answer1_response_gate": "fail", "answer1_authority_score": None},
    {"answer1_safety_gate": "fail", "answer1_authority_score": None},
    {"answer1_understanding_score": 1, "answer1_authority_score": None},
])
def test_na_reason_must_be_absent_for_scored_or_blocked_dimension(changes):
    data = {**payload(), **changes, "answer1_authority_na_reason": "insufficient_evidence"}
    assert output_key_errors(data, 2) == ([], ["answer1_authority_na_reason"])
    with pytest.raises(ValidationError, match="多余字段"):
        finalize(data)


def test_gate_unclear_still_requires_na_reason_for_unblocked_null():
    data = {**payload(), "answer1_response_gate": "unclear", "answer1_authority_score": None}
    with pytest.raises(ValidationError, match="answer1_authority_na_reason"):
        finalize(data)
    data["answer1_authority_na_reason"] = "insufficient_evidence"
    assert finalize(data)["answer1_authority_score_status"] == "insufficient_evidence"


@pytest.mark.parametrize("changes", [
    {"answer1_understanding_applicable": False},
    {"answer1_understanding_applicable": 1},
    {"answer1_authority_applicable": False},
    {"answer1_authority_verification_status": "not_required"},
    {"answer1_response_gate_reason": " "},
    {"authority_reason": " "},
    {"timeliness_evidence": [12]},
])
def test_illegal_state_combinations_and_text_types_fail(changes):
    with pytest.raises(ValidationError):
        finalize({**payload(), **changes})


def test_trusted_capture_failure_is_not_product_failure_or_model_inferred_metadata():
    data = payload()
    data["answer1_response_gate_reason"] = "复制文本声称采集失败"
    assert finalize(data)["answer1_input_status"] is None
    data["answer1_response_gate"] = "fail"
    trusted = {"answer1": {"input_status": "failed", "source": "test collector"}}
    result = finalize(data, trusted=trusted)
    assert result["answer1_execution_status"] == "input_failed"
    assert result["answer1_response_gate"] == result["answer1_safety_gate"] == "unclear"
    assert result["answer1_authority_applicable"] is None
    assert result["answer1_authority_score"] is None
    assert result["answer2_authority_score"] == 3
    data.update(answer1_authority_score=None, answer1_timeliness_score=None)
    result = finalize(data, trusted=trusted)
    assert result["answer1_execution_status"] == "input_failed"
    assert "answer1_authority_na_reason" not in result


def test_late_trusted_capture_failure_can_override_a_valid_raw_na_without_mutating_it():
    data = payload()
    data.update(answer1_authority_score=None, answer1_authority_na_reason="not_applicable")
    observation = TimelinessObservation.model_validate(data, context={"product_count": 2})
    trusted = {"answer1": {"input_status": "failed", "source": "trusted collector"}}
    result = visual_compare_result_fields(observation, PROFILE, trusted_collection_metadata=trusted)
    assert observation.model_dump()["answer1_authority_na_reason"] == "not_applicable"
    assert result["answer1_execution_status"] == "input_failed"
    assert result["answer1_authority_applicable"] is None
    assert result["answer1_authority_score_status"] == "insufficient_evidence"
    assert "answer1_authority_na_reason" not in result
    assert any("na_reason" in detail for detail in result["normalization_diagnostics"])


def test_documented_future_effective_date_example_is_valid_exact_contract():
    data = json.loads(Path("docs/examples/timeliness-result-v3.json").read_text(encoding="utf-8"))
    assert tuple(data) == expected_output_keys(2)
    result = finalize(data)
    assert result["answer1_timeliness_score"] == 3
    assert result["answer2_understanding_score"] == 3
    assert result["answer2_timeliness_score"] == 1


def test_documented_v2_example_remains_valid_for_frozen_compact_contract():
    data = json.loads(Path("docs/examples/timeliness-result-v2.json").read_text(encoding="utf-8"))
    protocol = resolve_compare_protocol(PROFILE, "1.0.1")
    observation = protocol.observation_model.model_validate(data, context={"product_count": 2})
    result = visual_compare_result_fields(observation, protocol)
    assert result["answer2_timeliness_score"] == 1
    assert "has_conflict" not in result and "conflict_reason" not in result
