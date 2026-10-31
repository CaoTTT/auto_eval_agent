"""Display aliases follow frozen revisions without changing evaluation identity."""
from copy import deepcopy
import hashlib
import json

import pytest

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.judges.compare_protocols import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, DEFAULT_COMPARE_PROTOCOL_ID,
    TIMELINESS_COMPARE_PROTOCOL_ID, V02_PLUS_COMPARE_PROTOCOL_ID,
    V1022_COMPARE_PROTOCOL_ID, list_compare_protocols, resolve_compare_protocol,
)
from auto_eval.web import history, server
from auto_eval.web.prompt_names import prompt_display_fields, prompt_name, prompt_name_catalog
from auto_eval.web.scheduler import EvalScheduler, _QueuedEval
from auto_eval.web.tasks import Task


# These are the request-metadata identities before presentation aliases were
# introduced. Even a label-only change inside request_metadata alters replay.
REQUEST_METADATA_SHA256 = {
    "qa_competitor_compare@0.2-simplified": "66104cd7ab3cb9182792c62db3b2b2a6f1b9f2f8d5b67bbc4bc691cabe669613",
    "qa_competitor_compare@0.2-simplified-plus": "da333c7729934eeae98ad50b1e5a8cda47d7e4cfc8838ab3ad4db31bfdb9f763",
    "qa_competitor_compare@0.3": "a22c64b1f7a3ba311852dfab069804f2ff8fa6629828f14d004a307893a5054d",
    "qa_competitor_compare@0.2-simplified-calibrated": "97de641db7303f89f6c4ee97db735f347e257e94ddfd1662565787ff7d39636f",
    "qa_competitor_compare@0.2-simplified-thinking-exposure": "319de09de906542922c84f21cb8a28c0dc523fbcf2d9016bac72d13f7efb273f",
    "qa_chat_compare@1.0": "c7aa4e44f06e07c6aaf933a8bc56887b6301d203e12e2f5ddb06fcd9beef7d4f",
    "qa_chat_compare@1.1": "d72edd4e0027cf2898a53e54cf1686efab0e6882f7239b1d6c2efb8d489edd8d",
    "qa_chat_compare@1.1-demand-calibrated": "c5a1166cecf913faf101e42bb1c4be35b3bb845a64acfee3186697250f0102ab",
    "chitchat_evidence_first_flat@1.1": "20166396e849219cf2be1e7cca4ca122c06ed50c6f4c88a01c930bdecc24da27",
    "timeliness_compare@1.0": "b099c289467ef1b437bcfe77f28969c2ec2feee208e7c8f4c9f51fe13d0fe7b0",
    "knowledge_qa_compare@1.0": "474063f1f1b5d59cffa109128eb0683172a6ba3ee43d9779fb36750ab8108d9c",
    "qa_competitor_compare@0.2-simplified-1022": "61d7ed6ce0b8d54f6f60581a5e8142dd3a40875694e0fbbfbc5accc3d9e0a552",
}


