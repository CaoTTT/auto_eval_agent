import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "offline_judge_mvp" / "pipeline.py"
spec = importlib.util.spec_from_file_location("pipeline", MODULE_PATH)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def sample(sid="S001", query="9:40半小时后", answer="10:10:00"):
    return {"_sample_id": sid, "_source_row": 2, "query": query, "垂域": "时区时差", "答案类型": "标准答案", "答案内容": answer}


def draft(sid="S001"):
    return {
        "sample_id": sid, "judge_mode": "确定答案核对", "question_intent": "计算半小时后的时间",
        "applicable_conditions": "按题面时分计算", "suggested_answer": "10:10", "accuracy_rules": "1. 直接作答时，应为10:10。",
        "accepted_variants": "10点10分", "reference_coverage": "核心答案穷尽", "conditional_rules": "", "task_completion": "",
        "temporal_type": "稳定", "temporal_scope": "", "issues": ["核实时效标签"], "evidence_to_verify": "9:40加30分钟，待专家复算。",
    }


def row(source, generated, status="待审核"):
    return {
        "样本ID": source["_sample_id"], "Query": source["query"], "审核状态": status,
        "事实确认": "未确认", "条件确认": "未确认", "审核意见": "", "审核人": "", "审核日期": "",
        "原垂域": source["垂域"], "原答案类型": source["答案类型"], "原答案摘要": source["答案内容"],
        **p.draft_review_fields(generated), "核验依据": "", "证据对应日期": "", "模型待确认问题": "核实时效标签",
    }


def approved_row(source=None, generated=None):
    source = source or sample()
    generated = generated or draft()
    record = row(source, generated, "通过")
    record.update({"事实确认": "已确认", "条件确认": "已确认", "审核人": "专家甲", "审核日期": "2026-10-12",
                   "核验依据": "专家复算确认：9:40加30分钟为10:10。"})
    return record


