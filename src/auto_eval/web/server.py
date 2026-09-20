"""FastAPI 后端：路由 + SSE 实时流 + 静态前端挂载。

启动：python -m auto_eval.web.server  （默认 http://localhost:8054）
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from dotenv import load_dotenv
from PIL import Image
from fastapi import Request, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..config import load_config
from ..judge_profiles import (
    model_profiles, default_profile_id, configured_profile,
    resolve_new_runtime, restore_runtime, check_frozen_options, legacy_runtime,
)
from ..judges.compare_protocols import (
    list_compare_protocols,
    resolve_compare_protocol,
)
from ..media import probe_duration
from ..paths import RUNS_DIR
from ..query_images import normalize_query_input, PREPARED_FIELDS, prepare_query_images, QueryImageError
from ..conversation import normalize_turn, validate_conversations, is_conversation, DIAGNOSTIC_FIELDS
from ..preparation import run_preparation
from .parse_input import Mode, normalize_compare_evidence, parse_csv, parse_jsonl, parse_text
from .history import (
    write_xlsx,
    delete_snapshot,
    export_rows,
    list_snapshots,
    load_item_judge_calls,
    load_snapshot,
    result_export_row,
    rows_to_csv,
    save_task,
    snapshot_payload,
    task_to_snapshot,
    write_frames_zip,
)
from .video_prepare import (
    VIDEO_EXTENSIONS,
    operation_video_roots,
    resolve_operation_video_path,
)
from .runner import run_eval, run_retry, run_resume, finish_pause, run_update_batch, spawn_background, snapshot_item_progress
from .execution_control import resume_indexes
from .scheduler import EvalScheduler
from .exports import XlsxExports, xlsx_download_name
from .dataset_media import DatasetMedia, file_hash
from .human_baselines import HumanStore
from .human_compare import HumanComparisons
from .human_routes import install_human_routes
from .persistence import queue_task_save, wait_task_save, task_save_pending
from .tasks import (
    TASKS,
    get_task,
    get_task_async,
    merge_items_by_id,
    new_task,
    latest_results_by_index,
    peek_task,
    peek_task_async,
)

# auto_eval_agent/ 目录（src/auto_eval/web/server.py 往上 4 层）
BASE_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = BASE_DIR / "config"
STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv(BASE_DIR / ".env", override=True)  # 注入 .env 的 key；以 .env 为准覆盖旧 shell 环境变量

app = FastAPI(title="auto_eval 评估台")
_state: dict = {}
EVAL_SCHEDULER = EvalScheduler()
XLSX_EXPORTS = XlsxExports(RUNS_DIR / "exports")
DATASET_MEDIA = DatasetMedia()
HUMAN_COMPARISONS = HumanComparisons(HumanStore(RUNS_DIR))
install_human_routes(app, lambda: HUMAN_COMPARISONS, lambda task_id: peek_task_async(task_id))


@app.on_event("startup")
async def _load():
    _state["cfg"] = load_config(CONFIG_DIR)
    await asyncio.to_thread(HUMAN_COMPARISONS.recover)
    EVAL_SCHEDULER.start()


@app.on_event("shutdown")
async def _shutdown():
    await EVAL_SCHEDULER.stop()
    await XLSX_EXPORTS.close()
    await HUMAN_COMPARISONS.close()


def cfg():
    return _state["cfg"]


class ParseReq(BaseModel):
    mode: Mode
    text: str | None = None
    jsonl: str | None = None
    csv: str | None = None


class EvalReq(BaseModel):
    mode: Mode
    items: list[dict]
    options: dict = {}
    dataset_name: str = ""
    evaluation_profile: str | None = None


class EvalItemsReq(BaseModel):
    """向已有 task 批量更新/追加 items 的请求（task_id 等全部走 Body 参数）。"""

    task_id: str
    mode: Mode | None = None  # 更新已有任务可省略；新建任务必填；不一致 422
    items: list[dict]
    options: dict = {}
    dataset_name: str = ""  # 仅新建时生效，更新时忽略
    evaluation_profile: str | None = None


class HistoryNoteReq(BaseModel):
    note: str = ""


class RetryReq(BaseModel):
    """人工失败补跑；indexes 为空时选择当前全部技术失败/未完成项。"""

    indexes: list[int] | None = None
    include_unfinished: bool = True
    options: dict = {}
    idempotency_key: str = ""


class QueuePositionReq(BaseModel):
    action: Literal["move_up", "move_down", "move_to_front"]


class ResumeReq(BaseModel):
    concurrency: int = Field(ge=1, le=128, strict=True)
    include_failed: bool = False
    idempotency_key: str = Field(default="", max_length=128)

    model_config = {"extra": "forbid"}


_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS


def _compare_protocol_or_422(protocol_id: str | None):
    try:
        return resolve_compare_protocol(protocol_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _protocol_manifest(protocol, app_cfg, options: dict, resolved_judges=None) -> dict:
    """Freeze non-secret runtime facts needed to interpret/reproduce a task."""
    manifest = protocol.public_metadata()
    visual_profile = app_cfg.visual_modes.get("rich_content")
    if visual_profile is not None:
        manifest["media_algorithm_version"] = visual_profile.extraction.algorithm_version
        manifest["visual_profile"] = visual_profile.model_dump()
        manifest["input_schema_version"] = "1.1"
    selected = options.get("judges") or (
        [app_cfg.judges[0].name] if app_cfg.judges else []
    )
    manifest["judges"] = [
        {
            "name": judge.name,
            "model": judge.model,
            "temperature": judge.temperature,
            "top_p": judge.top_p,
            "seed": judge.seed,
            "vl_high_resolution_images": judge.vl_high_resolution_images,
            "enable_thinking": judge.enable_thinking,
        }
        for judge in (resolved_judges if resolved_judges is not None else app_cfg.judges)
        if resolved_judges is not None or judge.name in selected
    ]
    if visual_profile is not None:
        from ..image_limits import resolve_image_limits
        candidates = resolved_judges if resolved_judges is not None else [j for j in app_cfg.judges if j.name in selected]
        if candidates:
            manifest["image_limits"] = resolve_image_limits(candidates[0], visual_profile)
    return manifest


def _new_judge_runtime(app_cfg, options):
    try:
        runtime, judges = resolve_new_runtime(app_cfg, options)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    options["judges"] = [j.name for j in judges]
    options["judge_model_profile"] = runtime["profile_id"]
    if judges[0].enable_thinking is not None:
        options["enable_thinking"] = judges[0].enable_thinking
    return runtime, judges


def _check_judge_options(task, options):
    try:
        check_frozen_options(task, options)
        if task.judge_runtime:
            restore_runtime(cfg(), task.judge_runtime)
        else:
            legacy_runtime(cfg(), task)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _freeze_legacy_judge(task, app_cfg):
    if not task.judge_runtime:
        task.judge_runtime = legacy_runtime(app_cfg, task)


def _resolve_operation_video_path(raw_path: str) -> Path:
    return resolve_operation_video_path(raw_path, base_dir=BASE_DIR)


def _validate_eval_request(req: EvalReq, app_cfg, *, previous_items=None) -> None:
    """提交前校验：compare 模式支持完整的2或3产品同类视觉证据。"""
    selected = req.options.get("judges") or (
        [app_cfg.judges[0].name] if app_cfg.judges else []
    )
    selected_judges = [judge for judge in app_cfg.judges if judge.name in selected]
    if not selected_judges:
        selected_judges = app_cfg.judges[:1]
    if req.mode != "compare":
        return
    _compare_protocol_or_422(req.evaluation_profile)
    invalid: list[str] = []
    for index, item in enumerate(req.items, 1):
        try:
            if not is_conversation(item):
                # Preserve legacy metadata, but don't let it activate the runner's
                # conversation path after request validation.
                if "session_id" in item or "turn_index" in item:
                    item.setdefault("source_data", {key: value for key, value in item.items() if key != "source_data"})
                for field in ("session_id", "turn_index", "session_group"):
                    item.pop(field, None)
            item.update(normalize_turn(item, external=True))
            for field in DIAGNOSTIC_FIELDS - {"session_id", "session_group", "turn_index", "input_schema_version"}:
                item.pop(field, None)
            item.pop("conversation_assets", None)
            item.pop("screenshot_evidence", None)
            item.update(normalize_query_input(item))
            for field in PREPARED_FIELDS:
                item.pop(field, None)
            for field in ("media", "frame_count", "video_source", *(f"{prefix}{n}" for n in (1, 2, 3)
                          for prefix in ("frames", "duration", "screenshot_meta", "video_source"))):
                item.pop(field, None)
            for n in (1, 2, 3):
                item.pop(f"video{n}_path", None)
            normalized = normalize_compare_evidence(item)
            # Preserve unused input paths for provenance, never prepared evidence.
            normalized.setdefault("source_data", {key: value for key, value in item.items()
                                                   if key != "source_data"})
            item.clear()
            item.update(normalized)
        except ValueError as exc:
            invalid.append(f"第{index}条 {exc}")
    try:
        validate_conversations([*(previous_items or []), *req.items])
    except ValueError as exc:
        invalid.append(str(exc))
    if invalid:
        preview = "；".join(invalid[:8])
        suffix = "……" if len(invalid) > 8 else ""
        raise HTTPException(
            422,
            f"垂域视觉对比输入不完整：{preview}{suffix}",
        )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# task_id / item id 允许所有字符（含中文/符号），仅限长度防文件名超长。
_MAX_ID_LENGTH = 128


def _validate_param_id(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(422, f"{label}不能为空")
    if len(value) > _MAX_ID_LENGTH:
        raise HTTPException(422, f"{label}不能超过 {_MAX_ID_LENGTH} 个字符")
    return value


def _validate_batch_item_ids(items: list[dict]) -> None:
    """更新批：所有条目必须携带非空字符串 id，且批内唯一。"""
    seen: set[str] = set()
    for pos, item in enumerate(items, 1):
        iid = item.get("id")
        if not isinstance(iid, str) or not iid.strip():
            raise HTTPException(422, f"第 {pos} 条缺少非空字符串 id")
        if len(iid) > _MAX_ID_LENGTH:
            raise HTTPException(
                422, f"第 {pos} 条 id 超过 {_MAX_ID_LENGTH} 个字符"
            )
        if iid in seen:
            raise HTTPException(422, f"id 重复：{iid}")
        seen.add(iid)


@app.get("/api/config")
def api_config():
    from ..request_throttle import recommended_concurrency, supports_bailian_pacing
    from ..preparation import preparation_concurrency

    c = cfg()
    profiles = model_profiles(c)
    public_profiles = []
    for profile in profiles:
        judge = configured_profile(c, profile, profile.default_enable_thinking)
        public_profiles.append({
            "id": profile.id, "display": profile.display, "model": profile.model,
            "judge_name": profile.judge_name, "supports_thinking": profile.supports_thinking,
            "default_enable_thinking": profile.default_enable_thinking,
            "recommended_concurrency": recommended_concurrency([judge]),
            "request_pacing": supports_bailian_pacing(judge),
        })
    return {
        "judge_model_profiles": public_profiles,
        "default_judge_model_profile": default_profile_id(c, profiles),
        "media_concurrency": preparation_concurrency(),
        "judges": [
            {"name": j.name, "display": j.display or j.name,
             "recommended_concurrency": recommended_concurrency([j]),
             "request_pacing": supports_bailian_pacing(j)}
            for j in c.judges
        ],
        "evaluation_profiles": [
            profile.public_metadata() for profile in list_compare_protocols()
        ],
    }


@app.post("/api/parse")
def api_parse(req: ParseReq):
    if req.csv:
        items, errs = parse_csv(req.csv, req.mode)
    elif req.jsonl:
        items, errs = parse_jsonl(req.jsonl, req.mode)
    elif req.text is not None:
        items, errs = parse_text(req.text, req.mode)
    else:
        raise HTTPException(400, "需提供 text、jsonl 或 csv")
    return {"items": items, "errors": errs, "count": len(items),
            "rejected_count": len(errs),
            "evidence_counts": {mode: sum(item.get("evidence_mode") == mode for item in items)
                                for mode in ("long_screenshot", "video_frames")}}


@app.post("/api/compare/preflight")
async def api_compare_preflight(req: EvalReq):
    from ..image_limits import resolve_image_limits
    from ..judges.conversation_prompt import assemble_conversation
    from .conversation_prepare import ConversationPreparation
    if req.mode != "compare":
        raise HTTPException(422, "多轮预检查仅支持 compare")
    app_cfg = cfg()
    _validate_eval_request(req, app_cfg)
    _, judges = _new_judge_runtime(app_cfg, req.options)
    profile = app_cfg.visual_modes["rich_content"]
    protocol = _compare_protocol_or_422(req.evaluation_profile)
    limits = resolve_image_limits(judges[0], profile)
    def inspect():
        prepared = ConversationPreparation(req.items, "preflight-" + uuid.uuid4().hex, profile, limits)
        rows, findings = [], {}
        for item in sorted((it for it in req.items if it.get("session_id")), key=lambda it: (it["session_id"], it["turn_index"])):
            error = None
            try:
                bundle = prepared.prepare(item)
                system = protocol.system_template.render(persona=judges[0].persona,
                    product_count=item.get("product_count", 2), evidence_mode="long_screenshot")
                assemble_conversation(system, bundle, protocol)
            except Exception as exc:
                error = str(exc)
            rows.append({"id": item["id"], "error": error,
                **{k: item[k] for k in DIAGNOSTIC_FIELDS if k in item and k != "image_findings"}})
            for finding in item.get("image_findings", []):
                saved = findings.setdefault(finding["finding_id"], {**finding, "target_turns": []})
                saved["target_turns"].append(item["turn_index"])
        return {"input_schema_version": "2.0", "accepted_session_count": len(prepared.index.groups),
            "accepted_turn_count": len(rows), "rejected_sessions": [], "warning_count": len(findings),
            "blocked_turn_count": sum(bool(r["error"]) for r in rows), "findings": list(findings.values()),
            "turns": rows, "limits_profile_version": limits["limits_profile_version"],
            "preflight_status": "completed", "model": judges[0].model}
    return await asyncio.to_thread(inspect)


@app.get("/api/request-pacing")
async def api_request_pacing(model: str | None = None) -> dict:
    """Read local request pacing without creating a controller or exposing keys."""
    from ..request_throttle import MODEL, supports_bailian_pacing

    controllers = getattr(asyncio.get_running_loop(), "_auto_eval_request_throttles", {})
    controller = controllers.get(model or MODEL)
    result = {
        "enabled": any(supports_bailian_pacing(judge) for judge in cfg().judges),
        "active": controller is not None,
        "scope": "process_event_loop",
        "controller": controller.snapshot() if controller is not None else None,
    }
    app_cfg = cfg()
    if hasattr(app_cfg, "judge_model_profiles"):
        from ..request_throttle import pacing_config
        policies = []
        for profile in model_profiles(app_cfg):
            judge = configured_profile(app_cfg, profile, profile.default_enable_thinking)
            policy = pacing_config(judge)
            if policy:
                policies.append({"profile_id": profile.id, **policy})
        snapshots = [{"model": key, **value.snapshot()} for key, value in controllers.items()]
        selected = next((row for row in snapshots if row.get("model") == model or model in row.get("models", [])), None) if model else (snapshots[0] if snapshots else None)
        result.update(controllers=snapshots, policies=policies,
                      enabled=any(p["model"] == model for p in policies) if model else bool(policies),
                      active=selected is not None, controller=selected)
    return result


@app.post("/api/eval")
async def api_eval(req: EvalReq):
    if not req.items:
        raise HTTPException(400, "items 为空")
    app_cfg = cfg()
    judge_runtime, judges = _new_judge_runtime(app_cfg, req.options)
    _validate_eval_request(req, app_cfg)
    from ..request_throttle import recommended_concurrency

    req.options.setdefault("concurrency", recommended_concurrency(judges))
    try:
        capacity = int(req.options["concurrency"])
    except (ValueError, TypeError):
        raise HTTPException(422, "并发容量须为 1–128 的整数")
    if isinstance(req.options["concurrency"], bool) or capacity != req.options["concurrency"] or not 1 <= capacity <= 128:
        raise HTTPException(422, "并发容量须为 1–128 的整数")
    protocol = (
        _compare_protocol_or_422(req.evaluation_profile)
        if req.mode == "compare"
        else None
    )
    task = new_task(
        req.mode,
        req.items,
        req.options,
        dataset_name=req.dataset_name.strip(),
        evaluation_profile=protocol.id if protocol else "",
        protocol_manifest=(
            _protocol_manifest(protocol, app_cfg, req.options, judges) if protocol else {}
        ),
        judge_runtime=judge_runtime,
    )
    queue_position = EVAL_SCHEDULER.enqueue(task, app_cfg, run_eval)
    return {
        "task_id": task.id,
        "status": "queued",
        "queue_position": queue_position,
        "evaluation_profile": task.evaluation_profile,
        "judge_runtime": task.judge_runtime,
    }


@app.get("/api/queue")
async def api_queue():
    """查看当前全量批跑任务及 FIFO 等待队列。"""
    return EVAL_SCHEDULER.snapshot()


@app.delete("/api/queue/{task_id}")
async def api_queue_cancel(task_id: str):
    """取消仍在等待队列中的全量评测任务。"""
    task_id = _validate_param_id(task_id, "task_id")
    task = EVAL_SCHEDULER.cancel(task_id)
    if task is not None:
        return {"task_id": task.id, "status": "cancelled"}
    running = EVAL_SCHEDULER.snapshot().get("running")
    if running and running.get("job_id") == task_id:
        raise HTTPException(409, "任务已经开始运行，不能按排队任务取消")
    raise HTTPException(404, "排队任务不存在")


@app.patch("/api/queue/{job_id}/position")
async def api_queue_position(job_id: str, req: QueuePositionReq):
    """人工调整等待项位置；运行中的任务不被抢占。"""
    job_id = _validate_param_id(job_id, "job_id")
    position = EVAL_SCHEDULER.reprioritize(job_id, req.action)
    if position is not None:
        return {"job_id": job_id, "queue_position": position}
    running = EVAL_SCHEDULER.snapshot().get("running")
    if running and running.get("job_id") == job_id:
        raise HTTPException(409, "运行中的任务不能调整优先级")
    raise HTTPException(404, "等待任务不存在")


@app.post("/api/eval/{task_id}/pause", status_code=202)
async def api_pause(task_id: str):
    task = await get_task_async(_validate_param_id(task_id, "task_id"))
    if not task:
        raise HTTPException(404, "task not found")
    if task.status == "paused" and not task.execution_control.get("save_error"):
        return {"task_id": task.id, "status": "pausing" if task.active_runs else "paused"}
    if not task.active_runs and not task.execution_control.get("save_error"):
        raise HTTPException(409, "任务当前未在执行或排队")
    if not task.pause_requested:
        task.execution_control.update(state="pausing", requested_at=time.time())
    EVAL_SCHEDULER.pause(task)
    if not task.active_runs:
        await finish_pause(task)
    else:
        if not await wait_task_save(task, save=save_task):
            raise HTTPException(503, "暂停已请求，但保存失败；请保持服务运行并重试暂停")
        if task.pause_requested:
            task._fanout("pausing", {"message": "正在暂停，已停止新模型请求，等待已发送响应和资源清理后保存", "execution_control": task.execution_control})
    if task.execution_control.get("save_error"):
        raise HTTPException(503, task.error)
    return {"task_id": task.id, "status": task.execution_control["state"]}


@app.post("/api/eval/{task_id}/resume", status_code=202)
async def api_resume(task_id: str, req: ResumeReq):
    task = await get_task_async(_validate_param_id(task_id, "task_id"))
    if not task:
        raise HTTPException(404, "task not found")
    attempts = task.execution_control.setdefault("resumes", [])
    idem = req.idempotency_key.strip()
    for attempt in attempts:
        if idem and attempt.get("idempotency_key") == idem:
            return {"task_id": task.id, "resume_id": attempt["id"], "status": attempt["status"], "idempotent_replay": True}
    if task.active_runs or task.pause_requested or task.execution_control.get("save_error"):
        raise HTTPException(409, "任务仍在执行或保存，请等待暂停完成")
    if task.status not in {"paused", "error", "cancelled", "done"}:
        raise HTTPException(409, "当前任务状态不能恢复")
    _check_judge_options(task, {})
    indexes = resume_indexes(task, req.include_failed)
    if not indexes:
        raise HTTPException(409, "没有待执行的题目；如需重试失败项，请勾选同时重试失败项")
    app_cfg = cfg()
    attempt = {"id": f"resume_{uuid.uuid4().hex[:10]}", "idempotency_key": idem,
               "status": "queued", "created_at": time.time(), "indexes": indexes,
               "concurrency": req.concurrency, "include_failed": req.include_failed,
               "completed": 0, "failed": 0}
    attempts.append(attempt)
    task.execution_control.update(state="queued", concurrency=req.concurrency, pending_indexes=indexes.copy())
    _freeze_legacy_judge(task, app_cfg)
    position = EVAL_SCHEDULER.enqueue(task, app_cfg, run_resume)
    await wait_task_save(task, save=save_task)
    return {"task_id": task.id, "resume_id": attempt["id"], "status": "queued", "selected": len(indexes), "queue_position": position}


@app.post("/api/eval/{task_id}/retries", status_code=202)
async def api_retry_failed(task_id: str, req: RetryReq):
    """手动创建失败补跑，并作为独立 job 加入全局 FIFO 队列。"""
    task_id = _validate_param_id(task_id, "task_id")
    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if task.status != "done":
        raise HTTPException(409, "仅已完成任务可以发起失败补跑")
    _check_judge_options(task, req.options)
    idem = req.idempotency_key.strip()
    if idem:
        for old in task.retry_runs.values():
            if old.get("idempotency_key") == idem:
                return {
                    "task_id": task.id,
                    "retry_id": old.get("retry_id"),
                    "status": old.get("status"),
                    "selected": len(old.get("indexes") or []),
                    "queue_position": None,
                    "idempotent_replay": True,
                }
    if task.active_runs > 0:
        active = next(
            (row for row in task.retry_runs.values() if row.get("status") in {"queued", "running"}),
            None,
        )
        if active:
            raise HTTPException(409, f"已有失败补跑进行中：{active.get('retry_id')}")
        raise HTTPException(409, "任务仍有运行中的评测，暂不能补跑")

    latest = latest_results_by_index(task)
    requested = list(dict.fromkeys(req.indexes or range(len(task.items))))
    accepted: set[int] = set()
    reasons: dict[int, str] = {}
    skipped: list[dict] = []
    for index in requested:
        if index < 0 or index >= len(task.items):
            skipped.append({"index": index, "reason": "index_out_of_range"})
            continue
        result = latest.get(index)
        if result is None and req.include_unfinished:
            accepted.add(index)
            reasons[index] = "unfinished"
        elif result is not None and result.get("error"):
            accepted.add(index)
            reasons[index] = "failed"
        else:
            skipped.append({"index": index, "reason": "latest_result_is_success"})

    # 会话中某轮失败会影响后续上下文；从最早目标轮起成组补跑。
    groups: dict[str, list[int]] = {}
    for index, item in enumerate(task.items):
        if item.get("session_group") and task.mode != "compare":
            groups.setdefault(str(item["session_group"]), []).append(index)
    for indexes in groups.values():
        indexes.sort(key=lambda i: task.items[i].get("turn_index", 0))
        selected_positions = [pos for pos, idx in enumerate(indexes) if idx in accepted]
        if not selected_positions:
            continue
        first = min(selected_positions)
        earlier_failed = [
            pos for pos in range(first)
            if latest.get(indexes[pos]) is None or (latest.get(indexes[pos]) or {}).get("error")
        ]
        if earlier_failed:
            first = min(earlier_failed)
        for idx in indexes[first:]:
            if idx not in accepted:
                accepted.add(idx)
                reasons[idx] = "session_dependency"

    if not accepted:
        raise HTTPException(409, {"message": "没有可补跑的失败项", "skipped": skipped})

    retry_id = f"retry_{uuid.uuid4().hex[:10]}"
    accepted_indexes = sorted(accepted)
    retry = {
        "retry_id": retry_id,
        "idempotency_key": idem,
        "status": "queued",
        "created_at": time.time(),
        "indexes": accepted_indexes,
        "reasons": {str(index): reasons[index] for index in accepted_indexes},
        "options": req.options,
        "total": len(accepted_indexes),
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "items": {
            str(index): {"status": "queued", "reason": reasons[index]}
            for index in accepted_indexes
        },
    }
    task.retry_runs[retry_id] = retry
    _freeze_legacy_judge(task, cfg())
    task.execution_control["pending_indexes"] = accepted_indexes.copy()

    async def _retry_runner(parent, app_cfg):
        await run_retry(parent, app_cfg, retry_id)

    queue_position = EVAL_SCHEDULER.enqueue_retry(
        task,
        cfg(),
        _retry_runner,
        retry_id=retry_id,
        total=len(accepted_indexes),
    )
    return {
        "task_id": task.id,
        "retry_id": retry_id,
        "status": "queued",
        "selected": len(accepted_indexes),
        "accepted_indexes": accepted_indexes,
        "skipped": skipped,
        "queue_position": queue_position,
    }


@app.get("/api/eval/{task_id}/retries/{retry_id}")
async def api_retry_status(task_id: str, retry_id: str):
    task = await peek_task_async(_validate_param_id(task_id, "task_id"))
    if not task:
        raise HTTPException(404, "task not found")
    retry = task.retry_runs.get(_validate_param_id(retry_id, "retry_id"))
    if not retry:
        raise HTTPException(404, "retry not found")
    return retry


@app.post("/api/eval/items")
async def api_eval_items(req: EvalItemsReq):
    """向 task 批量更新/追加 items：按 id 匹配，命中原位替换、未命中追加末尾。

    task_id 不存在则用传入 id 新建任务（upsert）。本次全部 items 作为一个
    串行会话在后台评测（批次间并行、后完成者按 index 覆盖）；接口不流式，
    立即返回合并摘要，结果经 GET /api/eval/item/result 轮询获取。
    """
    task_id = _validate_param_id(req.task_id, "task_id")
    if not req.items:
        raise HTTPException(400, "items 为空")
    _validate_batch_item_ids(req.items)
    app_cfg = cfg()
    task = get_task(task_id)
    created = task is None
    if created:
        judge_runtime, judges = _new_judge_runtime(app_cfg, req.options)
    if task is not None and (task.pause_requested or task.status == "paused" or task.execution_control.get("state") in {"queued", "running"}):
        raise HTTPException(409, "任务正在暂停或恢复；请先完成该任务再更新 items")
    if task is not None:
        pending_updates = {i for batch in task.execution_control.get("update_batches", {}).values() for i in batch.get("remaining", [])}
        pending_ids = {task.items[i].get("id") for i in pending_updates if 0 <= i < len(task.items)}
        if any(item.get("id") in pending_ids for item in req.items):
            raise HTTPException(409, "这些题目仍有未完成的更新；请先完成或恢复原批次")
    if task is not None and task.repair_status in {"queued", "running"}:
        raise HTTPException(409, "任务正在失败补跑，暂不能同时更新 items")
    if task is not None and any(it.get("query_images") for it in [*task.items, *req.items]):
        if task.active_runs or task.status in {"pending", "queued", "running"}:
            raise HTTPException(409, "图文任务正在运行，请完成或取消后再替换题目")
    mode = req.mode if created else task.mode
    if created:
        if req.mode is None:
            raise HTTPException(422, "新建任务必须提供 mode")
    elif req.mode is not None and req.mode != task.mode:
        raise HTTPException(
            422, f"mode 与任务不一致：任务为 {task.mode}，请求为 {req.mode}"
        )
    # compare 模式校验只针对本次批次 items（命中替换的条目也全部重评），
    # 放在 new_task 之前，避免校验失败留下空任务。
    requested_profile = req.evaluation_profile
    if not created and mode == "compare":
        old_items = {item.get("id"): item for item in task.items}
        for item in req.items:
            old = old_items.get(item.get("id"), {})
            if old.get("query_images") and "query_images" not in item:
                raise HTTPException(422, "替换图文题必须显式提交 query_images；移除图片请传 []")
        if any(item.get("query_images") for item in req.items) and task.protocol_manifest.get("input_schema_version") not in {"1.1", "2.0"}:
            raise HTTPException(422, "旧任务实现不支持提问图片，请新建任务")
    if not created and requested_profile and requested_profile != task.evaluation_profile:
        raise HTTPException(
            422,
            "已有任务的评测协议不可变；请新建任务后使用另一版本评测",
        )
    if not created:
        _check_judge_options(task, req.options)
    protocol = (
        _compare_protocol_or_422(
            requested_profile if created else task.evaluation_profile
        )
        if mode == "compare"
        else None
    )
    validated_request = EvalReq(
        mode=mode,
        items=req.items,
        options=req.options,
        evaluation_profile=protocol.id if protocol else None,
    )
    previous_conversations = []
    if not created and mode == "compare" and any(is_conversation(it) for it in [*task.items, *req.items]):
        if any(key in req.options and req.options[key] != task.options.get(key) for key in ("product_names", "product_mapping")):
            raise HTTPException(422, "多轮产品映射不可修改；请新建任务")
        if task.active_runs or task.status in {"pending", "queued", "running"}:
            raise HTTPException(409, "多轮任务运行中不能修改或追加输入")
        existing = {it.get("id"): it for it in task.items}
        if any(it.get("id") in existing and (is_conversation(it) or is_conversation(existing[it["id"]])) for it in req.items):
            raise HTTPException(422, "多轮原始输入不可原地覆盖；修订后请新建任务")
        previous_conversations = [it.copy() for it in task.items]
    _validate_eval_request(validated_request, app_cfg, previous_items=previous_conversations)
    req.items = validated_request.items
    if created:
        task = new_task(
            mode,
            [],
            dict(req.options),
            dataset_name=req.dataset_name.strip(),
            task_id=task_id,
            evaluation_profile=protocol.id if protocol else "",
            protocol_manifest=(
                _protocol_manifest(protocol, app_cfg, req.options, judges) if protocol else {}
            ),
            judge_runtime=judge_runtime,
        )
    _freeze_legacy_judge(task, app_cfg)
    batch, replaced_ids, added_ids = merge_items_by_id(task, req.items)
    effective_options = {**task.options, **req.options}
    batch_id = uuid.uuid4().hex[:12]
    task.execution_control.setdefault("update_batches", {})[batch_id] = {
        "remaining": [index for index, _ in batch], "options": effective_options,
        "prior_summary": "", "created_at": time.time(),
    }
    task.active_runs += 1  # R1：提交时同步 pin（同 api_eval；run_update_batch 的 finally 负责解除）
    pending_save = queue_task_save(task, save=save_task)

    async def _start_later():
        # 先把合并结果响应出去，再启动可能较重的评测（与 /api/eval 同模式）。
        await asyncio.sleep(0.05)
        await run_update_batch(
            task,
            app_cfg,
            batch,
            options=effective_options,
            manage_status=created,
            batch_id=batch_id,
        )

    spawn_background(_start_later())
    if pending_save is not None:
        await asyncio.shield(pending_save)
    return {
        "task_id": task.id,
        "created": created,
        "replaced_ids": replaced_ids,
        "added_ids": added_ids,
        "total_items": len(task.items),
        "evaluation_profile": task.evaluation_profile,
    }


@app.get("/api/eval/item/result")
async def api_item_result(task_id: str, item_id: str):
    """查询单条 item 的当前结果：重评中返回既有旧结果并带 evaluating 标志。

    result 与 xlsx/CSV「逐题结果」同名列、同转换；多余字段不返回，
    失败结果额外附 error。
    """
    # R6：原同步 def 在线程池执行，get_task 的注册/容量迭代与事件循环线程
    # 并发变异共享 OrderedDict；改异步后 TASKS 变异统一在循环线程。
    task = await get_task_async(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    idx = next(
        (i for i, it in enumerate(task.items) if it.get("id") == item_id), None
    )
    if idx is None:
        raise HTTPException(404, "item not found")
    evaluating = idx in task.in_flight_indexes
    # 从后往前取该 index 最新一条（历史可能存在同 index 重复条目）
    raw = next(
        (r for r in reversed(task.results) if r.get("index") == idx), None
    )
    if evaluating:
        status = "evaluating"
    elif raw is None:
        status = "pending"
    elif raw.get("error"):
        status = "failed"
    else:
        status = "done"
    return {
        "task_id": task.id,
        "item_id": item_id,
        "index": idx,
        "item": task.items[idx],
        "result": (
            result_export_row(task.mode, raw, idx, task.items)
            if raw is not None
            else None
        ),
        "evaluating": evaluating,
        "status": status,
    }


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 模块常量便于测试 monkeypatch
_UPLOAD_CHUNK = 1024 * 1024


@app.post("/api/upload/query-image")
async def api_upload_query_image(file: UploadFile = File(...)):
    policy = cfg().visual_modes["rich_content"].query_images
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(422, "仅支持静态 PNG/JPEG/WebP")
    upload_dir = RUNS_DIR / "query_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / (uuid.uuid4().hex + suffix)
    try:
        size = 0
        with path.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > policy.max_file_bytes:
                    raise HTTPException(413, "提问图片上传大小超限")
                dest.write(chunk)
        prepared = await run_preparation(
            prepare_query_images, {"query": "上传预览", "query_images": [str(path)]},
            session_name="uploads", cfg=policy, timeout=60,
        )
        meta = prepared["query_image_meta"][0]
        return {"path": meta["original_path"], **meta}
    except QueryImageError as exc:
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc
    finally:
        path.unlink(missing_ok=True)
        await file.close()


@app.get("/api/query-images/{image_id}")
def api_query_image(image_id: str, original: bool = False):
    if len(image_id) != 32 or any(c not in "0123456789abcdef" for c in image_id):
        raise HTTPException(404, "图片不存在")
    root = (RUNS_DIR / "query_images").resolve()
    registry = root / "registry" / f"{image_id}.json"
    if not registry.is_file():
        raise HTTPException(404, "图片不存在")
    meta = json.loads(registry.read_text(encoding="utf-8"))
    path = Path(meta["original_path" if original else "path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "图片不可用")
    return FileResponse(path, filename=path.name if original else None)


@app.post("/api/upload/video")
async def api_upload_video(file: UploadFile = File(...)):
    """上传视觉评估录屏；延迟到开始评估时使用 rich_content 专用参数抽帧。

    分块流式落盘并边写边计数，超限即刻中断——避免整个文件先读入内存
    （旧实现在 read() 之后才检查大小，20MB 限制对大文件形同虚设）。
    """
    video_dir = RUNS_DIR / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "v.mp4").suffix.lower() or ".mp4"
    video_path = video_dir / f"{video_id}{suffix}"
    written = 0
    try:
        with video_path.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "视频过大，限制 ≤20MB")
                out.write(chunk)
    except Exception:
        video_path.unlink(missing_ok=True)  # 清理半截文件（413 与 IO 异常 alike）
        raise
    finally:
        await file.close()
    duration = probe_duration(video_path)
    return {
        "video_id": video_id,
        "video_path": str(video_path),
        "frames": [],
        "frame_count": 0,
        "duration": round(duration, 2),
    }


@app.get("/api/eval/{task_id}/stream")
async def api_stream(task_id: str, compact: bool = False):
    # 只读视图：终态任务回放完即返回不驻留；运行中任务 peek 命中活对象，订阅正常
    task = peek_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")

    async def event_gen():
        # 先订阅再回放：回放期间新产生的事件进入本连接队列，断线重连/中途
        # 打开都不丢增量（可能与回放重复推送一次，前端按 index 覆盖渲染，
        # 幂等）。旧实现的共享单队列还有多连接互相瓜分事件的正确性问题。
        q = task.subscribe()
        try:
            if compact:
                # 新页面按题回放日志，避免每条历史日志触发一次页面渲染。
                # 首次 yield 前固定全部快照；回放期间的新事件由 q 按序补齐。
                histories = [(key, list(events)) for key, events in task.progress_events.items()]
                yield _sse("replay_state", {
                    "status": task.status,
                    "results": list(task.results),
                    "item_progress": snapshot_item_progress(task),
                    "progress": task.done_total,
                    "total": len(task.items),
                    "repair_status": task.repair_status,
                    "execution_control": task.execution_control,
                    "active_runs": task.active_runs,
                    "task_timing": task.timing_snapshot(),
                    "retry": max(task.retry_runs.values(), key=lambda row: float(row.get("created_at") or 0), default=None),
                })
                for key, events in histories:
                    yield _sse("progress_history", {"item_index": int(key), "events": events})
            # 回放有界事件历史，供 Web 展示与文件日志同源的逐行调用记录。
            # 内层列表同样快照（R8）：yield 挂起期间 _record_progress 会并发
            # append/del 同一列表，遍历活列表会跳帧/重帧。
            for item_events in ([] if compact else list(task.progress_events.values())):
                for progress_event in list(item_events):
                    yield _sse("progress_event", progress_event)
            # 回放每题最新进度，断线重连后能立即恢复当前阶段。
            for progress_item in ([] if compact else list(snapshot_item_progress(task).values())):
                yield _sse("item_progress", progress_item)
            # 先回放已有结果（断线重连不丢已完成的）
            for index, item in enumerate(task.items):
                if item.get("session_id") and item.get("input_diagnostic_status"):
                    yield _sse("input_diagnostics", {"item_index": index,
                        **{key: item[key] for key in DIAGNOSTIC_FIELDS if key in item}})
            for r in ([] if compact else list(task.results)):
                yield _sse("result", {"progress": task.done_total, "total": len(task.items), "result": r})
            # 终态判定叠加 active_runs（R4）：更新批 manage_status=False 全程
            # status=done，仅看 status 会在批运行中立即下发伪 done；批结束时
            # 由 run_update_batch 补发终态事件驱动下方实时循环退出。
            if task.status == "done" and task.active_runs <= 0:
                payload = {"summary": task.summary, "total": len(task.items), "task_timing": task.timing_snapshot()}
                if task.retry_runs and task.repair_status != "idle":
                    payload["retry"] = max(
                        task.retry_runs.values(),
                        key=lambda row: float(row.get("created_at") or 0),
                    )
                yield _sse("done", payload)
                return
            if task.status == "error" and task.active_runs <= 0:
                yield _sse("error", {"message": task.error, "task_timing": task.timing_snapshot()})
                return
            if task.status == "cancelled" and task.active_runs <= 0:
                yield _sse("cancelled", {"message": "排队任务已取消", "task_timing": task.timing_snapshot()})
                return
            if task.status == "paused" and task.active_runs <= 0:
                yield _sse("paused", {"status": "paused", "summary": task.summary, "execution_control": task.execution_control, "task_timing": task.timing_snapshot()})
                return
            # 实时跟进
            while True:
                msg = await q.get()
                yield _sse(msg["event"], msg["data"])
                if msg["event"] in ("done", "error", "cancelled", "retry_cancelled", "paused"):
                    break
        finally:
            task.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


class DatasetMediaReq(BaseModel):
    path: str
    role: Literal["query", "screenshot", "video"]
    expected_sha256: str = ""


class DatasetFileReq(DatasetMediaReq):
    index: int
    label: str = ""


class DatasetValidateReq(BaseModel):
    files: list[DatasetFileReq] = Field(default_factory=list, max_length=40000)


@app.get("/api/datasets")
async def api_datasets(page: int = 1, search: str = ""):
    rows = await asyncio.to_thread(list_snapshots, limit=None)
    needle = search.strip().casefold()
    rows = [row for row in rows if row.get("mode") == "compare" and row.get("total", 0) > 0
            and (not needle or needle in " ".join(str(row.get(k) or "") for k in
                                                 ("dataset_name", "note", "task_id")).casefold())]
    total = len(rows)
    page = min(max(1, page), max(1, (total + 9) // 10))
    return {"items": rows[(page - 1) * 10:page * 10], "total": total, "page": page, "page_size": 10}


@app.get("/api/datasets/{task_id}")
async def api_dataset(task_id: str):
    task = await peek_task_async(_validate_param_id(task_id, "task_id"))
    if task is None:
        raise HTTPException(404, "历史数据不存在或已删除")
    if task.mode != "compare":
        raise HTTPException(422, "仅支持复用垂域视觉对比数据")
    fields = {"id", "query", "question", "context", "category", "product_count", "evidence_mode",
              "query_images", "query_image_meta", "source_data", "source_line", "session_group", "turn_index",
              "task_start_time", "task_end_time", "session_id", "screenshot_scope", "conversation_mode"}
    fields.update(f"screenshot_scope{n}" for n in (1, 2, 3))
    fields.update(f"{name}{n}" for name in ("video", "screenshot", "answer", "context", "screenshot_meta", "video_source")
                  for n in range(1, 4))
    return {"task_id": task.id, "dataset_name": task.dataset_name, "created_at": task.created_at,
            "note": task.note, "items": copy.deepcopy([
                {key: value for key, value in item.items() if key in fields} for item in task.items
            ])}


@app.post("/api/dataset-files/validate")
def api_validate_dataset_files(req: DatasetValidateReq):
    issues = []
    for entry in req.files:
        try:
            DATASET_MEDIA.resolve(entry.path, entry.role, cfg().visual_modes["rich_content"].query_images, BASE_DIR)
        except (ValueError, OSError) as exc:
            issues.append({"index": entry.index, "label": entry.label, "message": str(exc)})
    return {"checked": len(req.files), "issues": issues}


@app.post("/api/dataset-media")
def api_dataset_media(req: DatasetMediaReq):
    try:
        return DATASET_MEDIA.register(req.path, req.role, cfg().visual_modes["rich_content"].query_images,
                                      BASE_DIR, req.expected_sha256)
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/dataset-media/{token}")
def api_dataset_media_file(token: str, download: bool = False):
    try:
        path, mime = DATASET_MEDIA.file(token, cfg().visual_modes["rich_content"].query_images, BASE_DIR)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=mime, filename=path.name if download else None,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/history")
async def api_history(limit: int = 50, page: int | None = None):
    # Keep the legacy limit-only API; paginated callers can reach all history.
    rows = await asyncio.to_thread(list_snapshots, limit=limit if page is None else None)
    pagination = {}
    if page is not None:
        total = len(rows)
        page_size = 10
        page = min(max(1, page), max(1, (total + page_size - 1) // page_size))
        rows = rows[(page - 1) * page_size:page * page_size]
        pagination = {"total": total, "page": page, "page_size": page_size}
    # 磁盘历史会把 pending/running/queued 视为上次服务中断；当前进程中的活
    # 对象需覆盖回来，避免历史列表把正在排队或运行的任务误显示为 error。
    for row in rows:
        task = TASKS.get(row.get("task_id"))
        if task is not None:
            row["task_timing"] = task.timing_snapshot()
        if task is None or task.active_runs <= 0:
            continue
        row["status"] = task.status
        row["error"] = task.error
        row["repair_status"] = task.repair_status
        row["done"] = task.done_total
        row["total"] = len(task.items)
    return {"items": rows, **pagination}


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: str):
    # peek 不注册：浏览历史不再把整份快照读进内存永久驻留（旧的 get_task
    # miss 路径会注册驻留，翻 N 个历史内存就涨 N 份快照）。
    # touch=False（R6）：同步 def 在线程池执行，跳过 move_to_end 以保证
    # 完全不变异 TASKS（纯 dict 读在 GIL 下安全）。
    task = peek_task(task_id, touch=False)
    if not task:
        raise HTTPException(404, "task not found")
    snapshot = task_to_snapshot(task)
    snapshot["item_progress"] = snapshot_item_progress(task)
    snapshot["active_runs"] = task.active_runs
    return snapshot_payload(snapshot)


@app.delete("/api/history/{task_id}")
async def api_history_delete(task_id: str):
    if task_save_pending(task_id):
        raise HTTPException(409, "任务正在保存，请稍后再删除")
    # R6：TASKS 增删必须发生在事件循环线程（原同步 def 在线程池执行，与
    # move_to_end/_enforce_capacity 迭代竞争触发 RuntimeError）。全程无
    # await：检查-删盘-清内存相对提交端点的同步 pin（同样无 await 前置）
    # 是原子的，堵住删除与运行竞态；两个 unlink 的阻塞可忽略。
    task = TASKS.get(task_id)
    if task and (task.active_runs > 0 or task.status in {"pending", "running"}):
        raise HTTPException(409, "任务运行中，请等待完成后再删除")
    if not delete_snapshot(task_id):
        raise HTTPException(404, "task not found")
    if task is not None:
        TASKS.pop(task_id, None)  # 删除历史同步清内存副本（旧行为只删盘不删内存）
    return {"ok": True}


@app.patch("/api/history/{task_id}/note")
async def api_history_note(task_id: str, req: HistoryNoteReq):
    # peek 先查注册表：运行中返回活对象，改 note 落盘不丢并发结果；
    # 终态任务用临时对象改写，不驻留。R6：异步视图 + 落盘放 to_thread，
    # TASKS 触碰留在循环线程。
    task = await peek_task_async(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    note = req.note.strip()
    if len(note) > 1000:
        raise HTTPException(422, "备注不能超过 1000 个字符")
    task.note = note
    if not await wait_task_save(task, save=save_task):
        raise HTTPException(500, "备注保存失败")
    return {"ok": True, "task_id": task.id, "note": task.note}


@app.post("/api/eval/{task_id}/exports", status_code=202)
async def api_prepare_xlsx(task_id: str, request: Request):
    return XLSX_EXPORTS.create(task_id, base_url=str(request.base_url))


class ComparisonExportReq(BaseModel):
    task_ids: list[str] = Field(min_length=2, max_length=2)


@app.post("/api/exports/comparison", status_code=202)
async def api_prepare_comparison_xlsx(req: ComparisonExportReq, request: Request):
    if any(not task_id.strip() for task_id in req.task_ids):
        raise HTTPException(422, "任务ID不能为空")
    if req.task_ids[0] == req.task_ids[1]:
        raise HTTPException(422, "请选择两个不同的任务")
    return XLSX_EXPORTS.create(req.task_ids[0], req.task_ids[1], base_url=str(request.base_url))


@app.get("/api/exports/{export_id}")
async def api_xlsx_status(export_id: str):
    return XLSX_EXPORTS.view(export_id)


@app.get("/api/exports/{export_id}/download")
async def api_xlsx_download(export_id: str):
    state = XLSX_EXPORTS.view(export_id)
    if state["status"] != "ready":
        raise HTTPException(409, state.get("error") or "Excel 尚未生成完成")
    return FileResponse(
        XLSX_EXPORTS.jobs[export_id]["path"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=state["filename"],
    )


@app.get("/api/eval/{task_id}/export")
async def api_export(task_id: str, format: str = "json", request: Request = None):
    task = await peek_task_async(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    data = copy.deepcopy(task_to_snapshot(task))
    data["export_base_url"] = str(request.base_url) if request else ""
    return await asyncio.to_thread(_export_snapshot, task_id, format, data)


def _export_snapshot(task_id: str, format: str, data: dict):

    if format == "json":
        return JSONResponse(snapshot_payload(data))

    if format == "screenshot_evidence":
        return _screenshot_evidence_zip(task_id, data)

    if format == "xlsx":
        # 保留原 GET 下载入口，改为文件响应；新页面使用有状态的后台导出。
        export_dir = RUNS_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / f".xlsx-{uuid.uuid4().hex}.xlsx"
        try:
            write_xlsx(data, archive_path)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return FileResponse(
            archive_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=xlsx_download_name(data.get("dataset_name", ""), task_id),
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    if format in {"frames", "frames_zip"}:
        export_dir = RUNS_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / f".{task_id}.{uuid.uuid4().hex}.zip"
        write_frames_zip(data, archive_path)
        raw_name = Path(str(data.get("dataset_name") or f"eval_{task_id}")).stem
        safe_name = "".join(
            char if char.isalnum() or char in "-_. " else "_"
            for char in raw_name
        ).strip(" ._") or f"eval_{task_id}"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{safe_name}_frames.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    sheets = export_rows(data)
    csv_text = rows_to_csv(sheets.get("逐题结果") or [])
    return StreamingResponse(
        iter([csv_text.encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=eval_{task_id}.csv"},
    )


def _download_stem(value: str, fallback: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_. " else "_"
        for char in value
    ).strip(" ._")
    return safe[:120] or fallback


@app.get("/api/eval/{task_id}/items/{item_index}/export")
def api_export_item(task_id: str, item_index: int, format: str):
    """导出单条结果关联的原视频、关键帧或裁判调用 JSON。"""
    task = peek_task(task_id, touch=False)  # 导出只读视图不驻留内存；touch=False：线程池端点不变异 TASKS
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    items = data.get("items") or []
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(404, "item not found")
    item = items[item_index]
    if format == "screenshot_evidence":
        return _screenshot_evidence_zip(task_id, data, {item_index})
    raw_id = str(item.get("id") or f"q{item_index + 1}")
    stem = _download_stem(
        f"{item_index + 1:03d}_{raw_id}",
        f"{item_index + 1:03d}_item",
    )

    if format == "video":
        raw_path = str(item.get("video_path") or "").strip()
        if not raw_path:
            media = item.get("media") or []
            raw_path = str(media[0]).strip() if media else ""
        if not raw_path:
            raise HTTPException(404, "该条结果没有原始视频路径")
        try:
            video_path = _resolve_operation_video_path(raw_path)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        source_identity = item.get("video_source") or item.get("video_source1") or {}
        if source_identity.get("sha256") and file_hash(video_path) != source_identity["sha256"]:
            raise HTTPException(409, "原视频内容已变更，与评测时采样帧不一致，请恢复原视频或重新评测")
        return FileResponse(
            video_path,
            filename=f"{stem}{video_path.suffix.lower()}",
        )

    if format in {"frames", "frames_zip"}:
        export_dir = RUNS_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_path = export_dir / (
            f".{task_id}.{item_index}.{uuid.uuid4().hex}.zip"
        )
        write_frames_zip(data, archive_path, item_indexes={item_index})
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{stem}_frames.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    if format in {"judge", "judge_calls"}:
        payload = load_item_judge_calls(data, item_index)
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    f"{quote(f'{stem}_judge_calls.json')}"
                ),
            },
        )

    raise HTTPException(400, "format 必须是 video、frames_zip 或 judge_calls")


@app.get("/api/eval/{task_id}/items/{item_index}/screenshots/{product_no}")
def api_item_screenshot(task_id: str, item_index: int, product_no: int, download: bool = False):
    """Serve only the original screenshot bound to this task/product, never a slice."""
    from PIL import Image

    if product_no not in (1, 2, 3):
        raise HTTPException(404, "产品不存在")
    task = peek_task(task_id, touch=False)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data or not 0 <= item_index < len(data.get("items") or []):
        raise HTTPException(404, "题目不存在")
    item = data["items"][item_index]
    source = item.get("source_data") or {}
    meta = item.get(f"screenshot_meta{product_no}") or {}
    raw = meta.get("original_path") or item.get(f"screenshot{product_no}") or source.get(f"screenshot{product_no}")
    count = item.get("product_count") or (3 if item.get("screenshot3") or source.get("screenshot3") else 2)
    if not raw or product_no > count:
        raise HTTPException(404, "该产品没有回答长截图")
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else BASE_DIR / path).resolve()
    if not any(path.is_relative_to(root) for root in operation_video_roots(BASE_DIR)) or not path.is_file():
        raise HTTPException(404, "长截图不存在或不在授权目录")
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("不支持的长截图格式")
            mime = Image.MIME[image.format]
            image.verify()
        if meta.get("original_sha256"):
            with path.open("rb") as file:
                digest = hashlib.sha256()
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != meta["original_sha256"]:
                raise HTTPException(409, "原始长截图已变化，无法展示本次评测原图")
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "长截图无法读取") from exc
    stem = _download_stem(str(item.get("id") or item_index + 1), "item")
    return FileResponse(path, media_type=mime,
                        filename=f"{stem}_product{product_no}_original{path.suffix.lower()}" if download else None,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _screenshot_evidence_zip(task_id: str, data: dict, indexes: set[int] | None = None):
    from .screenshot_evidence import write_evidence_zip
    directory = RUNS_DIR / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"evidence-{uuid.uuid4().hex}.zip"
    try:
        write_evidence_zip(data, path, indexes)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/zip",
                        filename=f"{_download_stem(task_id, 'task')}_screenshot_evidence.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


def _screenshot_record(task_id: str, item_index: int):
    from .screenshot_evidence import evidence_records
    task = peek_task(task_id, touch=False)
    data = task_to_snapshot(task) if task else load_snapshot(task_id)
    if not data:
        raise HTTPException(404, "任务不存在")
    records = evidence_records(data, {item_index})
    if not records:
        raise HTTPException(404, "该题没有长截图证据")
    return records[0]


@app.get("/api/eval/{task_id}/items/{item_index}/screenshot-evidence")
def api_screenshot_evidence(task_id: str, item_index: int):
    return _screenshot_record(task_id, item_index)


@app.get("/api/eval/{task_id}/items/{item_index}/screenshot-evidence/{image_no}")
def api_screenshot_evidence_image(task_id: str, item_index: int, image_no: int):
    from .screenshot_evidence import evidence_bytes
    from fastapi.responses import Response
    record = _screenshot_record(task_id, item_index)
    image = next((im for im in record["images"] if im["image_no"] == image_no), None)
    if image is None:
        raise HTTPException(404, "证据图片不存在")
    try:
        raw, mime = evidence_bytes(image)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(raw, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8054)
