"""Cache identity must bind the source bytes, configuration and returned frames."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.web import runner, video_prepare
from auto_eval.web.tasks import Task


def extractor(calls):
    def extract(video, directory, **kwargs):
        calls.append(video.read_bytes())
        frame=directory / "kf_001.jpg"
        frame.write_bytes(video.read_bytes())
        return [frame]
    return extract


def test_same_path_same_size_same_timestamp_replacement_invalidates_cache(tmp_path):
    source=tmp_path / "source.mp4"
    source.write_bytes(b"video-A")
    stamp=source.stat()
    calls=[]
    extract=extractor(calls)
    first=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    source.write_bytes(b"video-B")
    os.utime(source,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    second=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    assert calls==[b"video-A",b"video-B"]
    assert first!=second, "new video generations must not overwrite historical frame paths"
    assert first[0].read_bytes()==b"video-A" and second[0].read_bytes()==b"video-B"


def test_corrupt_frame_same_count_is_not_a_cache_hit(tmp_path):
    source=tmp_path / "source.mp4";source.write_bytes(b"video")
    calls=[];extract=extractor(calls)
    first=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    first[0].write_bytes(b"wrong")
    second=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    assert len(calls)==2 and second[0].read_bytes()==b"video"


def test_unchanged_cache_hit_and_policy_change_are_isolated(tmp_path):
    source=tmp_path / "source.mp4";source.write_bytes(b"video")
    calls=[];extract=extractor(calls)
    first=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract,cache_key="policy-A")
    same=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract,cache_key="policy-A")
    changed=video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract,cache_key="policy-B")
    assert len(calls)==2 and first==same and changed!=first
    marker=json.loads((first[0].parent / ".complete").read_text(encoding="utf-8"))
    assert marker["source"]["sha256"]==hashlib.sha256(b"video").hexdigest()
    assert marker["frames"][0]["sha256"]==hashlib.sha256(first[0].read_bytes()).hexdigest()


def test_concurrent_same_cache_extracts_once(tmp_path):
    source=tmp_path / "source.mp4";source.write_bytes(b"video")
    calls=[];extract=extractor(calls)
    def prepare(_):
        return video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(prepare,range(4)))
    assert len(calls)==1 and all(paths==results[0] for paths in results)


def test_video_changed_during_extraction_never_publishes_cache(tmp_path):
    source=tmp_path / "source.mp4";source.write_bytes(b"first")
    def extract(video,directory):
        frame=directory / "kf_001.jpg";frame.write_bytes(video.read_bytes())
        video.write_bytes(b"other")
        return [frame]
    with pytest.raises(ValueError,match="抽帧期间"):
        video_prepare._extract_frames(source,tmp_path / "frames",extract_fn=extract)
    assert not list((tmp_path / "frames").rglob(".complete"))


def test_old_marker_is_not_trusted_and_full_config_is_in_key(tmp_path):
    source=tmp_path / "source.mp4";source.write_bytes(b"current")
    directory=tmp_path / "frames";directory.mkdir()
    (directory / "kf_001.jpg").write_bytes(b"old")
    (directory / ".complete").write_text(json.dumps({"cache_key":video_prepare.KEYFRAME_ALGORITHM_VERSION,"frame_count":1}))
    calls=[]
    frames=video_prepare._extract_frames(source,directory,extract_fn=extractor(calls))
    assert calls==[b"current"] and frames[0].read_bytes()==b"current"
    profile=VisualModeProfile(extraction={"algorithm_version":"test"})
    kwargs,key=video_prepare._rich_content_timing({},24,profile,str(source))
    assert json.loads(key)["config"]==asdict(kwargs["config"])


@pytest.mark.parametrize("count", [2,3])
def test_preparation_records_each_product_source_and_only_reextracts_changed_one(tmp_path,count):
    item={"id":"case","product_count":count}
    for n in range(1,count+1):
        (tmp_path / f"{n}.mp4").write_bytes(f"video{n}".encode())
        item[f"video{n}"]=f"{n}.mp4"
    calls=[]
    kwargs=dict(profile=VisualModeProfile(extraction={"algorithm_version":"test"}),
                session_name="test",item_index=0,total_items=1,base_dir=tmp_path,runs_dir=tmp_path / "runs",
                probe_fn=lambda p:24,extract_fn=extractor(calls))
    first=video_prepare.prepare_session_visual_compare_item(item,**kwargs)
    (tmp_path / "2.mp4").write_bytes(b"replaced")
    second=video_prepare.prepare_session_visual_compare_item(first,**kwargs)
    assert len(calls)==count+1
    assert first["frames1"]==second["frames1"] and first["frames2"]!=second["frames2"]
    assert second["video_source2"]["sha256"]==hashlib.sha256(b"replaced").hexdigest()
    assert Path(first["frames2"][0]).read_bytes()==b"video2"


def test_video_download_rejects_replaced_original(tmp_path,monkeypatch):
    from fastapi import HTTPException
    from auto_eval.web import server
    source=tmp_path / "source.mp4";source.write_bytes(b"original")
    data={"items":[{"video_path":str(source),"video_source":video_prepare._video_identity(source)}]}
    monkeypatch.setattr(server,"peek_task",lambda *a,**k:None)
    monkeypatch.setattr(server,"load_snapshot",lambda *a:data)
    monkeypatch.setattr(server,"_resolve_operation_video_path",lambda raw:Path(raw))
    assert server.api_export_item("test",0,format="video").path==source
    source.write_bytes(b"replaced")
    with pytest.raises(HTTPException) as exc:
        server.api_export_item("test",0,format="video")
    assert exc.value.status_code==409


def test_real_video_replacement_preserves_old_frames_and_extracts_new_content(tmp_path):
    ffmpeg=shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the real video identity check")
    source=tmp_path / "source.mp4"
    def video(color):
        subprocess.run([ffmpeg,"-v","error","-y","-f","lavfi","-i",f"color=c={color}:s=120x160:r=2:d=3",
                        "-c:v","libx264","-threads","1","-pix_fmt","yuv420p",str(source)],
                       check=True,capture_output=True,timeout=30)
    kwargs=dict(profile=VisualModeProfile(extraction={"algorithm_version":"test-cache"}),
                session_name="real",item_index=0,total_items=1,base_dir=tmp_path,runs_dir=tmp_path / "runs")
    video("red")
    first=video_prepare.prepare_session_rich_content_item({"id":"same","video_path":str(source)},**kwargs)
    video("blue")
    second=video_prepare.prepare_session_rich_content_item(first,**kwargs)
    assert first["frames"]!=second["frames"] and first["video_source"]["sha256"]!=second["video_source"]["sha256"]
    with Image.open(first["frames"][0]) as old,Image.open(second["frames"][0]) as new:
        r,_,b=old.convert("RGB").getpixel((60,80));assert r>b+150
        r,_,b=new.convert("RGB").getpixel((60,80));assert b>r+150


@pytest.mark.parametrize("mode", ["compare","rich_content"])
async def test_runner_revalidates_prepared_frames_when_video_is_present(monkeypatch,mode):
    class Client:
        def __init__(self,*args): pass
    prepared=[]
    def prepare(item,**kwargs):
        prepared.append(item["id"])
        if mode=="compare":
            return {**item,"frames1":["new1.jpg"],"frames2":["new2.jpg"]}
        return {**item,"frames":["new.jpg"]}
    async def evaluate(*args,**kwargs):
        return {"query":"q"}
    async def finish(*args): pass
    monkeypatch.setattr(runner,"JudgeClient",Client)
    monkeypatch.setattr(runner,"_eval_one",evaluate)
    monkeypatch.setattr(runner,"_persist_task",lambda *a:None)
    monkeypatch.setattr(runner,"prepare_session_visual_compare_item",prepare)
    monkeypatch.setattr(runner,"prepare_session_rich_content_item",prepare)
    item={"id":"case","query":"q"}
    if mode=="compare":
        item.update(video1="a.mp4",video2="b.mp4",frames1=["old1.jpg"],frames2=["old2.jpg"])
    else:
        item.update(video_path="a.mp4",frames=["old.jpg"])
    task=Task(id="cache-check",mode=mode,items=[item],options={})
    cfg=AppConfig(judges=[JudgeConfig(name="test")],visual_modes={"rich_content":VisualModeProfile(extraction={"algorithm_version":"test"})})
    one,_=runner._make_item_evaluator(task,cfg,on_result=finish)
    result=await one(0,item)
    assert "error" not in result,result
    assert prepared==["case"]