def simple_xlsx(path, rows, sheet="专家审核", formulas=None, drawing=False):
    """Minimal XML fixture; production intentionally does not write XLSX."""
    body = []
    for row_no, values in rows.items():
        cells = []
        for col, value in values.items():
            number = col + 1
            letters = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(65 + remainder) + letters
            address = f"{letters}{row_no}"
            formula = (formulas or {}).get(address)
            if formula:
                cells.append(f'<c r="{address}"><f>{escape(formula)}</f><v>1</v></c>')
            else:
                cells.append(f'<c r="{address}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
        body.append(f'<row r="{row_no}">' + "".join(cells) + "</row>")
    namespace = p.MAIN
    with ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{namespace}" xmlns:r="{p.REL}"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{p.PKG_REL}"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="{p.REL}/worksheet"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{namespace}" xmlns:r="{p.REL}"><sheetData>' + "".join(body) + '</sheetData>' + ('<drawing r:id="rId2"/>' if drawing else "") + '</worksheet>')


class DateSemanticsTests(unittest.TestCase):
    def test_excel_time_is_displayed_time(self):
        self.assertEqual(p.normalize_number("0.4236111111111111", "h:mm:ss"), "10:10:00")

    def test_date_1900_and_1904_system(self):
        self.assertEqual(p.normalize_number("61", "yyyy-mm-dd"), "1900-03-01")
        self.assertEqual(p.normalize_number("0", "yyyy-mm-dd", True), "1904-01-01")

    def test_invalid_excel_leap_day_rejected(self):
        with self.assertRaises(p.PipelineError):
            p.normalize_number("60", "yyyy-mm-dd")

    def test_elapsed_time_not_wrapped(self):
        self.assertEqual(p.normalize_number("1.5", "[h]:mm:ss"), "36:00:00")

    def test_leading_zero_and_percent(self):
        self.assertEqual(p.normalize_number("21", "000"), "021")
        self.assertEqual(p.normalize_number("0.125", "0.00%"), "12.50%")

    def test_literal_string_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "literal.xlsx"
            simple_xlsx(path, {1: {0: "query", 1: "答案内容"}, 2: {0: "=literal + text", 1: "0010"}}, "数据")
            records = p.import_samples(path)
            self.assertEqual(records[0]["query"], "=literal + text")
            self.assertEqual(records[0]["答案内容"], "0010")


class DraftTests(unittest.TestCase):
    def test_valid_minimum_draft(self):
        self.assertEqual(p.validate_draft(draft(), "S001"), [])

    def test_missing_or_unknown_field_rejected(self):
        bad = draft()
        del bad["accuracy_rules"]
        bad["approval"] = "通过"
        self.assertTrue(p.validate_draft(bad))

    def test_duplicate_missing_and_unknown_samples(self):
        errors = p.validate_drafts([sample(), sample("S002")], [draft(), draft(), draft("S003")])
        self.assertTrue(any("重复" in x for x in errors))
        self.assertTrue(any("缺少草稿" in x for x in errors))
        self.assertTrue(any("不在" in x for x in errors))

    def test_json_fence_allowed_but_extra_output_rejected(self):
        content = json.dumps(draft(), ensure_ascii=False)
        self.assertEqual(p.parse_model_json("```json\n" + content + "\n```"), draft())
        with self.assertRaises(p.PipelineError):
            p.parse_model_json("回答如下\n" + content)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.samples = [sample()]
        self.drafts = [draft()]

    def test_pending_rows_never_enter_card_library(self):
        for status in ["待审核", "待补充", "剔除"]:
            self.assertEqual(p.approval_cards(self.samples, self.drafts, [row(sample(), draft(), status)]), [])

    def test_explicit_confirmation_is_required(self):
        record = row(sample(), draft(), "通过")
        with self.assertRaises(p.PipelineError) as error:
            p.approval_cards(self.samples, self.drafts, [record])
        self.assertIn("事实确认", str(error.exception))

    def test_valid_card_preserves_provenance(self):
        cards = p.approval_cards(self.samples, self.drafts, [approved_row()], "hash")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["provenance"]["raw_sample"], sample())
        self.assertEqual(cards[0]["provenance"]["model_draft"], draft())
        self.assertEqual(cards[0]["review"]["reviewer"], "专家甲")

    def test_expert_adding_basis_can_pass_without_modified_status(self):
        record = approved_row()
        self.assertNotIn("核验依据", p.draft_review_fields(draft()))
        self.assertEqual(row(sample(), draft())["核验依据"], "")
        self.assertEqual(record["审核状态"], "通过")
        card = p.approval_cards(self.samples, self.drafts, [record])[0]
        self.assertEqual(card["provenance"]["expert_changes"], {})
        self.assertEqual(card["provenance"]["expert_verification_basis"], record["核验依据"])
        self.assertEqual(card["provenance"]["model_draft"]["evidence_to_verify"], draft()["evidence_to_verify"])

    def test_approved_dates_must_be_real_iso_dates(self):
        for invalid in ["2026-02-30", "2026/10/12", ""]:
            record = approved_row()
            record["审核日期"] = invalid
            with self.assertRaises(p.PipelineError):
                p.approval_cards(self.samples, self.drafts, [record])

    def test_query_or_raw_labels_tampering_rejected(self):
        for field in ["Query", "原垂域", "原答案类型"]:
            record = approved_row()
            record[field] += "修改"
            with self.assertRaises(p.PipelineError):
                p.approval_cards(self.samples, self.drafts, [record])

    def test_source_answer_snapshot_not_derived_from_summary(self):
        record = approved_row()
        record["原答案摘要"] = "摘要不代表完整事实"
        card = p.approval_cards(self.samples, self.drafts, [record])[0]
        self.assertEqual(card["provenance"]["raw_sample"]["答案内容"], "10:10:00")

    def test_duplicate_unknown_missing_rows_rejected(self):
        for records in [[approved_row(), approved_row()], [], [dict(approved_row(), 样本ID="unknown")]]:
            with self.assertRaises(p.PipelineError):
                p.approval_cards(self.samples, self.drafts, records)

    def test_dynamic_card_needs_evidence_date(self):
        generated = draft()
        generated.update({"temporal_type": "动态", "temporal_scope": "上海，测试当天"})
        record = approved_row(generated=generated)
        with self.assertRaises(p.PipelineError):
            p.approval_cards(self.samples, [generated], [record])
        record["证据对应日期"] = "2026-10-12"
        self.assertEqual(len(p.approval_cards(self.samples, [generated], [record])), 1)

    def test_changed_final_fields_require_modified_status_and_notes(self):
        record = approved_row()
        record["最终答案"] = "10点10分"
        with self.assertRaises(p.PipelineError):
            p.approval_cards(self.samples, self.drafts, [record])
        record["审核状态"] = "修改后通过"
        record["审核意见"] = "采用中文时间表达"
        cards = p.approval_cards(self.samples, self.drafts, [record])
        self.assertEqual(cards[0]["provenance"]["expert_changes"]["最终答案"]["expert"], "10点10分")

    def test_card_order_follows_source_not_reordered_review(self):
        s2, d2 = sample("S002"), draft("S002")
        cards = p.approval_cards([sample(), s2], [draft(), d2], [approved_row(s2, d2), approved_row()])
        self.assertEqual([x["sample_id"] for x in cards], ["S001", "S002"])

    def test_mixed_valid_invalid_approval_never_returns_partial_library(self):
        s2, d2 = sample("S002"), draft("S002")
        bad = approved_row(s2, d2)
        bad["事实确认"] = "未确认"
        with self.assertRaises(p.PipelineError):
            p.approval_cards([sample(), s2], [draft(), d2], [approved_row(), bad])


