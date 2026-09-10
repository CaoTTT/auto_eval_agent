"""评估执行：垂域视觉评测 / 垂域视觉对比评测 + 并发 + 推 SSE 事件 + 汇总。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from ..paths import RUNS_DIR
from ..config import AppConfig
from ..judges import (
    JudgeClient,
    RichContentJudge,
    VisualCompareJudge,
)
from ..judges.base import flush_web_trace_records
from ..judges.compare_protocols import resolve_compare_protocol
from ..llm_stream import is_retriable_llm_error
from ..long_screenshot import ContextBudgetExceeded, RISKY_REVIEW_REASON
from ..observability import (
    bind_chain_context,
    error_details,
    log_event,
    make_request_id,
)
from ..schema import EvalItem
from .history import save_task
from .video_prepare import (
    prepare_session_rich_content_item,
    prepare_session_visual_compare_item,
    prepare_session_long_screenshot_item,
)
from .tasks import (
    Task,
    latest_results_by_index,
    retire_task,
    upsert_result_by_index,
)


logger = logging.getLogger(__name__)
MAX_PROGRESS_EVENTS_PER_ITEM = 100
COMPARE_DIMENSIONS = (
    "understanding",
    "accuracy",
    "service_closure",
    "scenario_fulfillment",
    "intuitive_efficiency",
    "evidence_quality",
    "guided_recommendation",
)
COMPARE_AGGREGATION_DIMENSIONS = tuple(
    dimension for dimension in COMPARE_DIMENSIONS if dimension != "accuracy"
)


# 持久化节流：普通调用走 debounce（默认 2s，环境变量可调）或每 N 题强刷一次，
# 避免大任务每完成一题就把 items+全部 results+progress_events 全量 json.dumps
# （O(n²) 序列化、瞬时内存峰值约快照大小的 2-3 倍）。force=True 立即落盘，
# 用于终态/异常/退休前。语义变化：进程崩溃最多丢 debounce 窗口内的结果；
# 任务正常终态保证盘上完整（eval_errors.jsonl 与 judge trace 仍即时写）。
_PERSIST_DEBOUNCE_S = float(os.environ.get("AUTO_EVAL_PERSIST_DEBOUNCE_S", "2.0"))
_PERSIST_FORCE_EVERY_N = 20
_pending_flush: dict[str, asyncio.TimerHandle] = {}
_unpersisted: dict[str, int] = {}


def _flush_now(task: Task) -> None:
    """立即落盘：取消 pending 定时器、重算 summary、save_task。"""
    handle = _pending_flush.pop(task.id, None)
    if handle is not None:
        handle.cancel()
    _unpersisted.pop(task.id, None)
    task.summary = _summarize(task)
    try:
        save_task(task)
    except Exception:
        logger.exception("unexpected task snapshot failure: task_id=%s", task.id)


def _persist_task(task: Task, *, force: bool = False) -> None:
    """Persist without allowing history I/O to break the evaluation/SSE.

    TimerHandle 闭包直接持 task 引用（不按 id 回查 TASKS）：退休前必先
    force 强刷，届时 pending 定时器已被取消，不存在退休后再刷盘的窗口。
    """
    if force:
        _flush_now(task)
        return
    n = _unpersisted.get(task.id, 0) + 1
    _unpersisted[task.id] = n
    if task.id in _pending_flush:
        return  # 已有定时刷在排队，等它
    if n >= _PERSIST_FORCE_EVERY_N:
        _flush_now(task)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _flush_now(task)  # 无事件循环的同步上下文，直接刷
        return
    _pending_flush[task.id] = loop.call_later(
        _PERSIST_DEBOUNCE_S,
        lambda t=task: _flush_now(t),
    )


def _record_progress(task: Task, item_index: int, payload: dict) -> dict:
    """Store one bounded Web projection of the same structured log event."""
    key = str(item_index)
    events = task.progress_events.setdefault(key, [])
    sequence = int(events[-1].get("sequence", 0)) + 1 if events else 1
    event_payload = {**payload, "sequence": sequence}
    previous = task.item_progress.get(key) or {}
    if (
        "started_at" not in event_payload
        and previous.get("started_at") is not None
        and event_payload.get("request_id") == previous.get("request_id")
    ):
        event_payload["started_at"] = previous["started_at"]
    events.append(event_payload)
    if len(events) > MAX_PROGRESS_EVENTS_PER_ITEM:
        del events[:-MAX_PROGRESS_EVENTS_PER_ITEM]
    task.item_progress[key] = event_payload
    task._fanout("item_progress", event_payload)
    return event_payload


def _to_evalitem(item: dict, idx: int) -> EvalItem:
    meta = dict(item.get("metadata") or {})
    if item.get("frames"):
        meta["frames"] = item["frames"]  # 抽好的关键帧路径，裁判读取后 encode 成 image_url
    return EvalItem(
        id=item.get("id", f"q{idx}"),
        question=item["query"],
        context=item.get("context"),
        category=item.get("category", "default"),
        media=item.get("media") or [],
        metadata=meta,
    )


def _compare_product_count(item: dict) -> int:
    declared = item.get("product_count")
    if declared in (2, 3) and not isinstance(declared, bool):
        return declared
    return 3 if any(
        item.get(field) not in (None, "", [])
        for field in ("video3", "screenshot3", "frames3", "context3", "answer3")
    ) else 2


def _compare_frames_ready(item: dict) -> bool:
    return all(
        bool(item.get(f"frames{product_no}"))
        for product_no in range(1, _compare_product_count(item) + 1)
    )


def _mark_interrupted_if_stuck(task: Task) -> bool:
    """R2：取消/未捕获 BaseException 会跳过 `except Exception`，任务可能停在
    pending/running——retire/_enforce_capacity 均拒绝非终态，Task 将永久 pin
    （不可删、DELETE 永久 409）。置 error 并通知订阅者后返回 True。"""
    if task.status not in {"pending", "running"}:
        return False
    task.status = "error"
    task.error = task.error or "服务中断，已保留中断前完成的评估结果"
    task._fanout("error", {"message": task.error})
    return True


async def run_eval(task: Task, cfg: AppConfig) -> None:
    """全量评测入口。pin 契约（R1）：调用方须在提交时同步 `active_runs += 1`
    （endpoint 在 spawn_background 之前），本函数只负责结束时解除——否则
    spawn 延迟窗口内任务可被 DELETE/LRU 淘汰，引发快照复活或双对象覆盖。"""
    try:
        await task.publish("start", {"total": len(task.items), "mode": task.mode})
        task.status = "running"
        _persist_task(task, force=True)
        try:
            await _run(task, cfg)
            task.summary = _summarize(task)
            task.status = "done"
            await task.publish("done", {"summary": task.summary, "total": len(task.items)})
            _persist_task(task, force=True)
        except Exception as e:
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            await task.publish("error", {"message": task.error})
            _persist_task(task, force=True)
    finally:
        task.active_runs -= 1
        _mark_interrupted_if_stuck(task)
        _persist_task(task, force=True)  # 退休前最后一次落盘，磁盘先于内存下线
        retire_task(task)


def _make_item_evaluator(
    task: Task,
    cfg: AppConfig,
    *,
    options: dict | None = None,
    on_result: Callable[[int, dict, float], Awaitable[None]] | None = None,
) -> tuple[Callable[[int, dict], Awaitable[dict]], list[JudgeClient]]:
    """构造单题评测协程 one(idx, item_dict) -> res（含失败 res，不抛出），
    以及本套裁判客户端（调用方负责在 finally 中 aclose，见 _aclose_judge_clients）。

    options：本次运行生效的配置（缺省 task.options），只读、不回写，
    供更新批以 {**task.options, **req.options} 运行。
    on_result：结果落地回调 (idx, res, started)，在评测上下文（bind_chain_context
    内）被 await，负责 append/merge、完成日志、SSE、持久化；缺省为完整跑批的原有行为。
    """
    runtime_options = options if options is not None else task.options
    compare_protocol = (
        resolve_compare_protocol(
            task.evaluation_profile or runtime_options.get("evaluation_profile")
        )
        if task.mode == "compare"
        else None
    )
    selected = runtime_options.get("judges") or [cfg.judges[0].name]
    judges_cfg = [j for j in cfg.judges if j.name in selected] or cfg.judges[:1]
    # R3：构造中途失败（如某个 judge 缺 base_url）时，已建客户端的连接池会
    # 无人关闭而泄漏——先登记再逐个构造，失败时交后台任务关闭后重抛。
    clients: list[JudgeClient] = []
    try:
        for j in judges_cfg:
            clients.append(JudgeClient(j))
    except BaseException:
        if clients:
            spawn_background(_aclose_judge_clients(clients))
        raise
    rich_profile = cfg.visual_modes.get("rich_content")
    rich_judges = (
        [RichContentJudge(client, rich_profile) for client in clients]
        if rich_profile is not None
        else []
    )
    # 垂域视觉对比：双视频多模态对比裁判（复用同一视觉配置）
    compare_judges = (
        [VisualCompareJudge(client, rich_profile, compare_protocol) for client in clients]
        if rich_profile is not None
        else []
    )
    # 垂域→中文显示名映射（rich_content.yaml 的 category_display）
    category_display = rich_profile.category_display if rich_profile else {}
    sem = asyncio.Semaphore(int(runtime_options.get("concurrency", 4)))
    eval_timeout = float(runtime_options.get("eval_timeout_s") or runtime_options.get("eval_timeout") or 300.0)
    loop = asyncio.get_running_loop()

    async def _default_on_result(idx: int, res: dict, started: float) -> None:
        task.results.append(res)
        task.done_total += 1
        failed = bool(res.get("error"))
        log_event(
            "任务",
            "完成",
            level=logging.ERROR if failed else logging.INFO,
            details={
                "状态": "失败" if failed else "成功",
                "总耗时": f"{time.perf_counter() - started:.2f}秒",
                "错误": res.get("error"),
            },
            progress=100,
            progress_message="评测失败" if failed else "评测完成",
            progress_status="error" if failed else "done",
        )
        await task.publish(
            "result",
            {"progress": task.done_total, "total": len(task.items), "result": res},
        )
        _persist_task(task)

    finish = on_result or _default_on_result

    async def one(idx: int, item_dict: dict) -> dict:
        request_id = make_request_id(task.created_at, task.id, idx)
        retry_id = str(runtime_options.get("_retry_id") or "")
        if retry_id:
            request_id = f"{request_id}_r{retry_id[-6:]}"
        pending_judge_traces: list[tuple[str, dict]] = []

        def publish_progress(payload: dict) -> None:
            def apply() -> None:
                _record_progress(task, idx, payload)
            try:
                if asyncio.get_running_loop() is loop:
                    apply()
                else:
                    loop.call_soon_threadsafe(apply)
            except RuntimeError:
                loop.call_soon_threadsafe(apply)

        item_id = item_dict.get("id") or f"q{idx}"

        def collect_judge_trace(trace_path: str, record: dict) -> None:
            pending_judge_traces.append((trace_path, record))

        with bind_chain_context(
            task_id=task.id,
            session_name=task.session_name,
            request_id=request_id,
            item_id=item_id,
            item_index=idx,
            progress_callback=publish_progress,
            judge_trace_callback=collect_judge_trace,
        ):
            log_event(
                "任务",
                "开始",
                details={
                    "问题": item_dict.get("query", ""),
                    "模式": task.mode,
                    "裁判": ",".join(j.display or j.name for j in judges_cfg),
                },
                progress=0,
                progress_message="排队等待评测",
            )
            async with sem:
                # 排队时间不计入单题耗时；取得并发槽后才启动计时。
                started = time.perf_counter()
                log_event(
                    "任务",
                    "开始评测",
                    progress=1,
                    progress_message="开始评测",
                    progress_fields={"started_at": int(time.time() * 1000)},
                )
                last_error = None
                res = None
                is_screenshot = task.mode == "compare" and (
                    item_dict.get("evidence_mode") == "long_screenshot" or bool(item_dict.get("screenshot1"))
                )
                needs_visual_prepare = is_screenshot or (
                    not _compare_frames_ready(item_dict)
                    if task.mode == "compare"
                    else not item_dict.get("frames")
                )
                if needs_visual_prepare:
                    try:
                        log_event(
                            "视觉证据准备" if is_screenshot else "视频准备",
                            "校验长截图并选择切片边界" if is_screenshot else "校验视频并分析场景",
                            details={
                                "视觉证据路径": (
                                    [
                                        item_dict.get(f"{'screenshot' if is_screenshot else 'video'}{product_no}")
                                        for product_no in range(
                                            1, _compare_product_count(item_dict) + 1
                                        )
                                    ]
                                    if task.mode == "compare"
                                    else item_dict.get("video_path")
                                )
                            },
                            progress=3,
                            progress_message="正在准备长截图视觉证据" if is_screenshot else "正在校验视频并分析场景",
                        )
                        if rich_profile is None:
                            raise ValueError("缺少 rich_content 视觉模式配置")
                        prepare_call = (
                            prepare_session_long_screenshot_item if is_screenshot else prepare_session_visual_compare_item
                            if task.mode == "compare"
                            else prepare_session_rich_content_item
                        )
                        prepared = await asyncio.wait_for(
                            asyncio.to_thread(
                                prepare_call,
                                item_dict,
                                session_name=task.session_name,
                                item_index=idx,
                                total_items=len(task.items),
                                profile=rich_profile,
                            ),
                            timeout=float(runtime_options.get("video_prepare_timeout_s") or 300),
                        )
                        item_dict.clear()
                        item_dict.update(prepared)
                        _persist_task(task)
                        _frame_dir = ""
                        if item_dict.get("frames"):
                            _frame_dir = str(Path(item_dict["frames"][0]).parent)
                        elif item_dict.get("frames1"):
                            _frame_dir = str(Path(item_dict["frames1"][0]).parent)
                        log_event(
                            "视觉证据准备" if is_screenshot else "视频准备",
                            "长截图准备完成" if is_screenshot else "关键帧提取完成",
                            details={
                                "视觉证据图片数": item_dict.get("frame_count"),
                                "证据目录": _frame_dir,
                                **({"长截图预处理": [item_dict.get(f"screenshot_meta{n}") for n in range(1, _compare_product_count(item_dict) + 1)]} if is_screenshot else {}),
                            },
                            progress=12,
                            progress_message=f"长截图准备完成（{item_dict.get('frame_count', 0)} 张）" if is_screenshot else f"关键帧提取完成（{item_dict.get('frame_count', 0)} 帧）",
                        )
                    except Exception as e:
                        last_error = e
                        log_event(
                            "视觉证据准备" if is_screenshot else "视频准备",
                            "失败",
                            level=logging.ERROR,
                            details=error_details(e),
                            progress=12,
                            progress_message="长截图准备失败" if is_screenshot else "视频校验或抽帧失败",
                            progress_status="error",
                        )
                if last_error is None:
                    for attempt in range(2):
                        try:
                            if attempt:
                                log_event(
                                    "单题评测",
                                    "开始外层重试",
                                    level=logging.WARNING,
                                    details={"请求次数": f"{attempt + 1}/2"},
                                    progress=15,
                                    progress_message="正在重新执行单题评测",
                                )
                            res = await asyncio.wait_for(
                                _eval_one(
                                    task.mode, idx, item_dict,
                                    rich_judges=rich_judges,
                                    compare_judges=compare_judges,
                                    category_display=category_display,
                                ),
                                timeout=eval_timeout,
                            )
                            break
                        except asyncio.TimeoutError:
                            last_error = TimeoutError(f"单题评估超过 {eval_timeout:.0f} 秒")
                            log_event(
                                "单题评测",
                                "超时",
                                level=logging.ERROR,
                                details=error_details(last_error),
                            )
                            break
                        except Exception as e:
                            last_error = e
                            retryable = is_retriable_llm_error(e)
                            will_retry = attempt == 0 and retryable
                            log_event(
                                "单题评测",
                                "失败，准备重试" if will_retry else "最终失败",
                                level=logging.WARNING if will_retry else logging.ERROR,
                                details={
                                    "请求次数": f"{attempt + 1}/2",
                                    "可重试": retryable,
                                    **error_details(e),
                                },
                            )
                            if will_retry:
                                await asyncio.sleep(1.0)
                                continue
                            break
                if res is None:
                    res = {
                        "index": idx,
                        "item_id": item_id,
                        "query": item_dict.get("query", ""),
                        "error": f"{type(last_error).__name__}: {last_error}",
                    }
                    if item_dict.get("context"):
                        res["context"] = item_dict["context"]
                    if isinstance(last_error, ContextBudgetExceeded):
                        res.update(
                            error_type=last_error.code, needs_human_review=True,
                            needs_review=True, needs_review_label="T",
                            review_reasons=[str(last_error)], review_reason=str(last_error),
                        )
                    if is_screenshot and any(
                        (item_dict.get(f"screenshot_meta{n}") or {}).get("split_status") == "risky"
                        for n in range(1, _compare_product_count(item_dict) + 1)
                    ):
                        reasons = list(dict.fromkeys([*res.get("review_reasons", []), RISKY_REVIEW_REASON]))
                        res.update(needs_human_review=True, needs_review=True, needs_review_label="T",
                                   review_reasons=reasons, review_reason="；".join(reasons))
                    _write_eval_error(
                        task.id,
                        idx,
                        item_dict,
                        last_error,
                        request_id=request_id,
                    )
            res["index"] = idx
            if pending_judge_traces:
                await asyncio.to_thread(
                    flush_web_trace_records,
                    pending_judge_traces,
                    res,
                )
            await finish(idx, res, started)
            return res

    return one, clients


async def _aclose_judge_clients(clients: list[JudgeClient]) -> None:
    """运行结束统一关闭裁判客户端（正常/异常/取消三条路径都经此）。"""
    results = await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("关闭裁判客户端失败: %s", r)


async def _run(task: Task, cfg: AppConfig) -> None:
    one, clients = _make_item_evaluator(task, cfg)

    # 多轮垂域视觉评测：同一 session_group 的各轮按 turn_index 串行评测，
    # 评完一轮即生成 ≤120 字总结并注入下一轮 context（会话间/独立项仍并发）。
    # session_group 由 parse_csv 据 is_start/is_end 切组赋值，与上游 session_id 列无关。
    sessions: dict[str, list[int]] = {}
    standalone: list[int] = []
    for i, it in enumerate(task.items):
        grp = it.get("session_group")
        if task.mode == "rich_content" and grp:
            sessions.setdefault(str(grp), []).append(i)
        else:
            standalone.append(i)
    for _grp, idxs in sessions.items():
        idxs.sort(key=lambda i: task.items[i].get("turn_index", 0))

    async def run_session(idxs: list[int]) -> None:
        """组内按轮次串行：把前序各轮总结累积写进当前轮 context 后再评测。
        总结直接取评测调用顺带产出的 turn_summary 字段，不再单独调用模型总结。"""
        prior_summary = ""
        for turn_no, idx in enumerate(idxs, 1):
            it = task.items[idx]
            if prior_summary:
                base_ctx = (it.get("context") or "").strip()
                it["context"] = (
                    f"{base_ctx}\n\n历史对话总结：\n{prior_summary}"
                    if base_ctx
                    else f"历史对话总结：\n{prior_summary}"
                )
            res = await one(idx, it)
            if turn_no == len(idxs):
                continue  # 最后一轮总结无人消费，跳过
            if res and not res.get("error"):
                summary = (res.get("turn_summary") or "").strip()
                prior_summary += (
                    f"【第{turn_no}轮】{summary}\n"
                    if summary
                    else f"【第{turn_no}轮】（未生成总结）\n"
                )
            else:
                prior_summary += f"【第{turn_no}轮】（评测未产出结果）\n"

    coros = [run_session(idxs) for idxs in sessions.values()]
    coros += [one(i, task.items[i]) for i in standalone]
    try:
        await asyncio.gather(*coros)
    finally:
        await _aclose_judge_clients(clients)


_PREPARED_ITEM_FIELDS = {
    "evidence_mode", "screenshot_meta1", "screenshot_meta2", "screenshot_meta3",
    "frames", "frames1", "frames2", "frames3", "frame_count", "media",
    "video_name", "video1_path", "video2_path", "video3_path",
    "duration", "duration1", "duration2", "duration3",
}


def _base_context(item: dict) -> str:
    """移除旧运行注入的会话总结，防止补跑时重复叠加。"""
    return str(item.get("context") or "").split("\n\n历史对话总结：\n", 1)[0].strip()


async def run_retry(task: Task, cfg: AppConfig, retry_id: str) -> None:
    """执行人工失败补跑；候选成功才写回原任务，失败仅进入 attempt 审计。"""
    retry = task.retry_runs[retry_id]
    retry["status"] = "running"
    retry["started_at"] = time.time()
    task.repair_status = "running"
    _persist_task(task, force=True)
    task._fanout("retry_start", {"retry_id": retry_id, "total": len(retry.get("indexes") or [])})

    indexes = [int(index) for index in retry.get("indexes") or []]
    target_set = set(indexes)
    reasons = {int(k): v for k, v in (retry.get("reasons") or {}).items()}
    working_items: dict[int, dict] = {}
    runtime_options = {
        **task.options,
        **(retry.get("options") or {}),
        "_retry_id": retry_id,
    }
    clients: list[JudgeClient] = []

    async def on_result(idx: int, res: dict, started: float) -> None:
        item_state = retry["items"].setdefault(str(idx), {})
        retry["completed"] = int(retry.get("completed", 0)) + 1
        item_state["finished_at"] = time.time()
        item_state["latency_s"] = round(time.perf_counter() - started, 3)
        item_state["request_id"] = make_request_id(task.created_at, task.id, idx) + f"_r{retry_id[-6:]}"
        if res.get("error"):
            retry["failed"] = int(retry.get("failed", 0)) + 1
            item_state.update({"status": "failed", "error": res.get("error")})
        else:
            current = latest_results_by_index(task).get(idx)
            may_replace_success = reasons.get(idx) == "session_dependency"
            if current is not None and not current.get("error") and not may_replace_success:
                retry["skipped"] = int(retry.get("skipped", 0)) + 1
                item_state.update({"status": "skipped", "reason": "already_succeeded"})
            else:
                res["retry_id"] = retry_id
                res["recovered_at"] = time.time()
                upsert_result_by_index(task, res)
                prepared = working_items.get(idx) or {}
                for field in _PREPARED_ITEM_FIELDS:
                    if field in prepared:
                        task.items[idx][field] = prepared[field]
                retry["succeeded"] = int(retry.get("succeeded", 0)) + 1
                item_state.update({"status": "succeeded", "error": None})
                await task.publish(
                    "result",
                    {"progress": task.done_total, "total": len(task.items), "result": res,
                     "retry_id": retry_id},
                )
        task._fanout(
            "retry_result",
            {"retry_id": retry_id, "index": idx, **item_state},
        )
        _persist_task(task, force=True)

    async def mark_dependency_skipped(indices: list[int], reason: str) -> None:
        for idx in indices:
            retry["completed"] = int(retry.get("completed", 0)) + 1
            retry["skipped"] = int(retry.get("skipped", 0)) + 1
            state = retry["items"].setdefault(str(idx), {})
            state.update({"status": "skipped", "reason": reason, "finished_at": time.time()})
            task._fanout("retry_result", {"retry_id": retry_id, "index": idx, **state})

    try:
        one, clients = _make_item_evaluator(
            task,
            cfg,
            options=runtime_options,
            on_result=on_result,
        )

        current_results = latest_results_by_index(task)
        grouped: dict[str, list[int]] = {}
        standalone: list[int] = []
        for idx, item in enumerate(task.items):
            group = item.get("session_group")
            if group:
                grouped.setdefault(str(group), []).append(idx)
            elif idx in target_set:
                standalone.append(idx)
        for group_indexes in grouped.values():
            group_indexes.sort(key=lambda i: task.items[i].get("turn_index", 0))

        async def run_one(idx: int, prior_summary: str = "") -> dict:
            working = copy.deepcopy(task.items[idx])
            base_ctx = _base_context(working)
            if prior_summary:
                working["context"] = (
                    f"{base_ctx}\n\n历史对话总结：\n{prior_summary}" if base_ctx
                    else f"历史对话总结：\n{prior_summary}"
                )
            else:
                working["context"] = base_ctx
            working_items[idx] = working
            task.in_flight_indexes.add(idx)
            retry["items"].setdefault(str(idx), {})["status"] = "running"
            try:
                return await one(idx, working)
            finally:
                task.in_flight_indexes.discard(idx)

        async def run_session(group_indexes: list[int]) -> None:
            prior_summary = ""
            selected = [idx for idx in group_indexes if idx in target_set]
            if not selected:
                return
            first_selected = group_indexes.index(selected[0])
            for idx in group_indexes[:first_selected]:
                previous = current_results.get(idx) or {}
                summary = str(previous.get("turn_summary") or "").strip()
                prior_summary += f"【前序轮次】{summary or '（未生成总结）'}\n"
            for position, idx in enumerate(group_indexes[first_selected:]):
                if idx not in target_set:
                    continue
                res = await run_one(idx, prior_summary)
                if res.get("error"):
                    remaining = [
                        other for other in group_indexes[first_selected + position + 1:]
                        if other in target_set
                    ]
                    await mark_dependency_skipped(remaining, "previous_turn_retry_failed")
                    break
                summary = str(res.get("turn_summary") or "").strip()
                prior_summary += f"【补跑轮次】{summary or '（未生成总结）'}\n"

        coros = [run_one(idx) for idx in standalone]
        coros.extend(
            run_session(group_indexes)
            for group_indexes in grouped.values()
            if any(idx in target_set for idx in group_indexes)
        )
        if coros:
            await asyncio.gather(*coros)
        retry["status"] = "completed" if not retry.get("failed") else "partial"
        task.repair_status = retry["status"]
    except asyncio.CancelledError:
        retry["status"] = "error"
        retry["error"] = "服务中断，失败补跑未完成"
        task.repair_status = "error"
        raise
    except Exception as exc:
        logger.exception("失败补跑异常: task_id=%s retry_id=%s", task.id, retry_id)
        retry["status"] = "error"
        retry["error"] = f"{type(exc).__name__}: {exc}"
        task.repair_status = "error"
    finally:
        if clients:
            await _aclose_judge_clients(clients)
        retry["finished_at"] = time.time()
        task.active_runs = max(0, task.active_runs - 1)
        _persist_task(task, force=True)
        task._fanout(
            "done",
            {"summary": task.summary, "total": len(task.items), "retry": retry},
        )
        retire_task(task)


# 登记后台更新批任务引用：避免协程被 GC，也便于测试等待完成。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn_background(coro: Awaitable[None]) -> asyncio.Task:
    """后台启动一个评测协程并保留强引用，完成后自动移出登记表。"""
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


async def _run_update_batch_body(
    task: Task,
    cfg: AppConfig,
    batch: list[tuple[int, dict]],
    *,
    options: dict,
    manage_status: bool = False,
) -> None:
    """后台更新批：batch 内全部条目按提交顺序作为一个串行会话评测；
    每题结果按 index 原地覆盖/追加（后完成者赢），全量重算 summary 并落快照。

    不做任务级状态迁移、不动 done_total、不发 start 事件；manage_status=True
    仅供"本接口新建的任务"使用（否则任务永远停在 pending，重启后会被
    get_task 误判为服务中断，且已连接的 SSE 流收不到终态）。
    """
    async def _merge_on_result(idx: int, res: dict, started: float) -> None:
        action = upsert_result_by_index(task, res)
        # summary 全量重算移入 _flush_now（随节流后的落盘一起做），
        # 不再每题重算 O(n)
        failed = bool(res.get("error"))
        log_event(
            "任务",
            "完成",
            level=logging.ERROR if failed else logging.INFO,
            details={
                "状态": "失败" if failed else "成功",
                "合并": action,
                "总耗时": f"{time.perf_counter() - started:.2f}秒",
                "错误": res.get("error"),
            },
            progress=100,
            progress_message="评测失败" if failed else "评测完成",
            progress_status="error" if failed else "done",
        )
        await task.publish(
            "result",
            {"progress": len(task.results), "total": len(task.items), "result": res},
        )
        _persist_task(task)

    one, clients = _make_item_evaluator(
        task, cfg, options=options, on_result=_merge_on_result
    )
    if manage_status:
        task.status = "running"
        _persist_task(task, force=True)
    current: tuple[int, dict] | None = None
    try:
        # 整批一个串行会话：前轮总结在批次内本地链式注入，
        # 不从 task.results 读回，不受并行批次覆盖影响。
        prior_summary = ""
        for turn_no, (idx, item_dict) in enumerate(batch, 1):
            current = (idx, item_dict)
            if prior_summary:
                base_ctx = (item_dict.get("context") or "").strip()
                item_dict["context"] = (
                    f"{base_ctx}\n\n历史对话总结：\n{prior_summary}"
                    if base_ctx
                    else f"历史对话总结：\n{prior_summary}"
                )
            task.in_flight_indexes.add(idx)
            try:
                res = await one(idx, item_dict)
            finally:
                task.in_flight_indexes.discard(idx)
            if turn_no == len(batch):
                continue  # 最后一轮总结无人消费，跳过
            if res and not res.get("error"):
                summary = (res.get("turn_summary") or "").strip()
                prior_summary += (
                    f"【第{turn_no}轮】{summary}\n"
                    if summary
                    else f"【第{turn_no}轮】（未生成总结）\n"
                )
            else:
                prior_summary += f"【第{turn_no}轮】（评测未产出结果）\n"
        if manage_status:
            task.status = "done"
            task.summary = _summarize(task)  # publish 前重算（节流后不再每题重算）
            await task.publish(
                "done", {"summary": task.summary, "total": len(task.items)}
            )
            _persist_task(task, force=True)
    except Exception as e:
        # one() 内部已把单题异常转成 error res；这里只兜底批级异常
        # （如持久化 I/O 崩溃），避免静默吞掉。
        logger.exception("更新批评测失败: task_id=%s", task.id)
        if current:
            _write_eval_error(task.id, current[0], current[1], e)
        if manage_status:
            task.status = "error"
            task.error = f"{type(e).__name__}: {e}"
            await task.publish("error", {"message": task.error})
        _persist_task(task, force=True)
    finally:
        await _aclose_judge_clients(clients)


async def run_update_batch(
    task: Task,
    cfg: AppConfig,
    batch: list[tuple[int, dict]],
    *,
    options: dict,
    manage_status: bool = False,
) -> None:
    """后台更新批公共入口（实现见 _run_update_batch_body）。

    pin 契约（R1）同 run_eval：调用方在提交时同步 `active_runs += 1`。
    外层负责退休前最后一次落盘、从 TASKS 注册表退休；并行批次共享同一活
    对象（计数 pin），全部结束（idle）时才退休。
    """
    try:
        await _run_update_batch_body(
            task, cfg, batch, options=options, manage_status=manage_status
        )
    finally:
        task.active_runs -= 1
        idle = task.active_runs <= 0
        interrupted = _mark_interrupted_if_stuck(task) if idle else False
        _persist_task(task, force=True)  # 退休前最后一次落盘，磁盘先于内存下线
        if idle and not manage_status and not interrupted and task.status in {"done", "error"}:
            # R4：manage_status=False 的批不发 start/done 终态事件，SSE 订阅者
            # 会一直等；最后一个批结束时补发一次终态（先 persist 再发，summary
            # 已在 _flush_now 重算）。manage_status=True 的终态由 body 发过。
            task._fanout(
                "done" if task.status == "done" else "error",
                (
                    {"summary": task.summary, "total": len(task.items)}
                    if task.status == "done"
                    else {"message": task.error}
                ),
            )
        retire_task(task)


def _write_eval_error(
    task_id: str,
    idx: int,
    item: dict,
    error: Exception | None,
    *,
    request_id: str = "",
) -> None:
    """持久化最终失败，避免内存任务结束后无法定位批跑异常。"""
    try:
        path = RUNS_DIR / "eval_errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "task_id": task_id,
            "request_id": request_id,
            "index": idx,
            "item_id": item.get("id") or f"q{idx}",
            "query": item.get("query", ""),
            "context": item.get("context", ""),
            "error": f"{type(error).__name__}: {error}" if error else "unknown",
            "traceback": "".join(traceback.format_exception(error)) if error else "",
        }
        raw_output = getattr(error, "raw_output", None)
        repair_output = getattr(error, "repair_output", None)
        if raw_output is not None or repair_output is not None:
            record.update({
                "stage": "judge_json_parse",
                "judge": getattr(error, "judge", None),
                "model": getattr(error, "model", None),
                "original_model_output": raw_output,
                "repair_model_output": repair_output,
                "original_output_length": len(raw_output or ""),
                "repair_output_length": len(repair_output or ""),
            })
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _eval_one(
    mode,
    idx,
    item_dict,
    *,
    rich_judges=None,
    compare_judges=None,
    category_display=None,
) -> dict:
    t0 = time.perf_counter()
    item = _to_evalitem(item_dict, idx)
    out: dict = {"query": item.question}
    if item.context:
        out["context"] = item.context

    if mode == "rich_content":
        if not rich_judges:
            raise ValueError("没有可用的垂域视觉评测裁判")
        frames = [str(path) for path in (item.metadata.get("frames") or [])]
        if not frames:
            raise ValueError("垂域视觉评测缺少关键帧")
        answer_text = str(item_dict.get("answer_text") or "").strip()
        if answer_text:
            out["answer_text"] = answer_text
        # 视觉事实列表不做多裁判模糊合并；使用用户选择顺序中的第一位裁判，
        # 保证 presence/count/items 始终来自同一份自洽观察。
        visual = await rich_judges[0].evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            answer_text=answer_text,
            frames=frames,
        )
        out.update(visual)
        log_event(
            "结果聚合",
            "视觉发现已结构化",
            details={
                "挂卡数": out.get("card_count"),
                "Superlink数": out.get("superlink_count"),
                "需复核": out.get("needs_review"),
                "是否解决问题": out.get("problem_solved"),
            },
            progress=90,
            progress_message="正在整理垂域视觉评测结果",
        )

    else:  # compare
        if not compare_judges:
            raise ValueError("没有可用的垂域视觉对比评测裁判")
        product_count = _compare_product_count(item_dict)
        frames_by_product = {
            product_no: item_dict.get(f"frames{product_no}") or []
            for product_no in range(1, product_count + 1)
        }
        missing_frames = [
            str(product_no)
            for product_no, frames in frames_by_product.items()
            if not frames
        ]
        if missing_frames:
            raise ValueError(
                f"垂域视觉对比评测产品{'/'.join(missing_frames)}缺少关键帧"
            )

        answers = {
            product_no: str(item_dict.get(f"answer{product_no}") or "").strip()
            for product_no in range(1, product_count + 1)
        }
        contexts = {
            product_no: str(item_dict.get(f"context{product_no}") or "").strip()
            for product_no in range(1, product_count + 1)
        }
        out["product_count"] = product_count
        for product_no in range(1, product_count + 1):
            out[f"answer{product_no}"] = answers[product_no]
            out[f"context{product_no}"] = contexts[product_no]

        compare_result = await compare_judges[0].evaluate(
            question=item.question,
            context=(item.context or "").strip(),
            context1=contexts[1],
            answer1=answers[1],
            frames1=[str(path) for path in frames_by_product[1]],
            context2=contexts[2],
            answer2=answers[2],
            frames2=[str(path) for path in frames_by_product[2]],
            context3=contexts.get(3, ""),
            answer3=answers.get(3, ""),
            frames3=(
                [str(path) for path in frames_by_product[3]]
                if product_count == 3
                else None
            ),
            product_count=product_count,
            **({
                "evidence_mode": "long_screenshot",
                "screenshot_metas": [item_dict[f"screenshot_meta{n}"] for n in range(1, product_count + 1)],
            } if item_dict.get("evidence_mode") == "long_screenshot" else {}),
        )
        out.update(compare_result)
        log_event(
            "结果聚合",
            "视觉对比完成",
            details={
                "产品数": product_count,
                "理解需求排名": compare_result.get("understanding_rank_groups"),
                "服务闭环排名": compare_result.get("service_closure_rank_groups"),
                "场景化满足排名": compare_result.get("scenario_fulfillment_rank_groups"),
                "直观高效排名": compare_result.get("intuitive_efficiency_rank_groups"),
                "需要复核": compare_result.get("needs_human_review"),
                "内容冲突": compare_result.get("has_conflict"),
            },
            progress=90,
            progress_message="垂域视觉对比评测完成",
        )

    # 实际归属垂域 + 题号，供按垂域聚合；未配置映射时显示原始 category
    out["item_id"] = item.id
    out["category"] = item.category
    out["category_display"] = (category_display or {}).get(item.category) or (
        item.category if item.category != "default" else "通用"
    )
    out["latency_s"] = round(time.perf_counter() - t0, 1)  # 该题评测总耗时（秒）
    return out


def _summarize(task: Task) -> dict:
    if task.mode == "rich_content":
        return _summarize_rich_content(task)
    # compare：协议定义七维绝对分；准确性保留逐题输出但暂不参与汇总。
    res = [
        result for index, result in latest_results_by_index(task).items()
        if 0 <= index < len(task.items)
    ]
    ok = [r for r in res if not r.get("error")]
    valid = [
        row for row in ok
        if all(
            row.get(f"answer{product_no}_input_status") != "failed"
            for product_no in range(1, int(row.get("product_count") or 2) + 1)
        )
    ]
    summary: dict = {
        "total": len(task.items),
        "done": len(ok),
        "failed": len(task.items) - len(ok),
        "input_failed": len(ok) - len(valid),
        "comparable": len(valid),
        "mode": task.mode,
        "standard_version": (
            task.protocol_manifest.get("standard_version")
            or next((row.get("standard_version") for row in ok if row.get("standard_version")), None)
            or resolve_compare_protocol(task.evaluation_profile or None).standard_version
        ),
        "evaluation_profile": task.evaluation_profile,
        "accuracy_aggregation_enabled": False,
        "needs_human_review_count": sum(
            bool(row.get("needs_human_review")) for row in valid
        ) + sum(bool(row.get("needs_human_review")) for row in res if row.get("error_type") == "context_budget_exceeded"),
    }
    max_product_count = max(
        (int(row.get("product_count") or 2) for row in valid),
        default=2,
    )
    summary["product_count"] = max_product_count
    for product_no in range(1, max_product_count + 1):
        summary[f"answer{product_no}_response_gate_pass"] = sum(
            row.get(f"answer{product_no}_response_gate") == "pass"
            for row in valid
            if int(row.get("product_count") or 2) >= product_no
        )
        summary[f"answer{product_no}_safety_gate_pass"] = sum(
            row.get(f"answer{product_no}_safety_gate") == "pass"
            for row in valid
            if int(row.get("product_count") or 2) >= product_no
        )

    for dimension in COMPARE_AGGREGATION_DIMENSIONS:
        applicable = [
            row for row in valid if row.get(f"{dimension}_applicable") is True
        ]
        scored = [
            row for row in applicable
            if all(
                row.get(f"answer{product_no}_{dimension}_score") is not None
                for product_no in range(1, int(row.get("product_count") or 2) + 1)
            )
        ]
        summary[f"{dimension}_applicable"] = len(applicable)
        summary[f"{dimension}_na"] = len(valid) - len(applicable)
        summary[f"{dimension}_coverage"] = (
            round(len(applicable) / len(valid), 3) if valid else None
        )
        summary[f"{dimension}_scored"] = len(scored)
        summary[f"{dimension}_score_coverage"] = (
            round(len(scored) / len(applicable), 3) if applicable else None
        )
        for product_no in range(1, max_product_count + 1):
            scores = [
                row.get(f"answer{product_no}_{dimension}_score")
                for row in applicable
                if int(row.get("product_count") or 2) >= product_no
                and row.get(f"answer{product_no}_{dimension}_score") is not None
            ]
            summary[f"{dimension}_answer{product_no}_avg"] = (
                round(sum(scores) / len(scores), 3) if scores else None
            )
            summary[f"{dimension}_answer{product_no}_sole_first"] = sum(
                row.get(f"{dimension}_rank_groups", [])[:1]
                == [[f"product{product_no}"]]
                for row in scored
            )
            summary[f"{dimension}_answer{product_no}_tied_first"] = sum(
                bool(row.get(f"{dimension}_rank_groups"))
                and f"product{product_no}" in row[f"{dimension}_rank_groups"][0]
                and len(row[f"{dimension}_rank_groups"][0]) > 1
                for row in scored
            )

    summary["conflict_yes"] = sum(1 for r in valid if r.get("has_conflict") == "yes")
    summary["conflict_no"] = sum(1 for r in valid if r.get("has_conflict") == "no")
    summary["conflict_unclear"] = sum(1 for r in valid if r.get("has_conflict") == "unclear")
    return summary


def _summarize_rich_content(task: Task) -> dict:
    """汇总视觉发现与整体评价，不使用问答类 correctness/准确率口径。"""
    results = [
        result for index, result in latest_results_by_index(task).items()
        if 0 <= index < len(task.items)
    ]
    ok = [row for row in results if not row.get("error")]
    card_cases = [row for row in ok if row.get("card_presence") == "present"]
    superlink_cases = [
        row for row in ok if row.get("superlink_presence") == "present"
    ]
    complete = [row for row in ok if row.get("answer_coverage") == "complete"]
    solved_ok = [row for row in ok if row.get("problem_solved") == "ok"]
    solved_nok = [row for row in ok if row.get("problem_solved") == "nok"]
    solved_review = [row for row in ok if row.get("problem_solved") == "need_review"]
    both = [
        row for row in ok
        if row.get("card_presence") == "present"
        and row.get("superlink_presence") == "present"
    ]
    neither = [
        row for row in complete
        if row.get("card_presence") == "absent"
        and row.get("superlink_presence") == "absent"
    ]

    by_category: dict[str, dict] = {}
    for row in ok:
        category = str(row.get("category") or "default")
        entry = by_category.setdefault(category, {
            "category": category,
            "display": row.get("category_display") or category,
            "count": 0,
            "card_cases": 0,
            "superlink_cases": 0,
            "solved_ok": 0,
            "solved_nok": 0,
            "solved_review": 0,
        })
        entry["count"] += 1
        entry["card_cases"] += int(row.get("card_presence") == "present")
        entry["superlink_cases"] += int(
            row.get("superlink_presence") == "present"
        )
        entry["solved_ok"] += int(row.get("problem_solved") == "ok")
        entry["solved_nok"] += int(row.get("problem_solved") == "nok")
        entry["solved_review"] += int(row.get("problem_solved") == "need_review")

    return {
        "total": len(task.items),
        "done": len(ok),
        "failed": len(task.items) - len(ok),
        "mode": task.mode,
        "card_case_count": len(card_cases),
        "card_presence_rate": (
            round(len(card_cases) / len(ok), 3) if ok else None
        ),
        "card_total": sum(int(row.get("card_count") or 0) for row in ok),
        "superlink_case_count": len(superlink_cases),
        "superlink_presence_rate": (
            round(len(superlink_cases) / len(ok), 3) if ok else None
        ),
        "superlink_total_observed": sum(
            int(row.get("superlink_count") or 0) for row in ok
        ),
        "both_count": len(both),
        "neither_count": len(neither),
        "needs_review_count": sum(bool(row.get("needs_review")) for row in ok),
        "complete_coverage_count": len(complete),
        "solved_ok": len(solved_ok),
        "solved_nok": len(solved_nok),
        "solved_review": len(solved_review),
        "solved_ok_rate": (
            round(len(solved_ok) / len(ok), 3) if ok else None
        ),
        "by_category": sorted(
            by_category.values(),
            key=lambda entry: (-entry["count"], entry["category"]),
        ),
    }
