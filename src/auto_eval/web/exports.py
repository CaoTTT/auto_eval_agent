"""后台 XLSX 生成与有期限的文件下载；不占用评测队列。"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from .history import task_to_snapshot, write_xlsx
from .tasks import peek_task_async

logger = logging.getLogger(__name__)


class XlsxExports:
    def __init__(self, directory: Path, *, capacity: int = 4, ttl: float = 1800):
        self.directory = directory
        self.capacity = capacity
        self.ttl = ttl
        self.jobs: dict[str, dict] = {}
        self.workers: set[asyncio.Task] = set()
        self.slot = asyncio.Semaphore(1)
        self.last_cleanup = 0.0

    def cleanup(self) -> None:
        for key, job in list(self.jobs.items()):
            if job["status"] in {"ready", "error"} and time.monotonic() - job["finished"] > self.ttl:
                self.remove(key)
        if time.monotonic() - self.last_cleanup > 300:
            self.last_cleanup = time.monotonic()
            active_paths = {job["path"] for job in self.jobs.values()}
            for path in self.directory.glob(".xlsx-*.xlsx"):
                if path in active_paths or not re.fullmatch(r"\.xlsx-[0-9a-f]{32}\.xlsx", path.name):
                    continue
                try:
                    if time.time() - path.stat().st_mtime > self.ttl:
                        path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("过期导出文件暂时无法清理: %s", path.name)

    def remove(self, key: str) -> None:
        job = self.jobs.get(key)
        if job:
            try:
                job["path"].unlink(missing_ok=True)
            except OSError:
                logger.warning("导出文件仍被占用，将稍后清理: export_id=%s", key)
                return
            self.jobs.pop(key, None)

    def view(self, key: str) -> dict:
        self.cleanup()
        job = self.jobs.get(key)
        if not job:
            raise HTTPException(404, "导出记录已过期，请重新导出")
        return {"export_id": key, "task_id": job["task_id"], "status": job["status"], "error": job.get("error", "")}

    def create(self, task_id: str) -> dict:
        self.cleanup()
        active = [job for job in self.jobs.values() if job["status"] in {"queued", "generating"}]
        for key, job in self.jobs.items():
            if job["task_id"] == task_id and job["status"] in {"queued", "generating"}:
                return self.view(key)
        if len(active) >= self.capacity:
            raise HTTPException(429, "导出队列已满，请稍后重试")
        # 限制未下载的完成记录和临时文件数量。
        completed = sorted((key for key in self.jobs if self.jobs[key]["status"] in {"ready", "error"}), key=lambda key: self.jobs[key]["finished"])
        for key in completed[:-15]:
            self.remove(key)
        key = uuid.uuid4().hex
        self.jobs[key] = {"task_id": task_id, "status": "queued", "path": self.directory / f".xlsx-{key}.xlsx"}
        worker = asyncio.create_task(self._generate(key))
        self.workers.add(worker)
        worker.add_done_callback(self.workers.discard)
        return self.view(key)

    async def _generate(self, key: str) -> None:
        job = self.jobs[key]
        try:
            async with self.slot:
                job["status"] = "generating"
                task = await peek_task_async(job["task_id"])
                if task is None:
                    raise ValueError("任务不存在或已删除")
                # 快照在开始生成时固定；运行中的原任务可以继续更新。
                snapshot = copy.deepcopy(task_to_snapshot(task))
                await asyncio.to_thread(self._write, snapshot, job["path"])
                job["status"] = "ready"
        except Exception as exc:
            logger.exception("XLSX 导出失败: task_id=%s", job["task_id"])
            try:
                job["path"].unlink(missing_ok=True)
            except OSError:
                logger.warning("导出失败后的临时文件清理失败: export_id=%s", key)
            job["status"] = "error"
            job["error"] = "生成 Excel 失败，请重试；若仍失败请检查服务器磁盘空间和日志。"
            if isinstance(exc, ValueError):
                job["error"] = str(exc)
        finally:
            job["finished"] = time.monotonic()

    def _write(self, snapshot: dict, path: Path) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        write_xlsx(snapshot, path)

    async def close(self) -> None:
        # 不取消正在写文件的线程；先结束生成，再清理文件。
        if self.workers:
            await asyncio.gather(*list(self.workers), return_exceptions=True)
        for key in list(self.jobs):
            self.remove(key)
