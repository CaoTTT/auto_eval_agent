"""单消费者 FIFO 任务调度器 + 跨任务共享的模型限流（速率+在途）+ 运行时设置。

任务排队沿用单消费者 FIFO（EvalScheduler，一次跑一张评测表，表内组并发）。
模型调用做跨任务共享的全局令牌桶限流：

- MODEL_LIMITER = TokenBucketRateLimiter：模型调用级限流，每次实际模型请求
  （含重试的每一次尝试）须同时满足「每 RATE_WINDOW_S 秒 至多 concurrency 次」
  与「在途 ≤ max_in_flight」才发出。对任何模型（含 OpenAI 兼容代理）生效，
  不依赖百炼的 RPM/TPM 专属限流。
- PIPELINE_LIMITER = ResizableLimiter（上限 = max_in_flight + waiting_capacity）：
  流水线准入，整题（预处理 + 等模型槽）持有，封顶「抽帧中 + 已抽帧等模型」。
- 速率（concurrency）、最大在途（max_in_flight）、预处理并发（waiting_capacity）、
  单题超时与裁判由 GET/PUT /api/settings 全局管理，持久化到
  runs/web_settings.json（runs/ 不入库，重启后加载）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..paths import RUNS_DIR
from ..preparation import PreparationLimiter
from .history import save_task
from .persistence import drain_task_saves, queue_task_save, wait_task_save
from .tasks import Task, retire_task


logger = logging.getLogger(__name__)
RunEval = Callable[[Task, object], Awaitable[None]]


SETTINGS_PATH = RUNS_DIR / "web_settings.json"
DEFAULT_CONCURRENCY = 5  # 模型调用速率限流：每 2 秒至多 5 次请求
RATE_WINDOW_S = 2.0  # 速率限流窗口：concurrency 表示「每窗口窗口内 N 次」
DEFAULT_MAX_IN_FLIGHT = 40  # 模型调用最大在途上限（模型服务商有在途并发限制）
DEFAULT_WAITING_CAPACITY = 10  # 视频预处理并发上限：同时抽帧的评测数
DEFAULT_EVAL_TIMEOUT_S = 3600.0  # 为 64K 输出及修复增加时间预算，仍可通过 /api/settings 调整
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 64
MIN_MAX_IN_FLIGHT, MAX_MAX_IN_FLIGHT = 1, 1024
MIN_WAITING_CAPACITY, MAX_WAITING_CAPACITY = 0, 256
MIN_EVAL_TIMEOUT_S, MAX_EVAL_TIMEOUT_S = 30.0, 3600.0


@dataclass
class RuntimeSettings:
    concurrency: int = DEFAULT_CONCURRENCY
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT  # 模型在途上限（模型服务商限制）
    waiting_capacity: int = DEFAULT_WAITING_CAPACITY
    eval_timeout_s: float = DEFAULT_EVAL_TIMEOUT_S
    # 裁判名列表（本模块不感知 config，只存原始名字；空 = 未设置，由
    # 调用方回落到配置的第一个裁判）。合法性校验在 server PUT 侧做。
    judges: list[str] = field(default_factory=list)


class ResizableLimiter:
    """FIFO 全局并发槽；运行中可调上限。

    不用 asyncio.Semaphore/Condition：它们在首次挂起时绑定事件循环，
    pytest-asyncio 每个测试新建循环会报 "bound to a different event loop"。
    本实现的等待 future 每次 acquire 现取 running loop 创建，跨循环安全。

    调大上限立即唤醒队头等待者；调小为软限制——不抢占运行中的任务，
    新的 acquire 按 `running < limit` 门控，运行完自然收敛。

    acquire(priority=True) 把等待者插到队头（_wake 的 popleft 天然先放行），
    供 session 组第 2+ 轮 / 批第 2+ 条优先续队：不排到新到达者之后。多个
    优先者之间为 LIFO——每组同一时刻至多一个优先等待者（组内轮次串行），
    有界可接受。取消清理（按 future 恒等出队 / 已预占则归还）与队列位置
    无关，priority 路径与普通路径同等安全。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._running = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    def stats(self) -> dict:
        return {
            "limit": self._limit,
            "running": self._running,
            "queued": sum(1 for w in self._waiters if not w.done()),
        }

    def set_limit(self, limit: int) -> None:
        new = max(1, int(limit))
        if new != self._limit:
            self._limit = new
            self._wake()  # 调大时立刻放行队头；调小时 _wake 自行按新上限门控

    def _wake(self) -> None:
        while self._waiters and self._running < self._limit:
            waiter = self._waiters.popleft()
            if waiter.done():  # 已被取消的滞留者
                continue
            # 先预占槽位再放行，防止连续唤醒超发
            self._running += 1
            waiter.set_result(None)

    async def acquire(self, *, priority: bool = False) -> None:
        if not self._waiters and self._running < self._limit:
            # 有等待者时禁止插队，保证 FIFO（显式 priority 除外）
            self._running += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(waiter)
        else:
            self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.cancelled() or not waiter.done():
                # 从未取得槽位：仅出队
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            else:
                # _wake 已替本协程预占槽位但协程仍被取消：必须归还，
                # 否则计数泄漏、后续请求被永久少一个槽
                self.release()
            raise

    def release(self) -> None:
        self._running = max(0, self._running - 1)
        self._wake()

    def would_block(self) -> bool:
        """现在 acquire 是否需要排队（供展示层决定是否发「等待槽位」事件）。"""
        return bool(self._waiters) or self._running >= self._limit

    async def __aenter__(self) -> "ResizableLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        """带参数的槽位 async CM（`async with lim:` 语法无法给 __aenter__ 传参）。"""
        return _LimiterSlot(self, priority)


class _LimiterSlot:
    def __init__(self, limiter: ResizableLimiter, priority: bool) -> None:
        self._limiter = limiter
        self._priority = priority

    async def __aenter__(self) -> ResizableLimiter:
        await self._limiter.acquire(priority=self._priority)
        return self._limiter

    async def __aexit__(self, *exc) -> None:
        self._limiter.release()


class TokenBucketRateLimiter:
    """模型调用级限流：平滑速率发射（每窗口 `rate` 个，均匀分布）+ 最大在途上限。

    每次实际模型请求（含重试的每一次 `_eval_one`）在 acquire 时须同时满足：
    - 一个速率令牌（每 RATE_WINDOW_S 秒 `rate` 个，**相邻请求间隔 = interval，
      不瞬时突发**）；
    - 一个在途名额（`in_flight < max_in_flight`，模型服务商有在途并发限制）。

    `_in_flight` 即当前在途的模型调用数，随请求开始 `+1`、结束 `release()` `-1`。

    关键（防突发）：发射由 `_refill_loop` 统一按 `interval` 节拍放行，每拍**至多
    放行一个**——即使在途满期间攒了整桶令牌、又一批槽空出来，也只会逐拍放出，
    绝不一批吞掉。`acquire` 一律入队等发射器：稀疏请求即时响应（令牌已备好），
    突发请求被节拍拉开。priority=True 经 appendleft 插队头。取消清理按 future
    恒等出队；set_rate / set_max_in_flight 调大立即放行、调小软生效。
    """

    def __init__(
        self,
        rate: int,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        self._rate = max(1, int(rate))  # 每窗口内的令牌数（窗口 = RATE_WINDOW_S）
        self._rate_per_sec = self._rate / RATE_WINDOW_S  # 折算每秒补币速率
        self._interval = RATE_WINDOW_S / self._rate  # 相邻请求的发射间隔
        self._max_in_flight = max(1, int(max_in_flight))
        self._tokens = 1.0  # 起始 1 个令牌：首个请求立即发出，保持平滑节奏
        self._last: float | None = None  # 上次补币的 loop 时间戳（None=未初始化）
        self._in_flight = 0  # 当前在途模型调用数（受 max_in_flight 约束）
        self._waiters: deque[asyncio.Future] = deque()
        self._minter: asyncio.Task | None = None
        self._minter_loop = None  # 发射器所在事件循环；跨循环(pytest)时须重起

    @property
    def limit(self) -> int:
        return self._rate

    def stats(self) -> dict:
        return {
            "limit": self._rate,
            "running": self._in_flight,
            "queued": sum(1 for w in self._waiters if not w.done()),
            "max_in_flight": self._max_in_flight,
        }

    def set_rate(self, rate: int) -> None:
        new = max(1, int(rate))
        if new != self._rate:
            self._rate = new
            self._rate_per_sec = new / RATE_WINDOW_S
            self._interval = RATE_WINDOW_S / new
            self._ensure_minter()

    def set_max_in_flight(self, cap: int) -> None:
        new = max(1, int(cap))
        if new != self._max_in_flight:
            self._max_in_flight = new
            # 调大：minter 下一拍/下一轮询自然放行，不做一次喂批
            self._ensure_minter()

    def would_block(self) -> bool:
        """现在 acquire 是否需等待（无令牌 或 在途满 或 已有排队者）。"""
        return (
            self._tokens < 1.0
            or self._in_flight >= self._max_in_flight
            or bool(self._waiters)
        )

    def _refill(self, now: float) -> None:
        if self._last is None:
            self._last = now  # 首次基线：只记录时间戳，不凭空补突发
        elapsed = now - self._last
        if elapsed > 0:
            # 每秒补 rate_per_sec 个（= 每 RATE_WINDOW_S 秒补 rate 个）
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate_per_sec)
        self._last = now

    def _ensure_minter(self) -> None:
        # 模块级 MODEL_LIMITER 单例跨测试/协程复用：发射器若绑定在已关闭的
        # 旧事件循环上会一直 pending，导致新循环里的 acquire 永久排队。只要
        # 发射器的 loop 与当前不一致就重起一个绑定新 loop 的。
        if not self._waiters:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行事件循环（如启动/离线同步改设置）：跳过，首个真正的
            # acquire 进入循环后自然补齐发射器。
            return
        if (
            self._minter is None
            or self._minter.done()
            or self._minter.cancelled()
            or self._minter_loop is not loop
        ):
            self._minter = loop.create_task(self._refill_loop())
            self._minter_loop = loop

    async def acquire(self, *, priority: bool = False) -> None:
        # 一律入队等发射器按节拍放行（稀疏请求即时响应，突发被 interval 拉开）
        fut = asyncio.get_running_loop().create_future()
        if priority:
            self._waiters.appendleft(fut)
        else:
            self._waiters.append(fut)
        self._ensure_minter()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.cancelled() or not fut.done():
                # 从未取得令牌/名额：仅出队（minter 也未喂，未计在途）
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            else:
                # 竞态兜底：已喂令牌+名额但协程被取消——归还展示计数
                self._in_flight = max(0, self._in_flight - 1)
            raise

    async def _refill_loop(self) -> None:
        """按 interval 节拍逐拍放行一个队头等待者；队列清空即退出。"""
        while self._waiters:
            now = asyncio.get_running_loop().time()
            self._refill(now)
            if not self._waiters:
                return
            if self._in_flight >= self._max_in_flight:
                # 在途满：只等 release() 释放名额后重入；短轮询兜底防漏唤醒
                await asyncio.sleep(0.02)
                continue
            if self._tokens >= 1.0:
                waiter = self._waiters[0]
                if waiter.done():  # 已被取消清理的滞留者
                    self._waiters.popleft()
                    continue
                self._waiters.popleft()
                self._tokens -= 1.0
                self._in_flight += 1
                waiter.set_result(None)
                # 平滑节奏：放行后等一个间隔再放下一个，绝不连发突发
                await asyncio.sleep(self._interval)
                continue
            await asyncio.sleep((1.0 - self._tokens) / self._rate_per_sec)

    def release(self) -> None:
        """请求结束：释放在途名额；发射节奏仍由 minter 节拍控制，不在此补发。"""
        self._in_flight = max(0, self._in_flight - 1)
        self._ensure_minter()

    async def __aenter__(self) -> "TokenBucketRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()

    def slot(self, *, priority: bool = False) -> "_LimiterSlot":
        return _LimiterSlot(self, priority)


MODEL_LIMITER: ResizableLimiter | TokenBucketRateLimiter = TokenBucketRateLimiter(
    DEFAULT_CONCURRENCY, DEFAULT_MAX_IN_FLIGHT
)
PIPELINE_LIMITER = ResizableLimiter(
    DEFAULT_MAX_IN_FLIGHT + DEFAULT_WAITING_CAPACITY
)
# 预处理（抽帧/关键帧提取）并发槽：容量 = 等待容量（waiting_capacity），
# 由 run_preparation 在抽帧阶段持有、进模型前释放。跨事件循环安全。
PREP_LIMITER = PreparationLimiter(max(1, DEFAULT_WAITING_CAPACITY))

_settings = RuntimeSettings()


def get_settings() -> RuntimeSettings:
    return _settings


def _sync_pipeline_limit() -> None:
    """流水线准入 = 最大在途 + 预处理等待容量；整题持有，封顶关键帧内存。

    同时把预处理并发槽对到当前等待容量（clamp ≥1，等待容量最小允许 0）。
    """
    PIPELINE_LIMITER.set_limit(
        max(1, _settings.max_in_flight + _settings.waiting_capacity)
    )
    PREP_LIMITER.set_capacity(max(1, _settings.waiting_capacity))


def apply_settings(
    *,
    concurrency: int | None = None,
    max_in_flight: int | None = None,
    waiting_capacity: int | None = None,
    eval_timeout_s: float | None = None,
    judges: list[str] | None = None,
) -> RuntimeSettings:
    """更新运行时设置并即时对齐限流器（越界值 clamp 到合法区间）。

    concurrency = 模型调用速率（每 RATE_WINDOW_S 秒的令牌数）→ MODEL_LIMITER.set_rate；
    max_in_flight = 模型最大在途（模型服务商并发限制）→ MODEL_LIMITER.set_max_in_flight；
    waiting_capacity = 「预处理并发 + 预处理后等模型」的额度 → PIPELINE_LIMITER。
    流水线准入 = max_in_flight + waiting_capacity（整题持有至模型结束），
    从而在途 ≤ max_in_flight、「抽帧中 + 已抽帧等模型」≤ waiting_capacity——
    关键帧内存被该名额封顶。max_in_flight 或 waiting_capacity 任一变化都重算
    PIPELINE 上限。waiting_capacity=0 时 pipeline = max_in_flight（最严不排队，
    预处理也要等模型释放，ResizableLimiter 再 clamp 到 >=1）。
    """
    global _settings
    if concurrency is not None:
        _settings.concurrency = min(
            MAX_CONCURRENCY, max(MIN_CONCURRENCY, int(concurrency))
        )
        MODEL_LIMITER.set_rate(_settings.concurrency)
    if max_in_flight is not None:
        _settings.max_in_flight = min(
            MAX_MAX_IN_FLIGHT, max(MIN_MAX_IN_FLIGHT, int(max_in_flight))
        )
        MODEL_LIMITER.set_max_in_flight(_settings.max_in_flight)
        _sync_pipeline_limit()
    if waiting_capacity is not None:
        _settings.waiting_capacity = min(
            MAX_WAITING_CAPACITY, max(MIN_WAITING_CAPACITY, int(waiting_capacity))
        )
        _sync_pipeline_limit()
    if eval_timeout_s is not None:
        _settings.eval_timeout_s = min(
            MAX_EVAL_TIMEOUT_S, max(MIN_EVAL_TIMEOUT_S, float(eval_timeout_s))
        )
    if judges is not None:
        _settings.judges = list(dict.fromkeys(str(name) for name in judges))
    return _settings


def load_persisted_settings(path: Path | None = None) -> RuntimeSettings:
    """启动时加载持久化设置；文件缺失/损坏一律回退默认值，绝不阻断启动。"""
    global _settings
    target = path or SETTINGS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    concurrency = None
    max_in_flight = None
    waiting_capacity = None
    eval_timeout_s = None
    judges = None
    if isinstance(raw, dict):
        if isinstance(raw.get("concurrency"), int):
            concurrency = raw["concurrency"]
        if isinstance(raw.get("max_in_flight"), int):
            max_in_flight = raw["max_in_flight"]
        if isinstance(raw.get("waiting_capacity"), int):
            waiting_capacity = raw["waiting_capacity"]
        if isinstance(raw.get("eval_timeout_s"), (int, float)):
            eval_timeout_s = float(raw["eval_timeout_s"])
        persisted_judges = raw.get("judges")
        if isinstance(persisted_judges, list) and all(
            isinstance(name, str) for name in persisted_judges
        ):
            judges = persisted_judges
    apply_settings(
        concurrency=concurrency,
        max_in_flight=max_in_flight,
        waiting_capacity=waiting_capacity,
        eval_timeout_s=eval_timeout_s,
        judges=judges,
    )
    return _settings


def persist_settings(path: Path | None = None) -> bool:
    """原子写（tmp + os.replace，镜像 history.save_task 的模式）。best-effort。"""
    target = path or SETTINGS_PATH
    payload = {
        "concurrency": _settings.concurrency,
        "max_in_flight": _settings.max_in_flight,
        "waiting_capacity": _settings.waiting_capacity,
        "eval_timeout_s": _settings.eval_timeout_s,
        "judges": _settings.judges,
        "updated_at": time.time(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return True
    except OSError:
        logger.exception("保存系统设置失败: %s", target)
        return False


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
        if task.execution_control.get("state") == "queued":
            task.execution_control["state"] = "cancelled"
            task.execution_control["resumes"][-1]["status"] = "cancelled"
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
        saved = queue_task_save(task, save=save_task)
        task._fanout(event, {"message": message, "job_id": job.job_id})
        if saved is None:
            retire_task(task)
        else:
            # Saving pins the task; retry retirement only after the write ends.
            # On disk failure retain the in-memory result for recovery.
            saved.add_done_callback(
                lambda future: retire_task(task)
                if not future.cancelled() and future.result() else None
            )
        return task

    def pause(self, task: Task) -> None:
        """Remove waiting jobs; running jobs observe the durable pause flag."""
        for job in list(self._pending):
            if job.task is not task:
                continue
            self._pending.remove(job)
            task.active_runs = max(0, task.active_runs - 1)
            if job.kind == "retry":
                retry = task.retry_runs[job.job_id]
                retry.update(status="paused", finished_at=time.time())
                task.repair_status = "paused"

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
                        if job.kind == "initial":
                            job.task.start_timing()
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
                            job.task.finish_timing()
                        await wait_task_save(job.task, save=save_task)
                        retire_task(job.task)
                    finally:
                        if job.kind == "initial" and job.task._timing_started_monotonic is not None:
                            job.task.finish_timing()
                            await wait_task_save(job.task, save=save_task)
                        self._running = None
                        job = None  # The idle worker otherwise retains the last task.
                        retry = None
                self._wake.clear()
        finally:
            self._running = None

    @staticmethod
    def _entry(job: _QueuedEval, *, status: str) -> dict:
        task = job.task
        attempt = (task.execution_control.get("resumes") or [{}])[-1]
        resuming = job.kind == "initial" and attempt.get("status") in {"queued", "running"}
        entry = {
            "job_id": job.job_id,
            "kind": "resume" if resuming else job.kind,
            "task_id": task.id,
            "dataset_name": task.dataset_name,
            "mode": task.mode,
            "status": status,
            # Compatibility field: current global requests per RATE_WINDOW_S,
            # not the obsolete per-task Case capacity or model in-flight count.
            # Do not parse legacy task/retry/resume options for live scheduling.
            "concurrency": get_settings().concurrency,
            "done": task.done_total if job.kind == "initial" else int(
                (task.retry_runs.get(job.job_id) or {}).get("completed", 0)
            ),
            "total": job.total,
            "created_at": (
                (task.retry_runs.get(job.job_id) or {}).get("created_at", task.created_at)
                if job.kind == "retry" else task.created_at
            ),
            "task_timing": task.timing_snapshot(),
        }
        if task.evaluation_profile:
            entry["evaluation_profile"] = task.evaluation_profile
        if task.judge_runtime:
            entry["judge_runtime"] = task.judge_runtime
        if job.kind == "retry":
            entry["retry_id"] = job.job_id
        return entry
