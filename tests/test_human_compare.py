import copy
from types import SimpleNamespace

import pytest

from auto_eval.web.human_baselines import HumanError, HumanStore, atomic_json, digest
from auto_eval.web.human_compare import (ComparisonRequest, GenerateRequest, HumanComparisons, TaskMapping,
                                        align_baseline_to_run, freeze_run_snapshot)
from auto_eval.web.tasks import Task
from test_compare_statistics import make_snapshot


def example(scores=None,standard="0.2-simplified",media=False):
    data=make_snapshot(scores or [[4,3],[2,5]],standard)
    for item,result in zip(data["items"],data["results"]):
        item.update(answer1="source A",context1="",answer2="source B",context2="")
        if item["product_count"]==3:item.update(answer3="source C",context3="")
        if media:item.update(video1="a.mp4",video2="b.mp4")
        result.update({k:v for k,v in item.items() if k in ("query","context","answer1","answer2","answer3","context1","context2","context3")})
    task=Task(id=data["task_id"],mode="compare",items=data["items"],results=data["results"],options={},status="done",
              evaluation_profile=data["evaluation_profile"],protocol_manifest=data["protocol_manifest"])
    snapshot=freeze_run_snapshot(task)
    products=[dict(product_id=f"p{n}",display_name=f"产品{n}") for n in range(1,data["items"][0]["product_count"]+1)]
    cases,labels=[],[]
    for item in data["items"]:
        ck="catalog:"+item["id"]
        responses={p["product_id"]:dict(answer=item[f"answer{n}"],context="",answer_text_origin="source",context_origin="source",
                                        evidence=item.get(f"video{n}","")) for n,p in enumerate(products,1)}
        cases.append(dict(case_key=ck,case_id=item["id"],query=item["query"],context="",context_origin="source",query_hashes=[],responses=responses))
        for p in products:
            labels.append(dict(case_key=ck,product_id=p["product_id"],dimension_id="understanding",score=3,
                               score_status="scored",resolution="accepted",source={},reason=""))
    baseline=dict(baseline_id="hb_test",version=1,name="人工参考",products=products,cases=cases,labels=labels,states=[],
                  created_at=1800000000,case_count=len(cases),label_count=len(labels),
                  score_range=[0,3] if standard=="0.3" else [1,5],human_standard_version=standard,
                  human_policy_note="测试人工标准",purpose="regression",labels_sha256=digest(labels))
    mapping=TaskMapping(task_id="test",product_map={f"answer{n}":p["product_id"] for n,p in enumerate(products,1)})
    return task,snapshot,baseline,mapping


def put_baseline(store,baseline):
    folder=store.path("baselines",baseline["baseline_id"],"v1")
    for key in ("cases","labels","states"):atomic_json(folder/f"{key}.json",baseline[key])
    atomic_json(folder/"manifest.json",{k:v for k,v in baseline.items() if k not in ("cases","labels","states")})


def rows(baseline,snapshot,mapping):
    return align_baseline_to_run(baseline,snapshot,mapping,["case-0","case-1"],["understanding"],"same_standard")


def test_swapped_products_and_stale_results():
    task,snapshot,baseline,mapping=example()
    assert all(r["comparable"] for r in rows(baseline,snapshot,mapping))
    changed=copy.deepcopy(snapshot)
    changed['items'][0]['context']='new background'
    assert rows(baseline,changed,mapping)[0]['match_status']=='content_mismatch'
    assert rows(baseline,changed,mapping)[0]['result_input_binding']=='binding_unverified'
    baseline['cases'][0]['session_group']='original-conversation'
    changed['items'][0].update(context='',session_group='different-conversation')
    assert rows(baseline,changed,mapping)[0]['match_status']=='content_mismatch'
    baseline['cases'][0].pop('session_group')
    for item,result in zip(task.items,task.results):
        item["answer1"],item["answer2"]=item["answer2"],item["answer1"]
        result["answer1"],result["answer2"]=result["answer2"],result["answer1"]
        result["answer1_understanding_score"],result["answer2_understanding_score"]=result["answer2_understanding_score"],result["answer1_understanding_score"]
    swapped=rows(baseline,freeze_run_snapshot(task),mapping.model_copy(update={"product_map":{"answer1":"p2","answer2":"p1"}}))
    assert swapped[0]["model_score"]==4 and all(r["comparable"] for r in swapped)
    task.items[0]["answer1"]="new answer"
    stale=rows(baseline,freeze_run_snapshot(task),mapping)[0]
    assert stale["result_input_binding"]=="stale_result" and not stale["comparable"]


def test_freeze_active_runs_and_stable_digest():
    task,snapshot,_,_=example()
    task.active_runs=1
    with pytest.raises(HumanError,match="仍在执行"):freeze_run_snapshot(task)
    task.active_runs=0
    task.repair_status="queued"
    with pytest.raises(HumanError):freeze_run_snapshot(task)
    task.repair_status="idle"
    task.results[0]["timings"]={"elapsed_s":123}
    assert snapshot["snapshot_sha256"]==freeze_run_snapshot(task)["snapshot_sha256"]
    task.results[0]["index"]="0"
    assert freeze_run_snapshot(task)["results"][0]["index"]==0
    task.results[0]["answer1_understanding_score"]=5
    assert snapshot["snapshot_sha256"]!=freeze_run_snapshot(task)["snapshot_sha256"]


