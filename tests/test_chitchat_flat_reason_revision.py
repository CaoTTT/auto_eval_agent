"""Freeze historical chat prompts while making evidence-linked reasons selectable."""
from copy import deepcopy
import hashlib
import re

import pytest

from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID as PROFILE,
    CHITCHAT_FLAT_DIMENSIONS,
    CHITCHAT_FLAT_RESULT_SCHEMA_VERSION,
)
from auto_eval.judges.chitchat_flat_rubric_refs import RUBRIC_REFERENCES, render_rubric_references
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges import visual_compare_prompt_chitchat_flat as current
from auto_eval.judges import visual_compare_prompt_chitchat_flat_r110 as frozen
from auto_eval.web.human_baselines import HumanError, ImportMapping
from auto_eval.web.human_baseline_import import build_human_template, inspect_workbook, parse_human_labels
from auto_eval.web.human_compare import align_baseline_to_run
from auto_eval.web.protocol_identity import require_snapshot_protocol
from auto_eval.web.tasks import _task_from_snapshot
from test_chitchat_flat_human import flat_example
from test_chitchat_flat_statistics import dataset


def test_new_selection_updates_only_bundle_and_keeps_old_prompt_frozen():
    old = resolve_compare_protocol(PROFILE, "1.1.0")
    new = resolve_compare_protocol(PROFILE)
    assert new.bundle_revision == "1.1.1"
    assert old.bundle_revision == frozen.BUNDLE_REVISION == "1.1.0"
    assert new.id == old.id == PROFILE
    assert new.result_schema_version == old.result_schema_version == CHITCHAT_FLAT_RESULT_SCHEMA_VERSION
    assert new.observation_model is old.observation_model
    assert new.dimensions == old.dimensions
    assert old.system_template is frozen.VISUAL_COMPARE_SYSTEM
    assert new.system_template is current.VISUAL_COMPARE_SYSTEM
    assert hashlib.sha256(frozen.SYSTEM_TEMPLATE_SOURCE.encode()).hexdigest() == "5f3779b43571439fa30d77514d0250d95740d88d0e138e6d74f60597f1b2b6c1"
    assert hashlib.sha256(frozen.USER_TEMPLATE_SOURCE.encode()).hexdigest() == "f1c872491b6437fd69923d641588b6018074e5f14c65a82ab0f0a6706149042a"
    assert new.request_metadata() != old.request_metadata()
    with pytest.raises(ValueError):
        resolve_compare_protocol(PROFILE, "1.1.99")


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_all_four_input_variants_are_identical_across_revisions(count, mode):
    params = dict(product_count=count, evidence_mode=mode, evaluation_datetime="2026-10-30", question="陪我聊聊")
    for n in (1, 2, 3):
        for field in ("context", "answer", "split_manifest"):
            params[f"{field}{n}"] = f"{field}-product-{n}"
        params[f"image_count{n}"] = n
        params[f"frame_count{n}"] = n + 1
    assert current.VISUAL_COMPARE_USER.render(**params) == frozen.VISUAL_COMPARE_USER.render(**params)


@pytest.mark.parametrize("revision", ["1.1.0", "1.1.1"])
def test_task_restore_and_human_template_keep_recorded_revision(tmp_path, revision):
    snapshot = dataset()
    protocol = resolve_compare_protocol(PROFILE, revision)
    snapshot["protocol_manifest"] = protocol.public_metadata()
    for row in snapshot["results"]:
        row["bundle_revision"] = revision
    before = deepcopy(snapshot)
    task = _task_from_snapshot(snapshot, "chat-frozen-reason")
    assert task.protocol_manifest["bundle_revision"] == revision
    assert require_snapshot_protocol(snapshot).system_template is protocol.system_template
    assert snapshot == before
    path = tmp_path / "frozen-template.xlsx"
    path.write_bytes(build_human_template(snapshot, PROFILE))
    mapping = inspect_workbook(path)["suggested_mapping"]
    assert mapping["human_bundle_revision"] == revision
    mapping["policy_note"] = "保持原任务冻结的证据理由规则"
    parsed = parse_human_labels(path, ImportMapping(**mapping))
    assert not parsed["issues"]
    assert parsed["mapping"]["human_bundle_revision"] == revision


@pytest.mark.parametrize("mode", ["same_standard", "regression"])
def test_old_human_baseline_cannot_silently_compare_new_reason_revision(mode):
    _, snapshot, baseline, mapping = flat_example()
    baseline["human_bundle_revision"] = "1.1.0"
    with pytest.raises(HumanError, match="冻结修订号"):
        align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["understanding"], mode)


