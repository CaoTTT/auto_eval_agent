from io import BytesIO
import zipfile

from auto_eval.judges.visual_compare_judge import visual_compare_result_fields
from auto_eval.judges.compare_protocols import VisualCompareObservationV02
from auto_eval.web.history import _visual_compare_export_rows, build_xlsx


def _observation(**overrides) -> VisualCompareObservationV02:
    data = {
        "product_count": 3,
        "answer1_input_status": "complete",
        "answer2_input_status": "complete",
        "answer3_input_status": "complete",
        "answer1_response_gate": "pass",
        "answer2_response_gate": "pass",
        "answer3_response_gate": "pass",
        "answer1_safety_gate": "pass",
        "answer2_safety_gate": "pass",
        "answer3_safety_gate": "pass",
        "understanding_applicable": True,
        "understanding_verification_status": "not_required",
        "understanding_evidence": ["understanding | 产品1 | 文本 | 条件完整"],
        "understanding_reason": "三者均可评分",
        "answer1_understanding_score": 5,
        "answer2_understanding_score": 3,
        "answer3_understanding_score": 5,
        "accuracy_applicable": True,
        "accuracy_verification_status": "unverifiable",
        "accuracy_reason": "缺少外部事实依据",
        "answer1_accuracy_score": None,
        "answer2_accuracy_score": None,
        "answer3_accuracy_score": None,
        "service_closure_applicable": False,
        "scenario_fulfillment_applicable": False,
        "intuitive_efficiency_applicable": True,
        "intuitive_efficiency_verification_status": "not_required",
        "answer1_intuitive_efficiency_score": 4,
        "answer2_intuitive_efficiency_score": 4,
        "answer3_intuitive_efficiency_score": 3,
        "evidence_quality_applicable": False,
        "guided_recommendation_applicable": False,
        "content_conflict": "no",
        "confidence": "high",
    }
    data.update(overrides)
    if "content_conflict" in data:
        data["has_conflict"] = data.pop("content_conflict")
    return VisualCompareObservationV02.model_validate(data)


def test_three_product_scores_generate_dimension_rank_groups():
    result = visual_compare_result_fields(_observation())

    assert result["standard_version"] == "0.2-simplified"
    assert result["understanding_rank_groups"] == [
        ["product1", "product3"],
        ["product2"],
    ]
    assert result["intuitive_efficiency_rank_groups"] == [
        ["product1", "product2"],
        ["product3"],
    ]
    assert result["accuracy_rank_groups"] == []
    assert result["overall_ranking"] is None
    assert result["answer1_total_score"] is None
    assert result["needs_human_review"] is False


def test_failed_product_is_excluded_and_forces_review():
    result = visual_compare_result_fields(_observation(
        answer3_input_status="failed",
        answer3_understanding_score=5,
        answer3_intuitive_efficiency_score=5,
    ))

    assert result["answer3_understanding_score"] is None
    assert result["answer3_intuitive_efficiency_score"] is None
    assert result["understanding_rank_groups"] == [["product1"], ["product2"]]
    assert result["needs_human_review"] is True
    assert "产品3输入失败" in result["review_reason"]


def test_response_gate_not_pass_clears_product_scores():
    result = visual_compare_result_fields(_observation(
        answer2_response_gate="unclear",
    ))

    assert result["answer2_understanding_score"] is None
    assert result["answer2_intuitive_efficiency_score"] is None
    assert result["needs_human_review"] is True


def test_two_product_call_keeps_legacy_projection():
    result = visual_compare_result_fields(_observation(
        product_count=2,
        answer3_input_status=None,
        answer3_response_gate=None,
        answer3_safety_gate=None,
    ))

    assert result["answer3_understanding_score"] is None
    assert result["understanding_rank_groups"] == [["product1"], ["product2"]]
    assert result["relevance"] == "answer1"
    assert result["content_quality"] is None


def test_export_contains_third_product_and_v02_fields():
    result = visual_compare_result_fields(_observation())
    result.update({
        "item_id": "q1",
        "query": "示例问题",
        "answer1": "回答1",
        "answer2": "回答2",
        "answer3": "回答3",
    })
    row = _visual_compare_export_rows([result])[0]

    assert row["标准版本"] == "0.2-simplified"
    assert row["产品数量"] == 3
    assert row["产品3回答"] == "回答3"
    assert row["产品3理解需求分"] == 5
    assert row["理解需求排名组"] == [["product1", "product3"], ["product2"]]


def _xlsx_worksheet_xml(snapshot: dict) -> str:
    with zipfile.ZipFile(BytesIO(build_xlsx(snapshot))) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet")
        )


def test_two_product_xlsx_omits_all_product3_columns():
    result = visual_compare_result_fields(_observation(
        product_count=2,
        answer3_input_status=None,
        answer3_response_gate=None,
        answer3_safety_gate=None,
    ))
    result.update({
        "index": 0,
        "item_id": "q1",
        "query": "示例问题",
        "answer1": "回答1",
        "answer2": "回答2",
    })
    snapshot = {
        "mode": "compare",
        "items": [{
            "id": "q1",
            "query": "示例问题",
            "product_count": 2,
            "source_data": {
                "answer1": "回答1",
                "answer2": "回答2",
                "answer3": "",
                "context3": "",
                "video3": "",
            },
        }],
        "results": [result],
    }

    worksheet_xml = _xlsx_worksheet_xml(snapshot)

    assert "产品3" not in worksheet_xml
    assert "answer3" not in worksheet_xml
    assert "context3" not in worksheet_xml
    assert "video3" not in worksheet_xml


def test_three_product_xlsx_keeps_product3_columns():
    result = visual_compare_result_fields(_observation())
    result.update({
        "index": 0,
        "item_id": "q1",
        "query": "示例问题",
        "answer1": "回答1",
        "answer2": "回答2",
        "answer3": "回答3",
    })
    snapshot = {
        "mode": "compare",
        "items": [{
            "id": "q1",
            "query": "示例问题",
            "product_count": 3,
        }],
        "results": [result],
    }

    worksheet_xml = _xlsx_worksheet_xml(snapshot)

    assert "产品3回答" in worksheet_xml
    assert "产品3理解需求分" in worksheet_xml