def test_media_confirmation_is_scoped_and_conflicts_cannot_be_overridden(tmp_path):
    task,snapshot,baseline,mapping=example(media=True)
    store=HumanStore(tmp_path)
    put_baseline(store,baseline)
    service=HumanComparisons(store)
    req=ComparisonRequest(baseline_id="hb_test",version=1,tasks=[mapping],dimensions=["understanding"])
    preview=service.create_preview(req,[snapshot])
    assert preview["comparable_n"]==0
    ids=[i for g in preview["confirmations"] for i in g["match_ids"]]
    bids=[i for g in preview["confirmations"] for i in g["binding_ids"]]
    request=GenerateRequest(preview_id=preview["preview_id"],config_sha256=preview["config_sha256"],
                            confirm_match_ids=ids,confirm_binding_ids=bids,confirm_reason="对照采集清单确认同一批回答")
    manifest=service.prepare_report(request)
    assert service.prepare_report(request)["report_id"]==manifest["report_id"]
    task.items[0]["query"]="changed after preview"
    service.generate(manifest["report_id"])
    from auto_eval.web.human_baselines import read_json
    report=read_json(store.path("reports",manifest["report_id"],"report.json"))
    assert all(r["comparable"] for r in report["rows"])
    assert report["rows"][0]["query"]!="changed after preview"
    assert read_json(store.path("reports",manifest["report_id"]))["status"]=="ready"
    second=service.create_preview(req,[freeze_run_snapshot(task)])
    with pytest.raises(HumanError,match="明确冲突"):
        service.prepare_report(GenerateRequest(preview_id=second["preview_id"],config_sha256=second["config_sha256"],
                               confirm_match_ids=[second["rows"][0]["match_id"]],confirm_reason="ignore"))


def test_vqa_conflict_protocols_and_missing_ids(tmp_path):
    task,snapshot,baseline,mapping=example()
    baseline["cases"][0]["query_hashes"]=["original"]
    task.items[0].update(query_images=["other.png"],query_image_meta=[{"original_sha256":"other"}])
    assert rows(baseline,freeze_run_snapshot(task),mapping)[0]["match_status"]=="content_mismatch"
    baseline["score_range"]=[0,3]
    with pytest.raises(HumanError,match="分制不兼容"):rows(baseline,snapshot,mapping)
    baseline["score_range"]=[1,5]
    baseline["human_standard_version"]="0.2-simplified-calibrated"
    with pytest.raises(HumanError,match="回归比较"):rows(baseline,snapshot,mapping)
    allowed=align_baseline_to_run(baseline,snapshot,mapping,["case-0"],["understanding"],"regression")
    assert allowed[0]["compatible"]
    store=HumanStore(tmp_path);put_baseline(store,baseline)
    task.items[0].pop("id")
    with pytest.raises(HumanError,match="真实题号"):
        HumanComparisons(store).create_preview(ComparisonRequest(baseline_id="hb_test",version=1,tasks=[mapping]),[freeze_run_snapshot(task)])


@pytest.mark.parametrize('standard',['0.2-simplified','0.2-simplified-calibrated','0.2-simplified-thinking-exposure','0.3'])
@pytest.mark.parametrize('count',[2,3])
@pytest.mark.parametrize('vqa',[False,True])
@pytest.mark.parametrize('evidence_mode',['video_frames','long_screenshot'])
def test_protocol_modality_evidence_product_matrix(standard,count,vqa,evidence_mode):
    task,_,baseline,mapping=example([[2]*count,[3]*count],standard,media=True)
    for item,result,case in zip(task.items,task.results,baseline['cases']):
        item['evidence_mode']=evidence_mode
        if vqa:
            item.update(query_images=['question.png'],query_image_meta=[{'original_sha256':'question-sha'}])
            case['query_hashes']=['question-sha']
        item['input_manifest_sha256']='evaluation-input'
        result['input_manifest_sha256']='evaluation-input'
        for n in range(1,count+1):
            item[f'capture_id{n}']=f'capture-{item["id"]}-{n}'
            if evidence_mode=='long_screenshot':
                item[f'screenshot{n}']=f'answer{n}.png';item.pop(f'video{n}',None)
            case['responses'][f'p{n}']['response_id']=item[f'capture_id{n}']
    aligned=rows(baseline,freeze_run_snapshot(task),mapping)
    assert len(aligned)==count*2 and all(r['comparable'] for r in aligned)


def test_unknown_protocol_and_restart_recovery(tmp_path):
    task,snapshot,baseline,mapping=example()
    store=HumanStore(tmp_path);put_baseline(store,baseline)
    service=HumanComparisons(store)
    snapshot['results'][0]['bundle_revision']='future-revision'
    preview=service.create_preview(ComparisonRequest(baseline_id='hb_test',version=1,tasks=[mapping],dimensions=['understanding']),[snapshot])
    with pytest.raises(HumanError,match='未知评分标准'):
        service.prepare_report(GenerateRequest(preview_id=preview['preview_id'],config_sha256=preview['config_sha256']))
    good=service.create_preview(ComparisonRequest(baseline_id='hb_test',version=1,tasks=[mapping],dimensions=['understanding']),[freeze_run_snapshot(task)])
    manifest=service.prepare_report(GenerateRequest(preview_id=good['preview_id'],config_sha256=good['config_sha256']))
    service.recover()
    from auto_eval.web.human_baselines import read_json
    assert read_json(store.path('reports',manifest['report_id']))['status']=='error'
    service.generate(manifest['report_id'])
    assert read_json(store.path('reports',manifest['report_id']))['status']=='ready'