def test_rubric_ids_belong_to_their_dimension_and_original_workbook_row():
    groups = (
        ("响应", "response_experience", 3, 2),
        ("安全", "safety_stability", 3, 3),
        ("理解", "understanding", 4, 4),
        ("情感", "emotional_expression", 4, 6),
        ("风格", "style", 5, 7),
        ("场景", "scenario_fulfillment", 4, 8),
        ("引导", "guided_recommendation", 4, 9),
    )
    assert set(CHITCHAT_FLAT_DIMENSIONS) == {dimension for _, dimension, _, _ in groups}
    assert set(RUBRIC_REFERENCES) == {
        f"{prefix}-{n}" for prefix, _, count, _ in groups for n in range(1, count + 1)
    }
    for prefix, dimension, count, row in groups:
        for n in range(1, count + 1):
            item = RUBRIC_REFERENCES[f"{prefix}-{n}"]
            assert item["dimension"] == dimension
            assert item["name"].strip()
            assert item["source_item"] == n
            expected_cells = [f"C{row}"] if row <= 3 else [f"{column}{row}" for column in "CEG"]
            if prefix == "响应" and n == 3:
                expected_cells.append("H2")
            assert item["source_cells"] == expected_cells
            expected_anchors = {"门槛原文"} if row <= 3 else {"1分原文", "3分原文", "5分原文"}
            if prefix == "响应" and n == 3:
                expected_anchors.add("原表示例")
            assert set(item["anchors"]) == expected_anchors
            assert all(isinstance(text, str) and text.strip() for text in item["anchors"].values())


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_current_system_renders_all_rubric_anchors_and_only_active_product_fields(count, mode):
    rendered = current.VISUAL_COMPARE_SYSTEM.render(product_count=count, evidence_mode=mode)
    references = render_rubric_references()
    assert current.BUNDLE_REVISION == "1.1.1"
    assert current.RUBRIC_REFERENCE_TEXT == references
    assert rendered.count(references) == 1
    assert "{{" not in rendered and "{%" not in rendered
    for key, item in RUBRIC_REFERENCES.items():
        assert references.count(f"[{key} {item['name']}]") == 1
        assert all(text in references for text in item["anchors"].values())
    # In particular, examples and reason instructions must not introduce unknown IDs.
    cited_ids = set(re.findall(r"\[((?:响应|安全|理解|情感|风格|场景|引导)-\d+)\s", rendered))
    assert cited_ids == set(RUBRIC_REFERENCES)
    fields = re.findall(r"answer(\d+)_DIM_score", rendered)
    assert fields == [str(n) for n in range(1, count + 1)]
    assert rendered.index("DIM_reason：") < rendered.index("answer1_DIM_score")


def test_reason_contract_requires_source_evidence_comparison_and_degree_for_each_product():
    source = current.SYSTEM_TEMPLATE_SOURCE
    assert re.search(r"每个活动产品.*?依据\s*→\s*证据\s*→\s*对照\s*→\s*判定", source)
    assert "本维的有效ID和名称" in source
    assert "有分差时分别说明各自差异" in source
    assert "只输出可检查的判断摘要" in source
    assert "不输出完整内部思维过程" in source
    assert "数值评分只写在本维后续的score字段" in source
    assert "全部写入现有reason字符串" in source


@pytest.mark.parametrize("required_parts", [
    pytest.param(("本维适用的标准要求", "可定位的偏差证据", "实际影响或合理可推断的风险",
                  "少一项就撤销该项拟扣分"), id="deduction-needs-standard-evidence-impact"),
    pytest.param(("允许同分、双方满分", "另一方的额外亮点不能变成它的缺陷",
                  "常见表达不等于机械套话"), id="no-forced-gap-or-template-prejudice"),
    pytest.param(("轻微就一律放过", "完整条件在本轮成立", "不要求已有用户投诉或损害已经发生"),
                 id="real-minor-defects-still-count"),
    pytest.param(("不能把“无法证明缺陷”自动改为满分", "缺证不是表现差"),
                 id="unknown-is-neither-perfect-nor-bad"),
    pytest.param(("当前需求或标准在该情境下的要求", "相关图文已查全且确实缺少",
                  "语义等价表达", "采集省略"), id="absence-needs-full-relevant-evidence"),
    pytest.param(("主语、对象、否定、条件、程度和顺序", "澄清问句", "条件词或问号也不自动免责",
                  "不要加引号冒充逐字引文"), id="retain-meaning-without-blanket-immunity"),
    pytest.param(("每个维度都要独立证明", "未跨维机械重复处罚"), id="no-cascading-preference-penalties"),
])
def test_reason_guards_cover_unjustified_deductions_without_erasing_valid_defects(required_parts):
    # These assertions lock critical guardrails, not one complete prose wording or a model score.
    assert all(part in current.SYSTEM_TEMPLATE_SOURCE for part in required_parts)
