from io import BytesIO

import httpx
from openpyxl import load_workbook
import pytest

from auto_eval.web import server
from auto_eval.web.human_baselines import HumanStore, read_json
from auto_eval.web.human_compare import HumanComparisons
from test_human_compare import example, put_baseline


@pytest.mark.asyncio
async def test_api_freeze_generate_reopen_export_and_isolation(tmp_path,monkeypatch):
    task,_,baseline,mapping=example()
    service=HumanComparisons(HumanStore(tmp_path))
    put_baseline(service.store,baseline)
    monkeypatch.setattr(server,"HUMAN_COMPARISONS",service)
    async def peek(task_id):return task if task_id=="test" else None
    monkeypatch.setattr(server,"peek_task_async",peek)
    before=[dict(i) for i in task.items]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post('/api/human-comparisons/preview',json=dict(baseline_id="hb_test",version=1,tasks=[mapping.model_dump()],dimensions=["understanding"]))
        assert response.status_code==200,response.text
        preview=response.json()
        task.results[0]["answer1_understanding_score"]=1
        response=await client.post('/api/human-comparisons',json=dict(preview_id=preview["preview_id"],config_sha256=preview["config_sha256"]))
        assert response.status_code==202,response.text
        rid=response.json()["report_id"]
        await service.close()
        report=(await client.get('/api/human-comparisons/'+rid)).json()
        assert report["status"]=="ready",report
        assert report["metrics"]["scores"][0]["mae"]==1
        task.items.clear()
        report_again=(await client.get('/api/human-comparisons/'+rid)).json()
        assert report_again==report
        data=await client.get('/api/human-comparisons/'+rid+'/download')
        assert data.status_code==200
        wb=load_workbook(BytesIO(data.content))
        assert wb.sheetnames==["对比概览","人机评分统计","产品差距对比","分歧明细","匹配与排除","统计口径"]
        ws=wb["人机评分统计"]
        assert ws['J3'].value==0 and ws['J3'].number_format=='0.00%'
        assert ws['L3'].value==1
        detail=(await client.get('/api/human-comparisons/'+rid+'/rows',params={"direction":"higher"})).json()
        assert detail["total"]==2
        assert all(r["difference"]>0 for r in detail["items"])
        reports=(await client.get('/api/human-comparisons',params={"task_id":"test"})).json()
        assert reports["items"][0]["report_id"]==rid


@pytest.mark.asyncio
async def test_upload_to_publish_api_with_template(tmp_path,monkeypatch):
    task,_,_,_=example()
    service=HumanComparisons(HumanStore(tmp_path))
    monkeypatch.setattr(server,"HUMAN_COMPARISONS",service)
    async def peek(task_id):return task
    monkeypatch.setattr(server,"peek_task_async",peek)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.get('/api/eval/test/human-template',params={"standard":"0.2-simplified"})
        assert response.status_code==200,response.text
        wb=load_workbook(BytesIO(response.content))
        ws=wb['人工标注']
        headers=[c.value for c in ws[1]]
        ws.cell(2,headers.index('人工分_产品1_理解需求')+1,4)
        data=BytesIO();wb.save(data)
        upload=await client.post('/api/human-baselines/imports',files={'file':('human.xlsx',data.getvalue())})
        assert upload.status_code==200,upload.text
        info=upload.json()
        mapping=info['suggested_mapping'];mapping['policy_note']='指定人工标准'
        preview=await client.post(f'/api/human-baselines/imports/{info["import_id"]}/preview',json=mapping)
        assert preview.status_code==200,preview.text
        published=await client.post('/api/human-baselines',json=dict(import_id=info['import_id'],preview_sha256=preview.json()['preview_sha256'],name='test'))
        assert published.status_code==200,published.text
        assert (await client.get('/api/human-baselines')).json()['total']==1
