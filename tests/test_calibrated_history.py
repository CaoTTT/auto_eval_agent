import copy

import pytest

from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
)
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import _task_from_snapshot


@pytest.mark.parametrize(
    "version_fields",
    [
        {"protocol_manifest": {"id": V02_CALIBRATED_COMPARE_PROTOCOL_ID}},
        {"protocol_manifest": {"standard_version": "0.2-simplified-calibrated"}},
        {"summary": {"standard_version": "0.2-simplified-calibrated"}},
        {"results": [{"index": 0, "standard_version": "0.2-simplified-calibrated"}]},
        {"results": [{"index": 0, "evaluation_profile": V02_CALIBRATED_COMPARE_PROTOCOL_ID}]},
    ],
)
def test_calibrated_profile_restores_identically_for_history_and_task_loading(version_fields):
    snapshot = {"task_id": "calibrated", "mode": "compare", "status": "done", **version_fields}
    original = copy.deepcopy(snapshot)

    payload = snapshot_payload(snapshot)
    task = _task_from_snapshot(snapshot, "calibrated")

    assert payload["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
    assert task.evaluation_profile == V02_CALIBRATED_COMPARE_PROTOCOL_ID
    assert snapshot_payload(task_to_snapshot(task))["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
    assert snapshot == original


@pytest.mark.parametrize("explicit_source", ["task", "options", "manifest"])
def test_explicit_legacy_profile_takes_priority_over_calibrated_result(explicit_source):
    snapshot = {
        "task_id": "explicit",
        "mode": "compare",
        "status": "done",
        "results": [{"index": 0, "standard_version": "0.2-simplified-calibrated",
                     "evaluation_profile": V02_CALIBRATED_COMPARE_PROTOCOL_ID}],
    }
    if explicit_source == "task":
        snapshot["evaluation_profile"] = DEFAULT_COMPARE_PROTOCOL_ID
        snapshot["options"] = {"evaluation_profile": V03_COMPARE_PROTOCOL_ID}
        snapshot["protocol_manifest"] = {"id": V02_CALIBRATED_COMPARE_PROTOCOL_ID}
    elif explicit_source == "options":
        snapshot["options"] = {"evaluation_profile": DEFAULT_COMPARE_PROTOCOL_ID}
        snapshot["protocol_manifest"] = {"id": V02_CALIBRATED_COMPARE_PROTOCOL_ID}
    else:
        snapshot["protocol_manifest"] = {"id": DEFAULT_COMPARE_PROTOCOL_ID}

    assert snapshot_payload(snapshot)["evaluation_profile"] == DEFAULT_COMPARE_PROTOCOL_ID
    assert _task_from_snapshot(snapshot, "explicit").evaluation_profile == DEFAULT_COMPARE_PROTOCOL_ID


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, DEFAULT_COMPARE_PROTOCOL_ID),
        ({"results": [{"standard_version": "0.3"}]}, V03_COMPARE_PROTOCOL_ID),
        ({"summary": {"standard_version": "0.2-simplified"}}, DEFAULT_COMPARE_PROTOCOL_ID),
        ({"mode": "rich_content", "protocol_manifest": {"id": V02_CALIBRATED_COMPARE_PROTOCOL_ID}}, ""),
    ],
)
def test_legacy_and_non_compare_profile_fallbacks_are_preserved(fields, expected):
    snapshot = {"task_id": "legacy", "mode": "compare", "status": "done", **fields}
    assert snapshot_payload(snapshot)["evaluation_profile"] == expected
    assert _task_from_snapshot(snapshot, "legacy").evaluation_profile == expected


def test_calibrated_frozen_revision_survives_restore_and_export():
    manifest = {"id": V02_CALIBRATED_COMPARE_PROTOCOL_ID,
                "standard_version": "0.2-simplified-calibrated", "bundle_revision": "0.2.2"}
    snapshot = {"task_id": "frozen", "mode": "compare", "status": "done",
                "protocol_manifest": manifest,
                "results": [{"index": 0, "error": "timeout"}]}
    task = _task_from_snapshot(snapshot, "frozen")
    restored = snapshot_payload(task_to_snapshot(task))
    assert task.protocol_manifest == manifest
    assert restored["protocol_manifest"] == manifest
    assert restored["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
