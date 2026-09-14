"""有界、顺序后台快照写入；在事件循环内冻结数据，线程只处理独立副本。"""
from __future__ import annotations

import asyncio
import copy
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from .history import save_task, task_to_snapshot

logger = logging.getLogger(__name__)


@dataclass
class _Writer:
    pending: OrderedDict = field(default_factory=OrderedDict)
    worker: asyncio.Task | None = None
    active_id: str | None = None


_writers = WeakKeyDictionary()


async def _drain(writer: _Writer) -> None:
    try:
        while writer.pending:
            writer.active_id, (snapshot, save, waiter) = writer.pending.popitem(last=False)
            try:
                ok = await asyncio.to_thread(save, snapshot)
            except Exception:
                logger.exception("后台快照保存失败: task_id=%s", snapshot.id)
                ok = False
            if not waiter.done():
                waiter.set_result(ok)
            writer.active_id = None
    finally:
        writer.active_id = None
        writer.worker = None


def queue_task_save(task, *, save=None) -> asyncio.Future | None:
    """每个任务最多保留一个待写快照；终态调用者可等待返回的 Future。"""
    save = save or save_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        save(task)
        return None
    writer = _writers.setdefault(loop, _Writer())
    # task_to_snapshot 是浅视图，必须在让出事件循环之前冻结嵌套数据。
    data = copy.deepcopy(task_to_snapshot(task))
    frozen = SimpleNamespace(**data, id=task.id)
    previous = writer.pending.get(task.id)
    waiter = previous[2] if previous else loop.create_future()
    writer.pending[task.id] = (frozen, save, waiter)
    if writer.worker is None:
        writer.worker = loop.create_task(_drain(writer))
    return waiter


async def wait_task_save(task, *, save=None) -> bool:
    pending = queue_task_save(task, save=save)
    return await asyncio.shield(pending) if pending is not None else True


async def drain_task_saves() -> None:
    writer = _writers.get(asyncio.get_running_loop())
    while writer is not None and writer.worker is not None:
        await asyncio.shield(writer.worker)


def task_save_pending(task_id: str) -> bool:
    try:
        writer = _writers.get(asyncio.get_running_loop())
    except RuntimeError:
        return False
    return writer is not None and (writer.active_id == task_id or task_id in writer.pending)
