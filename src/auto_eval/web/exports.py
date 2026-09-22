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


def xlsx_download_name(dataset_name: str, task_id: str, *, include_images: bool = True) -> str:
    """Use the uploaded dataset basename on both Windows and Linux servers."""
    basename = str(dataset_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(basename).stem if basename else str(task_id or "测评数据")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff\ufffe\uffff]', "_", stem).strip(" .")
    suffix = "" if include_images else "_不含原图"
    return f"{stem or '测评数据'}_模型测评结果{suffix}.xlsx"


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
        return {"export_id": key, "task_id": job["task_id"], "status": job["status"],
                "include_images": job["include_images"],
                "error": job.get("error", ""), "filename": job.get("filename", "")}

    def create(self, task_id: str, compare_task_id: str | None = None, *, base_url: str = "",
               include_images: bool = True) -> dict:
        # Paired exports contain text and links only.
        include_images = include_images if compare_task_id is None else False
        self.cleanup()
        active = [job for job in self.jobs.values() if job["status"] in {"queued", "generating"}]
        for key, job in self.jobs.items():
            if (job["task_id"] == task_id and job.get("compare_task_id") == compare_task_id
                    and job.get("base_url", "") == base_url and job["include_images"] == include_images
                    and job["status"] in {"queued", "generating"}):
                return self.view(key)
        if len(active) >= self.capacity:
            raise HTTPException(429, "导出队列已满，请稍后重试")
        # 限制未下载的完成记录和临时文件数量。
        completed = sorted((key for key in self.jobs if self.jobs[key]["status"] in {"ready", "error"}), key=lambda key: self.jobs[key]["finished"])
        for key in completed[:-15]:
            self.remove(key)
        key = uuid.uuid4().hex
        self.jobs[key] = {"task_id": task_id, "compare_task_id": compare_task_id, "base_url": base_url,
                          "include_images": include_images, "status": "queued", "path": self.directory / f".xlsx-{key}.xlsx"}
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
                snapshot["export_base_url"] = job.get("base_url", "")
                job["filename"] = xlsx_download_name(snapshot.get("dataset_name", ""), job["task_id"])
                if job.get("compare_task_id"):
                    other = await peek_task_async(job["compare_task_id"])
                    if other is None:
                        raise ValueError("对比任务不存在或已删除")
                    other_snapshot = copy.deepcopy(task_to_snapshot(other))
                    other_snapshot["export_base_url"] = job.get("base_url", "")
                    job["filename"] = job["filename"].replace("_模型测评结果.xlsx", "_任务对比结果.xlsx")
                    await asyncio.to_thread(self._write_comparison, snapshot, other_snapshot, job["path"])
                else:
                    job["filename"] = xlsx_download_name(snapshot.get("dataset_name", ""), job["task_id"],
                                                         include_images=job["include_images"])
                    await asyncio.to_thread(self._write, snapshot, job["path"], include_images=job["include_images"])
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

    def _write(self, snapshot: dict, path: Path, *, include_images: bool = True) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        write_xlsx(snapshot, path, include_images=include_images)

    def _write_comparison(self, snapshot: dict, other: dict, path: Path) -> None:
        from .comparison_export import write_comparison_xlsx
        self.directory.mkdir(parents=True, exist_ok=True)
        write_comparison_xlsx(snapshot, other, path)

    async def close(self) -> None:
        # 不取消正在写文件的线程；先结束生成，再清理文件。
        if self.workers:
            await asyncio.gather(*list(self.workers), return_exceptions=True)
        for key in list(self.jobs):
            self.remove(key)
