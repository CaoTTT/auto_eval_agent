"""Contract tests for the independently scored seven-dimension chat protocol."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from jinja2 import Environment, meta
from pydantic import ValidationError

from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, CHITCHAT_FLAT_DIMENSIONS,
    CHITCHAT_FLAT_RESULT_SCHEMA_VERSION, CHITCHAT_FLAT_STANDARD_ID,
    ChitchatFlatObservation, expected_output_keys,
    finalize_chitchat_flat_observation, parse_output, validate_output,
)
from auto_eval.judges.visual_compare_prompt_chitchat_flat import (
    SYSTEM_TEMPLATE_SOURCE, USER_TEMPLATE_SOURCE,
    VISUAL_COMPARE_SYSTEM, VISUAL_COMPARE_USER, render_system_prompt,
)
from auto_eval.judges.visual_compare_prompt_qa_chat_v2 import VISUAL_COMPARE_USER as ORIGINAL_USER


def sample_output(count=2):
    result = {}
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        result[dimension + "_reason"] = "；".join(
            f"产品{product}：已回应当前问题，符合本维要求"
            for product in range(1, count + 1)
        )
        for product in range(1, count + 1):
            result[f"answer{product}_{dimension}_score"] = 5
    return result


def protocol():
    return SimpleNamespace(
        id=CHITCHAT_FLAT_COMPARE_PROTOCOL_ID,
        standard_id=CHITCHAT_FLAT_STANDARD_ID, standard_version="1.1",
        bundle_revision="1.1.0", result_schema_version=CHITCHAT_FLAT_RESULT_SCHEMA_VERSION,
    )


@pytest.mark.parametrize("count,field_count", [(2, 21), (3, 28)])
def test_exact_active_contract_and_canonical_reason_first_order(count, field_count):
    source = sample_output(count)
    reversed_source = dict(reversed(list(source.items())))
    observation = validate_output(reversed_source, count)
    assert observation.product_count == count
    assert len(observation.model_dump()) == field_count
    assert tuple(observation.model_dump()) == expected_output_keys(count)
    assert json.loads(observation.model_dump_json()) == source
    assert tuple(json.loads(observation.model_dump_json())) == expected_output_keys(count)
    if count == 2:
        assert not any("answer3" in key for key in observation.model_dump())


@pytest.mark.parametrize("count", [None, True, 2.0, "2", 1, 4])
def test_product_count_is_strict(count):
    with pytest.raises((ValueError, ValidationError)):
        validate_output(sample_output(), count)


@pytest.mark.parametrize("key,value", [
    ("answer1_understanding_score", True),
    ("answer1_understanding_score", "3"),
    ("answer1_understanding_score", 3.0),
    ("answer1_understanding_score", 0),
    ("answer1_understanding_score", 6),
    ("answer1_response_experience_score", 2),
    ("answer2_safety_stability_score", 4),
    ("answer2_safety_stability_score", 1.0),
    ("understanding_reason", " \n "),
    ("understanding_reason", 1),
    ("understanding_reason", ["产品1：相关"]),
])
def test_invalid_scores_and_reasons_are_not_coerced(key, value):
    source = sample_output()
    source[key] = value
    with pytest.raises(ValidationError):
        validate_output(source, 2)


@pytest.mark.parametrize("extra", [
    "total_score", "personalization_reason", "answer1_response_gate",
    "has_conflict", "understanding_evidence", "answer1_understanding_na_reason",
    "answer3_understanding_score", "trusted_collection_metadata",
])
def test_extra_legacy_or_program_owned_fields_are_rejected(extra):
    source = sample_output()
    source[extra] = None
    with pytest.raises(ValidationError):
        validate_output(source, 2)


def test_missing_active_product_and_missing_dimension_field_are_rejected():
    source = sample_output(3)
    del source["answer3_guided_recommendation_score"]
    with pytest.raises(ValidationError):
        validate_output(source, 3)
    source = sample_output()
    del source["safety_stability_reason"]
    with pytest.raises(ValidationError):
        validate_output(source, 2)


@pytest.mark.parametrize("dimension", CHITCHAT_FLAT_DIMENSIONS)
def test_null_is_preserved_as_unverifiable_without_cascading(dimension):
    source = sample_output()
    source[f"answer1_{dimension}_score"] = None
    source[dimension + "_reason"] = "产品1：目标内容无法读取，缺少决定性证据；产品2：可见回应满足本维要求"
    result = finalize_chitchat_flat_observation(validate_output(source, 2), protocol())
    assert result[f"answer1_{dimension}_score"] is None
    assert result[f"answer1_{dimension}_score_status"] == "unverifiable"
    assert result[f"answer1_{dimension}_applicable"] is True
    assert result[f"answer2_{dimension}_score"] == 5
    assert result[f"answer2_{dimension}_score_status"] == "scored"
    assert not any(key.endswith("_na_reason") for key in result)
    for other in set(CHITCHAT_FLAT_DIMENSIONS) - {dimension}:
        assert result[f"answer1_{other}_score"] == 5


@pytest.mark.parametrize("dimension", ["response_experience", "safety_stability", "understanding"])
def test_low_scores_never_invoke_legacy_gate_or_understanding_block(dimension):
    source = sample_output()
    source[f"answer1_{dimension}_score"] = 1
    result = finalize_chitchat_flat_observation(validate_output(source, 2), protocol())
    assert {key: result[key] for key in source} == source
    assert all(result[f"answer1_{dim}_score_status"] == "scored" for dim in CHITCHAT_FLAT_DIMENSIONS)
    assert not any("gate" in key or "winner" in key or "rank_groups" in key for key in result)
    assert "has_conflict" not in result


def test_trusted_collection_failure_clears_only_failed_product_and_retains_raw_observation():
    source = sample_output(3)
    source["answer1_understanding_score"] = 3
    observation = ChitchatFlatObservation.model_validate(source, context={
        "product_count": 3,
        "trusted_collection_metadata": {"answer1": {"input_status": "failed", "source": "test_capture"}},
    })
    snapshot = copy.deepcopy(observation.model_dump())
    result = finalize_chitchat_flat_observation(observation, protocol())
    assert observation.model_dump() == snapshot == source
    assert result["answer1_execution_status"] == "input_failed"
    for dimension in CHITCHAT_FLAT_DIMENSIONS:
        assert result[f"answer1_{dimension}_score"] is None
        assert result[f"answer1_{dimension}_score_status"] == "unverifiable"
        assert result[dimension + "_reason"].endswith(source[dimension + "_reason"])
        assert result[dimension + "_reason"].startswith("程序记录：产品1可信采集失败")
        for product in (2, 3):
            assert result[f"answer{product}_{dimension}_score"] == 5
    assert result["needs_human_review"]
    assert result["normalization_diagnostics"]


def test_partial_collection_flags_review_without_removing_scores():
    observation = validate_output(sample_output(), 2)
    result = finalize_chitchat_flat_observation(observation, protocol(), trusted_collection_metadata={
        "answer2": {"input_status": "partial", "source": "test_capture"},
    })
    assert all(result[f"answer2_{dimension}_score"] == 5 for dimension in CHITCHAT_FLAT_DIMENSIONS)
    assert result["needs_human_review"]
    assert result["answer2_understanding_verification_status"] is None
    assert result["answer2_understanding_verification_status_source"] == "not_collected"


def test_shared_reason_labels_are_allowed_and_missing_labels_only_flag_review():
    source = sample_output()
    source["response_experience_reason"] = "产品1、产品2：可见文字均完整返回"
    result = finalize_chitchat_flat_observation(validate_output(source, 2), protocol())
    assert not result["normalization_diagnostics"]
    source["understanding_reason"] = "产品10：相关"
    result = finalize_chitchat_flat_observation(validate_output(source, 2), protocol())
    assert any("understanding_reason未明确标注产品1" in item for item in result["normalization_diagnostics"])
    assert result["answer1_understanding_score"] == 5


@pytest.mark.parametrize("raw", [
    "", "{} trailing", "```json\n{}\n```", "[]",
    '{"x":1,"x":2}', '{"x":{"a":1,"a":2}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
])
def test_raw_parser_rejects_non_json_duplicates_and_nonfinite_numbers(raw):
    with pytest.raises((ValueError, ValidationError)):
        parse_output(raw, 2)


def test_raw_parser_accepts_complete_output_without_rewriting_reasons():
    source = sample_output()
    assert parse_output(json.dumps(source, ensure_ascii=False), 2).model_dump() == source


def test_approved_prompt_text_and_input_contract_are_preserved():
    # Freeze the approved text with LF newlines, independent of the author's
    # workspace and the CRLF packaging used for the original Windows files.
    assert hashlib.sha256(SYSTEM_TEMPLATE_SOURCE.encode()).hexdigest() == "5f3779b43571439fa30d77514d0250d95740d88d0e138e6d74f60597f1b2b6c1"
    assert hashlib.sha256(USER_TEMPLATE_SOURCE.encode()).hexdigest() == "f1c872491b6437fd69923d641588b6018074e5f14c65a82ab0f0a6706149042a"
    env = Environment()
    assert meta.find_undeclared_variables(env.parse(USER_TEMPLATE_SOURCE)) == {
        "evaluation_datetime", "product_count", "question", "evidence_mode",
        *{f"{key}{product}" for key in ("context", "answer", "split_manifest", "image_count", "frame_count") for product in (1, 2, 3)},
    }


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_prompt_rendering_keeps_all_active_product_evidence(count, mode):
    params = {"product_count": count, "evidence_mode": mode, "question": "陪我聊聊", "evaluation_datetime": "2026-10-30"}
    for product in (1, 2, 3):
        for key in ("context", "answer", "split_manifest"):
            params[f"{key}{product}"] = f"{key}_{product}_unique"
        params[f"image_count{product}"] = product
        params[f"frame_count{product}"] = product + 2
    system = VISUAL_COMPARE_SYSTEM.render(**params)
    user = VISUAL_COMPARE_USER.render(**params)
    original_user = ORIGINAL_USER.render(**params)
    # Only the last output instruction changes in all four input variants.
    assert user.strip().splitlines()[:-1] == original_user.strip().splitlines()[:-1]
    assert "{{" not in system + user
    for product in range(1, count + 1):
        assert f"answer{product}_DIM_score" in system
        assert f"context_{product}_unique" in user
        assert f"answer_{product}_unique" in user
        if mode == "long_screenshot":
            assert f"split_manifest_{product}_unique" in user
        else:
            assert f"录屏关键帧共{product + 2}张" in user
    if count == 2:
        assert "answer3_DIM_score" not in system
        assert "answer_3_unique" not in user


@pytest.mark.parametrize("count,mode", [(1, "long_screenshot"), (True, "long_screenshot"), (2, "unknown")])
def test_prompt_helper_rejects_invalid_render_context(count, mode):
    with pytest.raises(ValueError):
        render_system_prompt(product_count=count, evidence_mode=mode)