def request_hash(protocol):
    serialized = json.dumps(protocol.request_metadata(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def case():
    return {"id": "case", "query": "合成展示测试", "product_count": 2,
            "video1": "one.mp4", "video2": "two.mp4",
            "answer1": "原产品1", "answer2": "原产品2",
            "context1": "原产品1背景", "context2": "原产品2背景"}


def task_for(profile_id=DEFAULT_COMPARE_PROTOCOL_ID, revision="current"):
    protocol = resolve_compare_protocol(profile_id, None if revision in ("current", None) else revision)
    manifest = protocol.public_metadata()
    result = {"index": 0, "item_id": "case", "query": "合成展示测试",
              "evaluation_profile": profile_id, "bundle_revision": protocol.bundle_revision,
              "raw_model_output": '{"original": "untouched"}'}
    if revision is None:
        manifest.pop("bundle_revision")
        result.pop("bundle_revision")
    return Task(id="prompt-name-task", mode="compare", items=[case()], options={},
                evaluation_profile=profile_id, protocol_manifest=manifest,
                dataset_name="synthetic.csv", status="done", results=[result])


@pytest.fixture
def app_config(monkeypatch):
    config = AppConfig(judges=[JudgeConfig(name="offline", model="offline")])
    monkeypatch.setattr(server, "cfg", lambda: config)
    return config


def test_configuration_exposes_aliases_separately_from_frozen_protocol_metadata(app_config):
    before = {p.id: deepcopy(p.public_metadata()) for p in list_compare_protocols()}
    response = server.api_config()
    catalog = response["prompt_name_catalog"]
    assert catalog == prompt_name_catalog()
    names = {(row["evaluation_profile"], row["bundle_revision"]): row for row in catalog}
    assert len(names) == len(response["evaluation_profiles"]) == 12
    for profile in response["evaluation_profiles"]:
        protocol = resolve_compare_protocol(profile["id"])
        record = names[(protocol.id, protocol.bundle_revision)]
        assert profile["canonical_name"] == record["canonical_name"]
        assert profile["display"] == record["legacy_name"] == before[protocol.id]["display"]
        assert profile["canonical_name"] != profile["display"]
        assert profile["selectable"] is protocol.selectable
        assert protocol.public_metadata() == before[protocol.id]
        assert "canonical_name" not in protocol.public_metadata()
        assert "canonical_name" not in protocol.request_metadata()
        assert request_hash(protocol) == REQUEST_METADATA_SHA256[protocol.id]


@pytest.mark.parametrize("profile_id,revision", [
    (DEFAULT_COMPARE_PROTOCOL_ID, "0.2.0"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2"),
    (V1022_COMPARE_PROTOCOL_ID, "0.2.5"),
    (CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, "1.1.0"),
    (TIMELINESS_COMPARE_PROTOCOL_ID, "1.0.1"),
])
def test_historical_revisions_do_not_inherit_the_current_date_or_display_name(profile_id, revision):
    protocol = resolve_compare_protocol(profile_id, revision)
    assert prompt_name(profile_id, revision, fallback=protocol.display) == protocol.display
    assert prompt_name(profile_id, revision) == profile_id
    current = resolve_compare_protocol(profile_id)
    assert prompt_name(profile_id, revision, fallback=protocol.display) != prompt_name(profile_id, current.bundle_revision)


@pytest.mark.parametrize("revision", [None, "", "not-recorded"])
def test_missing_or_unknown_revision_keeps_the_original_label(revision):
    assert prompt_name(DEFAULT_COMPARE_PROTOCOL_ID, revision, fallback="原冻结名称") == "原冻结名称"
    assert prompt_name(DEFAULT_COMPARE_PROTOCOL_ID, revision) == DEFAULT_COMPARE_PROTOCOL_ID


@pytest.mark.parametrize("profile_id,revision", [
    (DEFAULT_COMPARE_PROTOCOL_ID, "current"),
    (V1022_COMPARE_PROTOCOL_ID, "current"),
    (CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, "current"),
    (V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.2"),
    (CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, "1.1.0"),
    (DEFAULT_COMPARE_PROTOCOL_ID, None),
])
def test_history_rows_details_and_queue_use_the_same_frozen_revision(tmp_path, profile_id, revision):
    task = task_for(profile_id, revision)
    snapshot = history.task_to_snapshot(task)
    original = deepcopy(snapshot)
    recorded_revision = task.protocol_manifest.get("bundle_revision")
    expected = prompt_name(profile_id, recorded_revision, fallback=task.protocol_manifest["display"])
    row = history._snapshot_meta_row(snapshot, tmp_path / "task.json")
    assert (row.get("bundle_revision") or None) == recorded_revision
    assert row["evaluation_profile_display"] == expected
    detail = history.snapshot_payload(snapshot)
    assert detail["evaluation_profile_display"] == expected
    job = _QueuedEval(job_id=task.id, kind="initial", task=task, cfg=object(), runner=lambda *_: None, total=1)
    entry = EvalScheduler._entry(job, status="queued")
    assert (entry.get("bundle_revision") or None) == recorded_revision
    assert entry["evaluation_profile_display"] == expected
    assert snapshot == original
    assert detail["results"] == original["results"]
    assert detail["items"] == original["items"]
    assert task.protocol_manifest == original["protocol_manifest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["current", "1.1.0", None])
async def test_dataset_and_history_detail_return_display_metadata_without_rewriting_sources(monkeypatch, revision):
    task = task_for(CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, revision)
    before_items, before_results, before_manifest = deepcopy(task.items), deepcopy(task.results), deepcopy(task.protocol_manifest)

    async def peek(*_args, **_kwargs):
        return task

    monkeypatch.setattr(server, "peek_task_async", peek)
    monkeypatch.setattr(server, "peek_task", lambda *_args, **_kwargs: task)
    expected = prompt_name(task.evaluation_profile, task.protocol_manifest.get("bundle_revision"),
                           fallback=task.protocol_manifest["display"])
    dataset = await server.api_dataset(task.id)
    assert dataset["evaluation_profile"] == task.evaluation_profile
    assert dataset["protocol_manifest"] == before_manifest
    assert dataset["evaluation_profile_display"] == expected
    detail = server.api_history_detail(task.id)
    assert detail["evaluation_profile_display"] == expected
    assert detail["results"] == before_results
    assert task.items == before_items and task.results == before_results and task.protocol_manifest == before_manifest
    dataset["items"][0]["answer1"] = "独立副本修改"
    assert task.items == before_items


@pytest.mark.asyncio
async def test_new_task_alias_is_response_only_and_keeps_the_frozen_request_identity(monkeypatch, app_config):
    protocol = resolve_compare_protocol(CHITCHAT_FLAT_COMPARE_PROTOCOL_ID)
    original_public, original_request = deepcopy(protocol.public_metadata()), deepcopy(protocol.request_metadata())
    created = []

    def create(mode, items, options, **kwargs):
        task = Task(id=kwargs.pop("task_id"), mode=mode, items=items, options=options, **kwargs)
        created.append(task)
        return task

    async def source_files(*_args, **_kwargs):
        return []

    monkeypatch.setattr(server, "new_task", create)
    monkeypatch.setattr(server, "_request_source_files", source_files)
    monkeypatch.setattr(server.EVAL_SCHEDULER, "enqueue", lambda *_args: 1)
    response = await server.api_eval(server.EvalReq(mode="compare", items=[case()], evaluation_profile=protocol.id))
    expected = prompt_name(protocol.id, protocol.bundle_revision)
    assert response["evaluation_profile_display"] == expected
    task = created[0]
    assert response["protocol_manifest"] == task.protocol_manifest
    assert task.protocol_manifest["display"] == original_public["display"]
    assert "canonical_name" not in task.protocol_manifest
    assert "evaluation_profile_display" not in task.protocol_manifest
    assert "prompt_name_catalog" not in task.protocol_manifest
    assert protocol.public_metadata() == original_public
    assert protocol.request_metadata() == original_request
    assert request_hash(protocol) == REQUEST_METADATA_SHA256[protocol.id]


def test_old_summary_cache_keeps_legacy_label_without_reading_or_rewriting_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    task = task_for(CHITCHAT_FLAT_COMPARE_PROTOCOL_ID, "1.1.0")
    snapshot = history.task_to_snapshot(task)
    path = tmp_path / "historical.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    original_bytes = path.read_bytes()
    old_meta = history._snapshot_meta_row(snapshot, path)
    for field in ("bundle_revision", "evaluation_profile_display", "legacy_evaluation_profile_display"):
        old_meta.pop(field, None)
    old_meta["meta_version"] = 2
    history._meta_path(path).write_text(json.dumps(old_meta, ensure_ascii=False), encoding="utf-8")
    read_text = type(path).read_text

    def cached_only(candidate, *args, **kwargs):
        if candidate == path:
            pytest.fail("Listing an existing summary must not read the full historical snapshot")
        return read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", cached_only)
    row = history._load_meta_row(path)
    assert not row.get("bundle_revision")
    assert row["evaluation_profile_display"] == task.protocol_manifest["display"]
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("conflict", ["bundle_revision", "profile_id"])
def test_identity_conflict_stays_visible_after_history_summary_roundtrip(tmp_path, conflict):
    task = task_for(V02_PLUS_COMPARE_PROTOCOL_ID)
    snapshot = history.task_to_snapshot(task)
    if conflict == "bundle_revision":
        snapshot["bundle_revision"] = "0.2.2"
    else:
        snapshot["protocol_manifest"]["id"] = DEFAULT_COMPARE_PROTOCOL_ID
    before = deepcopy(snapshot)
    legacy_name = task.protocol_manifest["display"]
    current_name = prompt_name(V02_PLUS_COMPARE_PROTOCOL_ID, "0.2.3")
    direct = prompt_display_fields(snapshot)
    assert direct["protocol_name_identity_conflict"] is True
    assert direct["evaluation_profile_display"] == legacy_name != current_name

    path = tmp_path / f"conflicting-{conflict}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    original_bytes = path.read_bytes()
    meta = history._snapshot_meta_row(snapshot, path)
    assert meta["protocol_name_identity_conflict"] is True
    assert meta["evaluation_profile_display"] == legacy_name
    history._write_meta(path, meta)
    restored = history._load_meta_row(path)
    assert restored["protocol_name_identity_conflict"] is True
    assert restored["evaluation_profile_display"] == legacy_name != current_name
    assert prompt_display_fields(restored)["evaluation_profile_display"] == legacy_name
    assert snapshot == before
    assert path.read_bytes() == original_bytes
