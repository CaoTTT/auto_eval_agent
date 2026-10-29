"""Recover recorded protocol facts without upgrading historical chat tasks."""
from __future__ import annotations

from ..judges.compare_protocols import DEFAULT_COMPARE_PROTOCOL_ID, resolve_compare_protocol


def recover_protocol_identity(snapshot: dict) -> dict:
    manifest = dict(snapshot.get("protocol_manifest") or {})
    if snapshot.get("mode", "compare") != "compare":
        return {"evaluation_profile": snapshot.get("evaluation_profile", ""),
                "manifest": manifest, "diagnostics": [], "executable": True, "is_chat": False,
                "is_timeliness": False, "is_knowledge_qa": False}
    options = snapshot.get("options") or {}
    records = [snapshot, manifest, options, *(snapshot.get("results") or [])]
    profiles, families, versions, revisions, schemas = [], set(), set(), set(), set()
    diagnostics = []
    for record in records:
        profile = record.get("evaluation_profile")
        if record is manifest:
            if record.get("id") and profile and record["id"] != profile:
                diagnostics.append("冻结清单内 id 与 evaluation_profile 冲突")
            profile = record.get("id") or profile
        if profile:
            profiles.append(str(profile))
            if "@" in str(profile):
                family, version = str(profile).split("@", 1)
                families.add(family)
                versions.add(version)
        if record.get("standard_id"):
            families.add(str(record["standard_id"]))
        if record.get("standard_version"):
            versions.add(str(record["standard_version"]))
        if record.get("bundle_revision"):
            revisions.add(str(record["bundle_revision"]))
        if record.get("result_schema_version"):
            schemas.add(str(record["result_schema_version"]))
            if str(record["result_schema_version"]).startswith("knowledge-qa-result-"):
                families.add("knowledge_qa_compare")
    if len(set(profiles)) > 1 or len(families) > 1 or len(versions) > 1:
        diagnostics.append("任务、清单或结果的协议身份冲突，禁止续跑和正式评分统计")
    if len(revisions) > 1:
        diagnostics.append("任务与结果的协议包修订冲突，禁止续跑和正式评分统计")
    if len(schemas) > 1:
        diagnostics.append("任务与结果的结果Schema版本冲突，禁止续跑和正式评分统计")
    if profiles:
        profile = profiles[0]
    elif len(families) == 1 and len(versions) == 1:
        profile = f"{next(iter(families))}@{next(iter(versions))}"
    elif versions:
        # Historical competitor records did not always persist a family.
        profile = f"qa_competitor_compare@{next(iter(versions))}"
    elif "qa_chat_compare" in families:
        profile = "qa_chat_compare@unknown"
    elif families and families != {"qa_competitor_compare"}:
        profile = f"{next(iter(families))}@unknown"
        diagnostics.append("已有标准家族但缺少版本，禁止猜测旧默认协议")
    else:
        profile = DEFAULT_COMPARE_PROTOCOL_ID
    chat = "qa_chat_compare" in families or profile.startswith("qa_chat_compare@")
    timeliness = "timeliness_compare" in families or profile.startswith("timeliness_compare@")
    knowledge = "knowledge_qa_compare" in families or profile.startswith("knowledge_qa_compare@")
    revision = next(iter(revisions)) if len(revisions) == 1 else None
    if chat and not revision:
        diagnostics.append("问答闲聊任务缺少冻结的 bundle_revision，不能用当前版本补写历史")
    if timeliness and not revision:
        diagnostics.append("时效性任务缺少冻结的 bundle_revision，不能用当前版本补写历史")
    if timeliness and not schemas:
        diagnostics.append("时效性任务缺少冻结的结果Schema，不能用当前版本补写历史")
    if knowledge and not revision:
        diagnostics.append("知识问答任务缺少冻结的 bundle_revision，不能用当前版本补写历史")
    if knowledge and not schemas:
        diagnostics.append("知识问答任务缺少冻结的结果Schema，不能用当前版本补写历史")
    try:
        protocol = resolve_compare_protocol(profile, revision)
    except ValueError as exc:
        diagnostics.append(str(exc))
        protocol = None
    if protocol is not None and (chat or timeliness or knowledge) and schemas and schemas != {protocol.result_schema_version}:
        diagnostics.append("记录的结果Schema与冻结协议不一致，禁止续跑和正式评分统计")
    if protocol is not None and not diagnostics:
        # Only restore facts actually recorded for chat. Legacy compatibility
        # may use its existing inference, with that provenance made explicit.
        manifest.setdefault("id", profile)
        manifest.setdefault("standard_id", protocol.standard_id)
        manifest.setdefault("standard_version", protocol.standard_version)
        if revision:
            manifest.setdefault("bundle_revision", revision)
        if len(schemas) == 1:
            manifest.setdefault("result_schema_version", next(iter(schemas)))
        if not (chat or timeliness or knowledge) and not revision:
            manifest.setdefault("recovery_source", "legacy_compatible_inference")
    return {"evaluation_profile": profile, "manifest": manifest,
            "diagnostics": list(dict.fromkeys(diagnostics)), "executable": not diagnostics,
            "is_chat": chat, "is_timeliness": timeliness, "is_knowledge_qa": knowledge}


def require_snapshot_protocol(snapshot: dict):
    recovered = recover_protocol_identity(snapshot)
    if not recovered["executable"]:
        raise ValueError("；".join(recovered["diagnostics"]))
    return resolve_compare_protocol(recovered["evaluation_profile"],
                                    recovered["manifest"].get("bundle_revision"))
