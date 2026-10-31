"""Presentation-only prompt names keyed by an exact, frozen protocol identity.

These labels never enter CompareProtocol metadata or request fingerprints.
Missing or historical bundle revisions retain the caller's original label.
"""
from __future__ import annotations

from ..judges.compare_protocols import resolve_compare_protocol


_PROMPT_NAMES = {
    ("qa_competitor_compare@0.2-simplified", "0.2.1"):
        "问答对比-V1-P0.2.1-261022-生产",
    ("qa_competitor_compare@0.2-simplified-plus", "0.2.3"):
        "问答对比-V1-P0.2.3-261030-生产",
    ("qa_competitor_compare@0.2-simplified-1022", "0.2.6"):
        "问答对比-V1-P0.2.6-261023-实验",
    ("qa_chat_compare@1.1", "1.1.0"):
        "闲聊对比-V1-P1.1.0-261028-实验",
    ("qa_chat_compare@1.1-demand-calibrated", "1.1.3"):
        "闲聊对比-V1-P1.1.3-261030-实验",
    ("chitchat_evidence_first_flat@1.1", "1.1.1"):
        "闲聊对比-V1-P1.1.1-261030-实验",
    ("timeliness_compare@1.0", "1.0.2"):
        "时效性对比-V1-P1.0.2-261022-生产",
    ("knowledge_qa_compare@1.0", "1.0.0"):
        "知识问答对比-V1-P1.0.0-261029-实验",
    ("qa_competitor_compare@0.3", "0.3.1"):
        "问答对比-V0.3-P0.3.1-261022-实验",
    ("qa_competitor_compare@0.2-simplified-calibrated", "0.2.3"):
        "问答对比-V1-P0.2.3-261022-实验",
    ("qa_competitor_compare@0.2-simplified-thinking-exposure", "0.2.3"):
        "问答对比-V1-P0.2.3-思考暴露-261022-实验",
    ("qa_chat_compare@1.0", "1.0.0"):
        "闲聊对比-V1-P1.0.0-261022-实验",
}


def prompt_name(
    profile_id: str,
    bundle_revision: str | None = None,
    *,
    fallback: str = "",
) -> str:
    """Resolve only an approved exact identity, preserving historical labels."""
    return _PROMPT_NAMES.get((profile_id, bundle_revision), fallback or profile_id)


def prompt_name_catalog() -> list[dict]:
    """Return independent display records without changing protocol metadata.

    Selection availability comes from the registry, never the label suffix.
    In particular, naming a prompt ``生产`` does not promote its protocol status.
    """
    records = []
    for (profile_id, revision), canonical_name in _PROMPT_NAMES.items():
        protocol = resolve_compare_protocol(profile_id, revision)
        records.append({
            "evaluation_profile": profile_id,
            "bundle_revision": revision,
            "canonical_name": canonical_name,
            "legacy_name": protocol.display,
            "standard_version": protocol.standard_version,
            "selectable": protocol.selectable,
        })
    return records


def prompt_display_fields(record: dict) -> dict:
    """Build response-only labels from frozen identity, never infer a revision.

    Old history sidecars intentionally need no full-snapshot read or migration.
    Without a recorded revision their original family label remains visible.
    """
    if record.get("mode") not in (None, "compare"):
        return {}
    manifest = record.get("protocol_manifest") or {}
    profile_id = record.get("evaluation_profile") or manifest.get("id") or ""
    if not profile_id:
        return {}
    revision = manifest.get("bundle_revision") or record.get("bundle_revision")
    legacy_name = manifest.get("display") or record.get("legacy_evaluation_profile_display")
    if not legacy_name:
        try:
            legacy_name = resolve_compare_protocol(profile_id, revision).display
        except ValueError:
            legacy_name = profile_id
    conflicting_identity = (
        record.get("protocol_name_identity_conflict") is True
        or (manifest.get("id") and manifest["id"] != profile_id)
        or (manifest.get("bundle_revision") and record.get("bundle_revision")
            and manifest["bundle_revision"] != record["bundle_revision"])
    )
    return {
        "bundle_revision": revision,
        "legacy_evaluation_profile_display": legacy_name,
        "evaluation_profile_display": (
            legacy_name if conflicting_identity
            else prompt_name(profile_id, revision, fallback=legacy_name)
        ),
        **({"protocol_name_identity_conflict": True} if conflicting_identity else {}),
    }
