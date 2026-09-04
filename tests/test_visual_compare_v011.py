from auto_eval.judges.visual_compare_judge import visual_compare_result_fields
from auto_eval.schema import VisualCompareObservation
from auto_eval.web.history import _visual_compare_export_rows


def _observation(**overrides) -> VisualCompareObservation:
    data = {
        "answer1_response_gate": "pass",
        "answer2_response_gate": "pass",
        "answer1_safety_gate": "pass",
        "answer2_safety_gate": "pass",
        "understanding_applicable": True,
        "answer1_understanding_score": 2,
        "answer2_understanding_score": 1,
        "understanding_reason": "产品1理解更完整",
        "readability_applicable": True,
        "answer1_readability_score": 2,
        "answer2_readability_score": 2,
        "accuracy_applicable": True,
        "answer1_accuracy_score": 2,
        "answer2_accuracy_score": 1,
        "decision_support_applicable": False,
        "answer1_decision_support_score": None,
        "answer2_decision_support_score": None,
        "closure_applicable": True,
        "answer1_closure_score": 1,
        "answer2_closure_score": 2,
        "closure_reason": "产品2下一步更直接",
        "content_conflict": "no",
        "evidence": ["accuracy | 产品1 | 文本 | 核心数据完整"],
        "confidence": "high",
    }
    data.update(overrides)
    if "content_conflict" in data:
        data["has_conflict"] = data.pop("content_conflict")
    return VisualCompareObservation.model_validate(data)


def test_new_scores_generate_total_overall_and_legacy_fields():
    result = visual_compare_result_fields(_observation())

    assert result["standard_version"] == "0.1.1"
    assert result["answer1_total_score"] == 100.0
    assert result["answer2_total_score"] == 62.5
    assert result["overall_winner"] == "answer1"
    assert result["understanding_winner"] == "answer1"
    assert result["readability_winner"] == "tie"
    assert result["decision_support_winner"] is None
    assert result["relevance"] == "answer1"
    assert result["content_quality"] == "answer1"
    assert result["need_closure"] == "answer2"
    assert result["personalization"] is None
    assert result["needs_human_review"] is False


def test_failed_safety_gate_overrides_higher_quality_score():
    result = visual_compare_result_fields(_observation(
        answer1_safety_gate="fail",
        answer2_safety_gate="pass",
        answer1_understanding_score=2,
        answer2_understanding_score=0,
        answer1_readability_score=2,
        answer2_readability_score=0,
        answer1_accuracy_score=2,
        answer2_accuracy_score=0,
    ))

    assert result["answer1_total_score"] == 100.0
    assert result["answer2_total_score"] == 0.0
    assert result["overall_winner"] == "answer2"
    assert result["safety"] == "answer2"


def test_unclear_gate_forces_review_and_no_overall_winner():
    result = visual_compare_result_fields(_observation(
        answer1_response_gate="unclear",
    ))

    assert result["overall_winner"] is None
    assert result["needs_human_review"] is True
    assert "response_gate 无法判断" in result["review_reasons"]
    assert result["needs_review"] is True


def test_compare_export_keeps_new_fields_and_display_values():
    result = visual_compare_result_fields(_observation())
    result.update({
        "item_id": "q1",
        "query": "示例问题",
        "answer1": "回答1",
        "answer2": "回答2",
    })

    row = _visual_compare_export_rows([result])[0]

    assert row["标准版本"] == "0.1.1"
    assert row["产品1响应体验Gate"] == "通过"
    assert row["理解需求胜方"] == "产品1更优"
    assert row["整体胜负"] == "产品1更优"
    assert row["产品1核心质量总分"] == 100.0
    assert row["证据"] == ["accuracy | 产品1 | 文本 | 核心数据完整"]
