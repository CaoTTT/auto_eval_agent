"""全量评测与失败补跑共用的单消费者 FIFO 调度器。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .history import save_task
from .persistence import drain_task_saves, queue_task_save, wait_task_save
from .tasks import Task, retire_task


logger = logging.getLogger(__name__)
RunEval = Callable[[Task, object], Awaitable[None]]


@dataclass
class _QueuedEval:
    job_id: str
    kind: str
    task: Task
    cfg: object
    runner: RunEval
    total: int


class EvalScheduler:
    """逐个执行队列任务；每个任务内部仍由自己的并发数控制。"""

    def __init__(self) -> None:
        self._pending: deque[_QueuedEval] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._running: _QueuedEval | None = None
        self._closed = False

    def start(self) -> None:
        """确保调度 worker 已启动；允许在 FastAPI startup 前惰性启动。"""
        if self._worker is not None and not self._worker.done():
            return
        self._closed = False
        self._worker = asyncio.create_task(self._worker_loop())

    def enqueue(self, task: Task, cfg: object, runner: RunEval) -> int:
        """将任务放到队尾，返回当前等待队列中的 1-based 位置。"""
        task.status = "queued"
        task.error = None
        # queued 也必须 pin，避免等待期间被 LRU/删除端点移除。
        task.active_runs += 1
        self._pending.append(_QueuedEval(
            job_id=task.id,
            kind="initial",
            task=task,
            cfg=cfg,
            runner=runner,
            total=len(task.items),
        ))
        queue_task_save(task, save=save_task)
        self._wake.set()
        self.start()
        return len(self._pending)

    def enqueue_retry(
        self,
        task: Task,
        cfg: object,
        runner: RunEval,
        *,
        retry_id: str,
        total: int,
    ) -> int:
        """将人工触发的失败补跑放到同一 FIFO 队尾，不改变主任务终态。"""
        task.repair_status = "queued"
        task.active_runs += 1
        self._pending.append(_QueuedEval(
            job_id=retry_id,
            kind="retry",
            task=task,
            cfg=cfg,
            runner=runner,
            total=total,
        ))
        queue_task_save(task, save=save_task)
        self._wake.set()
        self.start()
        return len(self._pending)

    def cancel(self, job_id: str) -> Task | None:
        """取消尚未开始的任务；运行中或不存在时返回 None。"""
        job = next((item for item in self._pending if item.job_id == job_id), None)
        if job is None:
            return None
        self._pending.remove(job)
        task = job.task
        task.active_runs = max(0, task.active_runs - 1)
        if job.kind == "retry":
            retry = task.retry_runs.get(job.job_id) or {}
            retry["status"] = "cancelled"
            retry["finished_at"] = time.time()
            task.retry_runs[job.job_id] = retry
            task.repair_status = "cancelled"
            event = "retry_cancelled"
            message = "失败补跑已取消"
        else:
            task.status = "cancelled"
            task.error = None
            event = "cancelled"
            message = "排队任务已取消"
        queue_task_save(task, save=save_task)
        task._fanout(event, {"message": message, "job_id": job.job_id})
        retire_task(task)
        return task

    def reprioritize(self, job_id: str, action: str) -> int | None:
        """调整等待任务位置；不影响正在运行的任务，返回新的 1-based 位置。"""
        job = next((item for item in self._pending if item.job_id == job_id), None)
        if job is None:
            return None
        current = self._pending.index(job)
        if action == "move_to_front":
            target = 0
        elif action == "move_up":
            target = max(0, current - 1)
        elif action == "move_down":
            target = min(len(self._pending) - 1, current + 1)
        else:
            raise ValueError("unsupported queue action")
        if target != current:
            self._pending.remove(job)
            self._pending.insert(target, job)
        return target + 1

    def snapshot(self) -> dict:
        """返回前端展示所需的运行项和 FIFO 等待项。"""
        running = self._entry(self._running, status="running") if self._running else None
        queued = [
            {**self._entry(job, status="queued"), "queue_position": position}
            for position, job in enumerate(self._pending, 1)
        ]
        return {"running": running, "queued": queued}

    async def stop(self) -> None:
        """停止 worker，并把未开始的任务标记为服务中断。"""
        self._closed = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._worker = None
        while self._pending:
            job = self._pending.popleft()
            job.task.active_runs = max(0, job.task.active_runs - 1)
            if job.kind == "retry":
                retry = job.task.retry_runs.get(job.job_id) or {}
                retry["status"] = "error"
                retry["error"] = "服务中断，失败补跑未开始"
                job.task.retry_runs[job.job_id] = retry
                job.task.repair_status = "error"
            else:
                job.task.status = "error"
                job.task.error = "服务中断，排队任务未开始"
            await wait_task_save(job.task, save=save_task)
            retire_task(job.task)
        self._wake.clear()
        await drain_task_saves()

    async def _worker_loop(self) -> None:
        try:
            while not self._closed:
                await self._wake.wait()
                while self._pending and not self._closed:
                    job = self._pending.popleft()
                    self._running = job
                    try:
                        await job.runner(job.task, job.cfg)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # run_eval 已兜底；这里防止 worker 意外死亡
                        logger.exception("排队任务执行失败: task_id=%s", job.task.id)
                        if job.task.active_runs > 0:
                            job.task.active_runs -= 1
                        if job.kind == "retry":
                            retry = job.task.retry_runs.get(job.job_id) or {}
                            retry["status"] = "error"
                            retry["error"] = f"{type(exc).__name__}: {exc}"
                            job.task.retry_runs[job.job_id] = retry
                            job.task.repair_status = "error"
                        else:
                            job.task.status = "error"
                            job.task.error = f"{type(exc).__name__}: {exc}"
                        await wait_task_save(job.task, save=save_task)
                        retire_task(job.task)
                    finally:
                        self._running = None
                self._wake.clear()
        finally:
            self._running = None

    @staticmethod
    def _entry(job: _QueuedEval, *, status: str) -> dict:
        task = job.task
        entry = {
            "job_id": job.job_id,
            "kind": job.kind,
            "task_id": task.id,
            "dataset_name": task.dataset_name,
            "mode": task.mode,
            "status": status,
            "concurrency": int(task.options.get("concurrency", 4)),
            "done": task.done_total if job.kind == "initial" else int(
                (task.retry_runs.get(job.job_id) or {}).get("completed", 0)
            ),
            "total": job.total,
            "created_at": (
                (task.retry_runs.get(job.job_id) or {}).get("created_at", task.created_at)
                if job.kind == "retry" else task.created_at
            ),
        }
        if task.evaluation_profile:
            entry["evaluation_profile"] = task.evaluation_profile
        if job.kind == "retry":
            entry["retry_id"] = job.job_id
        return entry
