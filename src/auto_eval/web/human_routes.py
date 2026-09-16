"""HTTP boundary for human references and frozen comparisons."""
from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import Field

from .human_baselines import HumanError, ImportMapping, StrictModel, atomic_json, read_json, safe_id
from .human_baseline_import import MAX_UPLOAD, build_human_template, inspect_workbook, parse_human_labels
from .human_compare import ComparisonRequest, GenerateRequest, freeze_run_snapshot


class PublishRequest(StrictModel):
    import_id: str
    preview_sha256: str
    name: str = Field(min_length=1,max_length=200)
    baseline_id: str = ""
    expected_version: int = Field(default=0,ge=0)
    valid_only: bool = False
    save_mapping: bool = True


def install_human_routes(app, service_getter, peek):
    router=APIRouter()

    @app.exception_handler(HumanError)
    async def human_error(request: Request, exc: HumanError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status,content={"detail":str(exc)})

    async def freeze(task_id):
        task=await peek(safe_id(task_id))
        if task is None:
            raise HumanError("任务不存在",404)
        return freeze_run_snapshot(task)

    @router.get("/api/eval/{task_id}/human-template")
    async def template(task_id: str, standard: str=Query(min_length=1)):
        snapshot=await freeze(task_id)
        try:
            payload=await asyncio.to_thread(build_human_template,snapshot,standard)
        except ValueError as exc:
            raise HumanError(str(exc)) from exc
        return Response(payload,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition":'attachment; filename="human-template.xlsx"'})

    @router.post("/api/human-baselines/imports")
    async def upload(file: UploadFile=File(...)):
        try:
            if not (file.filename or "").lower().endswith(".xlsx"):
                raise HumanError("请选择 .xlsx 文件",400)
            data=await file.read(MAX_UPLOAD+1)
            if len(data)>MAX_UPLOAD:
                raise HumanError("上传文件不能超过 20 MB",400)
        finally:
            await file.close()
        store=service_getter().store
        import_id="hi_"+uuid.uuid4().hex[:20]
        path=store.path("imports",import_id,"source.xlsx")
        def inspect():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(data)
            return inspect_workbook(path)
        return dict(import_id=import_id,**await asyncio.to_thread(inspect))

    @router.get("/api/human-baselines/imports/{import_id}")
    async def inspect(import_id: str, sheet_name: str="", header_row: int=Query(default=1,ge=1,le=30)):
        return await asyncio.to_thread(inspect_workbook,service_getter().store.path("imports",import_id,"source.xlsx"),sheet_name,header_row)

    @router.post("/api/human-baselines/imports/{import_id}/preview")
    async def import_preview(import_id: str, mapping: ImportMapping):
        store=service_getter().store
        def parse():
            result=parse_human_labels(store.path("imports",import_id,"source.xlsx"),mapping)
            atomic_json(store.path("imports",import_id,"preview.json"),result)
            return {k:v for k,v in result.items() if k not in ("cases","states","labels")}|{"labels":result["labels"][:30]}
        return await asyncio.to_thread(parse)

    @router.post("/api/human-baselines")
    async def publish(request: PublishRequest):
        return await asyncio.to_thread(service_getter().store.publish_baseline,**request.model_dump())

    @router.get("/api/human-baselines")
    async def baselines(search: str="",page: int=Query(default=1,ge=1),page_size: int=Query(default=20,ge=1,le=100)):
        rows=await asyncio.to_thread(service_getter().store.list_baselines,search)
        return dict(total=len(rows),items=rows[(page-1)*page_size:page*page_size])

    @router.get("/api/human-baselines/{baseline_id}/versions/{version}")
    async def baseline(baseline_id: str,version: int,page: int=Query(default=1,ge=1),page_size: int=Query(default=30,ge=1,le=100)):
        data=await asyncio.to_thread(service_getter().store.load_baseline,baseline_id,version)
        return {k:v for k,v in data.items() if k not in ("cases","labels","states")}|{
            "labels":data["labels"][(page-1)*page_size:page*page_size],"total":len(data["labels"])}

    @router.post("/api/human-comparisons/preview")
    async def preview(request: ComparisonRequest):
        snapshots=[]
        for task in request.tasks:
            snapshots.append(await freeze(task.task_id))
        return await asyncio.to_thread(service_getter().create_preview,request,snapshots)

    @router.get("/api/human-comparisons/previews/{preview_id}/rows")
    async def preview_rows(preview_id: str,page: int=Query(default=1,ge=1),page_size: int=Query(default=50,ge=1,le=100)):
        data=await asyncio.to_thread(read_json,service_getter().store.path("previews",preview_id))
        return dict(total=len(data["rows"]),items=data["rows"][(page-1)*page_size:page*page_size])

    @router.post("/api/human-comparisons",status_code=202)
    async def generate(request: GenerateRequest):
        service=service_getter()
        manifest=await asyncio.to_thread(service.prepare_report,request)
        if manifest["status"] in ("queued","error"):
            service.schedule(manifest["report_id"])
        return manifest

    @router.post("/api/human-comparisons/{report_id}/retry",status_code=202)
    async def retry(report_id: str):
        service=service_getter()
        manifest=await asyncio.to_thread(read_json,service.store.path("reports",report_id))
        if manifest["status"]=="error":
            manifest.update(status="queued",error=None)
            await asyncio.to_thread(atomic_json,service.store.path("reports",report_id),manifest)
            service.schedule(report_id)
        return manifest

    @router.get("/api/human-comparisons")
    async def reports(task_id: str="",page: int=Query(default=1,ge=1),page_size: int=Query(default=20,ge=1,le=100)):
        def read():
            rows=[read_json(p) for p in (service_getter().store.root/"human_reports").glob("*/manifest.json")]
            rows=sorted((r for r in rows if not task_id or task_id in r["task_ids"]),key=lambda r:r["created_at"],reverse=True)
            return dict(total=len(rows),items=rows[(page-1)*page_size:page*page_size])
        return await asyncio.to_thread(read)

    def get_report(report_id):
        service=service_getter()
        manifest=read_json(service.store.path("reports",report_id))
        if manifest["status"]!="ready":
            return manifest
        return read_json(service.store.path("reports",report_id,"report.json"))|manifest

    @router.get("/api/human-comparisons/{report_id}")
    async def report(report_id: str):
        data=await asyncio.to_thread(get_report,report_id)
        return {k:v for k,v in data.items() if k not in ("rows","baseline")}|{
            "products":data.get("baseline",{}).get("products",[]),"row_count":len(data.get("rows",[]))}

    @router.get("/api/human-comparisons/{report_id}/rows")
    async def rows(report_id: str,task_id: str="",product_id: str="",dimension_id: str="",category: str="",
                   input_modality: str="",evidence_mode: str="",direction: str="",state_mismatch: bool=False,
                   min_difference: float=Query(default=0,ge=0),page: int=Query(default=1,ge=1),page_size: int=Query(default=50,ge=1,le=100)):
        def read():
            data=get_report(report_id)
            values=data.get("rows",[])
            filters=dict(task_id=task_id,product_id=product_id,dimension_id=dimension_id,category=category,input_modality=input_modality,evidence_mode=evidence_mode)
            values=[r for r in values if all(not v or r[k]==v for k,v in filters.items())]
            if min_difference:
                values=[r for r in values if r["difference"] is not None and abs(r["difference"])>=min_difference]
            if direction in ("higher","lower"):
                values=[r for r in values if r["difference"] is not None and (r["difference"]>0 if direction=="higher" else r["difference"]<0)]
            if state_mismatch:
                values=[r for r in values if (r["human_score"] is None)!=(r["model_score"] is None) or
                        r["human_applicable"] is not None and r["model_applicable"] is not None and r["human_applicable"]!=r["model_applicable"]]
            return dict(total=len(values),items=values[(page-1)*page_size:page*page_size])
        return await asyncio.to_thread(read)

    @router.get("/api/human-comparisons/{report_id}/download")
    async def download(report_id: str):
        manifest=await asyncio.to_thread(read_json,service_getter().store.path("reports",report_id))
        if manifest["status"]!="ready":
            raise HumanError("报告尚未生成",409)
        return FileResponse(service_getter().store.path("reports",report_id,"report.xlsx"),
                            filename=f'{manifest["baseline_name"]}_人工基准v{manifest["version"]}_人机对比_{report_id}.xlsx',
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    app.include_router(router)
