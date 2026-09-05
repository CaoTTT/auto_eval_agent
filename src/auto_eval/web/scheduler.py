"""全量评测任务的单消费者 FIFO 调度器。"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .history import save_task
from .tasks import Task, retire_task


logger = logging.getLogger(__name__)
RunEval = Callable[[Task, object], Awaitable[None]]


@dataclass
class _QueuedEval:
    task: Task
    cfg: object
    runner: RunEval


class EvalScheduler:
    """逐个执行全量评测任务；每个任务内部仍由自己的并发数控制。"""

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
        self._pending.append(_QueuedEval(task=task, cfg=cfg, runner=runner))
        save_task(task)
        self._wake.set()
        self.start()
        return len(self._pending)

    def cancel(self, task_id: str) -> Task | None:
        """取消尚未开始的任务；运行中或不存在时返回 None。"""
        job = next((item for item in self._pending if item.task.id == task_id), None)
        if job is None:
            return None
        self._pending.remove(job)
        task = job.task
        task.active_runs = max(0, task.active_runs - 1)
        task.status = "cancelled"
        task.error = None
        save_task(task)
        task._fanout("cancelled", {"message": "排队任务已取消"})
        retire_task(task)
        return task

    def snapshot(self) -> dict:
        """返回前端展示所需的运行项和 FIFO 等待项。"""
        running = self._entry(self._running.task, status="running") if self._running else None
        queued = [
            {**self._entry(job.task, status="queued"), "queue_position": position}
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
            job.task.status = "error"
            job.task.error = "服务中断，排队任务未开始"
            save_task(job.task)
            retire_task(job.task)
        self._wake.clear()

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
                        job.task.status = "error"
                        job.task.error = f"{type(exc).__name__}: {exc}"
                        save_task(job.task)
                        retire_task(job.task)
                    finally:
                        self._running = None
                self._wake.clear()
        finally:
            self._running = None

    @staticmethod
    def _entry(task: Task, *, status: str) -> dict:
        return {
            "task_id": task.id,
            "dataset_name": task.dataset_name,
            "mode": task.mode,
            "status": status,
            "concurrency": int(task.options.get("concurrency", 4)),
            "done": task.done_total,
            "total": len(task.items),
            "created_at": task.created_at,
        }
