"""Approved UI names must never relabel unknown history or protocol metadata."""
from copy import deepcopy
from pathlib import Path

import pytest

from auto_eval.judges.compare_protocols import list_compare_protocols, resolve_compare_protocol
from auto_eval.web.prompt_names import prompt_name, prompt_name_catalog


APPROVED = [
    ("qa_competitor_compare@0.2-simplified", "0.2.1", "问答对比-V1-P0.2.1-261022-生产"),
    ("qa_competitor_compare@0.2-simplified-plus", "0.2.3", "问答对比-V1-P0.2.3-261030-生产"),
    ("qa_competitor_compare@0.2-simplified-1022", "0.2.6", "问答对比-V1-P0.2.6-261023-实验"),
    ("qa_chat_compare@1.1", "1.1.0", "闲聊对比-V1-P1.1.0-261028-实验"),
    ("qa_chat_compare@1.1-demand-calibrated", "1.1.3", "闲聊对比-V1-P1.1.3-261030-实验"),
    ("chitchat_evidence_first_flat@1.1", "1.1.1", "闲聊对比-V1-P1.1.1-261030-实验"),
    ("timeliness_compare@1.0", "1.0.2", "时效性对比-V1-P1.0.2-261022-生产"),
    ("knowledge_qa_compare@1.0", "1.0.0", "知识问答对比-V1-P1.0.0-261029-实验"),
    ("qa_competitor_compare@0.3", "0.3.1", "问答对比-V0.3-P0.3.1-261022-实验"),
    ("qa_competitor_compare@0.2-simplified-calibrated", "0.2.3", "问答对比-V1-P0.2.3-261022-实验"),
    ("qa_competitor_compare@0.2-simplified-thinking-exposure", "0.2.3", "问答对比-V1-P0.2.3-思考暴露-261022-实验"),
    ("qa_chat_compare@1.0", "1.0.0", "闲聊对比-V1-P1.0.0-261022-实验"),
]


@pytest.mark.parametrize("profile_id,revision,name", APPROVED)
def test_only_exact_approved_identity_uses_canonical_name(profile_id, revision, name):
    assert prompt_name(profile_id, revision) == name
    assert prompt_name(profile_id, revision, fallback="原界面名称") == name
    assert prompt_name(profile_id) == profile_id
    assert prompt_name(profile_id, None, fallback="冻结的历史名称") == "冻结的历史名称"
    assert prompt_name(profile_id, "", fallback="缺少版本") == "缺少版本"
    assert prompt_name(profile_id, "99.0.0", fallback="未知历史版本") == "未知历史版本"


@pytest.mark.parametrize("profile_id,revision", [
    ("qa_competitor_compare@0.2-simplified", "0.2.0"),
    ("qa_competitor_compare@0.2-simplified-plus", "0.2.1"),
    ("qa_competitor_compare@0.2-simplified-plus", "0.2.2"),
    ("qa_competitor_compare@0.2-simplified-1022", "0.2.5"),
    ("qa_chat_compare@1.1-demand-calibrated", "1.1.2"),
    ("chitchat_evidence_first_flat@1.1", "1.1.0"),
    ("timeliness_compare@1.0", "1.0.1"),
])
def test_known_historical_revisions_keep_their_actual_legacy_names(profile_id, revision):
    old = resolve_compare_protocol(profile_id, revision)
    assert prompt_name(profile_id, revision, fallback=old.display) == old.display
    assert prompt_name(profile_id, revision) == profile_id


def test_unknown_profile_never_borrows_a_current_name():
    assert prompt_name("unknown@1.0", "1.1.1", fallback="自定义协议") == "自定义协议"
    assert prompt_name("unknown@1.0", "1.1.1") == "unknown@1.0"


def test_catalog_matches_current_identities_and_reads_availability_from_registry():
    records = prompt_name_catalog()
    assert [(r["evaluation_profile"], r["bundle_revision"], r["canonical_name"]) for r in records] == APPROVED
    assert {(r["evaluation_profile"], r["bundle_revision"]) for r in records} == {
        (protocol.id, protocol.bundle_revision) for protocol in list_compare_protocols()
    }
    assert len({r["canonical_name"] for r in records}) == 12
    for record in records:
        protocol = resolve_compare_protocol(record["evaluation_profile"], record["bundle_revision"])
        assert record["legacy_name"] == protocol.display
        assert record["standard_version"] == protocol.standard_version
        assert record["selectable"] is protocol.selectable
    hidden = {r["evaluation_profile"] for r in records if not r["selectable"]}
    assert hidden == {
        "qa_competitor_compare@0.3", "qa_competitor_compare@0.2-simplified-calibrated",
        "qa_competitor_compare@0.2-simplified-thinking-exposure", "qa_chat_compare@1.0",
    }
    timeliness = resolve_compare_protocol("timeliness_compare@1.0")
    assert prompt_name(timeliness.id, timeliness.bundle_revision).endswith("-生产")
    assert timeliness.status == "experimental" and timeliness.selectable is True


def test_display_helpers_do_not_change_protocol_metadata_or_templates():
    protocols = list_compare_protocols()
    before = [(deepcopy(p.public_metadata()), deepcopy(p.request_metadata()),
               p.system_template, p.user_template, p.status, p.selectable) for p in protocols]
    for record in prompt_name_catalog():
        prompt_name(record["evaluation_profile"], record["bundle_revision"])
    after = [(p.public_metadata(), p.request_metadata(), p.system_template,
              p.user_template, p.status, p.selectable) for p in protocols]
    assert after == before
    for protocol in protocols:
        assert "canonical_name" not in protocol.public_metadata()
        assert "canonical_name" not in protocol.request_metadata()


def test_catalog_callers_cannot_mutate_later_display_records():
    records = prompt_name_catalog()
    records[0]["canonical_name"] = "被调用方改写"
    records[0]["selectable"] = False
    records.clear()
    fresh = prompt_name_catalog()
    assert fresh[0]["canonical_name"] == APPROVED[0][2]
    assert fresh[0]["selectable"] is True


def test_document_mapping_matches_the_complete_runtime_catalog():
    path = Path(__file__).resolve().parents[1] / "docs" / "prompt-name-mapping.md"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("| ") and "@" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows.append(cells)
    expected = [[r["evaluation_profile"], r["bundle_revision"], r["standard_version"],
                 r["legacy_name"], r["canonical_name"], "是" if r["selectable"] else "否"]
                for r in prompt_name_catalog()]
    assert rows == expected