class WorkbookReadTests(unittest.TestCase):
    def test_review_workbook_uses_exact_columns(self):
        record = approved_row()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            simple_xlsx(path, {4: dict(enumerate(p.REVIEW_COLUMNS)), 5: {i: record.get(k, "") for i, k in enumerate(p.REVIEW_COLUMNS)}})
            rows = p.review_rows_from_xlsx(path)
            self.assertEqual(rows[0]["样本ID"], "S001")
            self.assertEqual(len(p.approval_cards([sample()], [draft()], rows)), 1)

    def test_review_top_summary_formula_ignored_but_data_formula_rejected(self):
        record = approved_row()
        fixture_rows = {2: {9: "status summary"}, 4: dict(enumerate(p.REVIEW_COLUMNS)),
                        5: {i: record.get(k, "") for i, k in enumerate(p.REVIEW_COLUMNS)}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            simple_xlsx(path, fixture_rows, formulas={"J2": 'COUNTIF(C5:C54,"通过")'})
            parsed = p.review_rows_from_xlsx(path)
            self.assertEqual(len(p.approval_cards([sample()], [draft()], parsed)), 1)
            self.assertNotIn(9, p.read_xlsx(path, "专家审核", ignore_formulas_before_row=4)[2])
            with self.assertRaises(p.PipelineError):
                p.read_xlsx(path, "专家审核")
            simple_xlsx(path, fixture_rows, formulas={"J2": 'COUNTIF(C5:C54,"通过")', "O5": '"core rule"'})
            with self.assertRaises(p.PipelineError) as error:
                p.review_rows_from_xlsx(path)
            self.assertIn("O5", str(error.exception))

    def test_review_header_formula_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            simple_xlsx(path, {4: dict(enumerate(p.REVIEW_COLUMNS))}, formulas={"C4": '"审核状态"'})
            with self.assertRaises(p.PipelineError) as error:
                p.review_rows_from_xlsx(path)
            self.assertIn("C4", str(error.exception))

    def test_formula_and_images_error_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "formula.xlsx"
            simple_xlsx(path, {1: {0: "query", 1: "答案内容"}, 2: {0: "测试", 1: "1"}}, formulas={"B2": "1+1"})
            with self.assertRaises(p.PipelineError) as error:
                p.import_samples(path)
            self.assertIn("公式", str(error.exception))
            simple_xlsx(path, {1: {0: "query", 1: "答案内容"}, 2: {0: "测试", 1: ""}}, drawing=True)
            with self.assertRaises(p.PipelineError) as error:
                p.import_samples(path)
            self.assertIn("图片", str(error.exception))

    def test_ids_stable_and_blank_rows_do_not_shift_existing_source_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.xlsx"
            simple_xlsx(path, {1: {0: "query", 1: "答案内容"}, 2: {0: "第一题"}, 3: {}, 4: {0: "第二题"}})
            first = p.import_samples(path)
            self.assertEqual(first, p.import_samples(path))
            self.assertEqual([x["_sample_id"] for x in first], ["S001", "S002"])
            self.assertEqual([x["_source_row"] for x in first], [2, 4])


class GenerationResumeTests(unittest.TestCase):
    def args(self, folder):
        folder = Path(folder)
        input_path = folder / "samples.json"
        p.write_json(input_path, [sample()])
        prompt_path = folder / "prompt.txt"
        prompt_path.write_text("test prompt", encoding="utf-8")
        return SimpleNamespace(samples=str(input_path), output=str(folder / "drafts.jsonl"), prompt=str(prompt_path),
                               api_key_env="TEST_JUDGE_KEY", model="test-model", base_url="https://example.invalid/v1",
                               json_mode=True, timeout=1, attempts=3, errors=None)

    def test_missing_key_does_not_send_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            with patch.dict(p.os.environ, {}, clear=True), patch.object(p, "request_card") as request:
                with self.assertRaises(p.PipelineError):
                    p.generate(args)
                request.assert_not_called()

    def test_matching_config_resumes_but_changed_source_regenerates(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            with patch.dict(p.os.environ, {"TEST_JUDGE_KEY": "not-a-real-secret"}), patch.object(p, "request_card", return_value=draft()) as request:
                p.generate(args)
                p.generate(args)
                self.assertEqual(request.call_count, 1)
                new_sample = sample(query="9点40分加半小时")
                p.write_json(args.samples, [new_sample])
                p.generate(args)
                self.assertEqual(request.call_count, 2)

    def test_retry_bounded_and_error_record_not_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            with patch.dict(p.os.environ, {"TEST_JUDGE_KEY": "not-a-real-secret"}), patch.object(p, "request_card", side_effect=p.PipelineError("schema invalid")) as request, patch.object(p.time, "sleep"):
                with self.assertRaises(p.PipelineError):
                    p.generate(args)
                self.assertEqual(request.call_count, 3)
                self.assertEqual(p.load_records(args.output), [])
                failures = p.load_records(Path(tmp) / "drafts.errors.jsonl")
                self.assertEqual(failures[0]["status"], "generation_failed")


if __name__ == "__main__":
    unittest.main()
