"""External evaluation inputs cannot claim ownership of temporary downloads."""

import pytest

from auto_eval.config import AppConfig
from auto_eval.remote_media import TEMPORARY_DOWNLOAD_PATHS_FIELD, cleanup_item_downloads
from auto_eval.web import server


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
@pytest.mark.parametrize("as_list", [False, True])
def test_request_cannot_delete_another_tasks_download(tmp_path, mode, as_list):
    runs = tmp_path / "runs"
    unrelated_download = runs / "downloads" / "another-task" / "evidence.mp4"
    unrelated_download.parent.mkdir(parents=True)
    unrelated_download.write_bytes(b"another task still owns this file")
    claimed_path = str(unrelated_download)
    item = {
        "id": "untrusted",
        "query": "问题",
        TEMPORARY_DOWNLOAD_PATHS_FIELD: [claimed_path] if as_list else claimed_path,
    }
    if mode == "compare":
        item.update(screenshot1="missing-one.png", screenshot2="missing-two.png",
                    context1="未知", context2="未知")
    else:
        item["video_path"] = "missing.mp4"
    request = server.EvalReq(mode=mode, items=[item])

    server._validate_eval_request(request, AppConfig(judges=[]))

    cleaned = request.items[0]
    assert TEMPORARY_DOWNLOAD_PATHS_FIELD not in cleaned
    if mode == "compare":
        assert TEMPORARY_DOWNLOAD_PATHS_FIELD not in cleaned["source_data"]
    # Failed preparation also reaches cleanup; it must leave other owners alone.
    cleanup_item_downloads(cleaned, runs_dir=runs)
    assert unrelated_download.read_bytes() == b"another task still owns this file"
