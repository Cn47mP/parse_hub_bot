from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core import bs
from db import get_session
from db.models.queue import ParseQueueDone
from log import logger
from plugins.parse.context import ParseRequest

logger = logger.bind(name="ParseQueue")


class ParseQueue:
    """固定 worker 池；每批 submit 独立统计，成功 URL 才参与断点续跑。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ParseRequest] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._started = False
        self._stopping = False
        self._done_urls: set[str] = set()
        self._batch_active = False
        self._batch_total = 0
        self._batch_done = 0
        self._batch_processed = 0

    @property
    def worker_count(self) -> int:
        return bs.max_concurrent_downloads

    @property
    def paused(self) -> bool:
        return not self._resume_event.is_set()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        """启动 worker 池，并从数据库载入成功完成的 URL。"""
        if self._started:
            return
        await self._load_done_urls()
        self._stopping = False
        self._resume_event.set()
        self._workers = [asyncio.ensure_future(self._worker_loop(i)) for i in range(self.worker_count)]
        self._started = True
        logger.info(f"解析队列已启动: worker={self.worker_count}, 历史已完成 {len(self._done_urls)} 条")

    async def stop(self) -> None:
        """取消所有 worker，等待退出；未执行请求随进程结束丢弃。"""
        if not self._started:
            return
        self._stopping = True
        self._resume_event.set()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False
        self._batch_active = False
        logger.info("解析队列已停止")

    async def submit(self, requests: list[ParseRequest]) -> int:
        """只入队、不创建任务；bypass_cache 强制重解析，返回实际入队数。

        已完成 URL 的去重只在批量投递(>1 条)时生效, 单条投递不受断点续跑影响。
        """
        if not self._started:
            raise RuntimeError("解析队列尚未启动")

        # 直到入队完成没有 await，因此并发 submit 不会交叉批次统计。
        if self._batch_active:
            logger.info(f"上一解析批次尚有任务，结束其统计: 总数 {self._batch_total}, 已处理 {self._batch_processed}")
        self._batch_active = False
        self._batch_total = 0
        self._batch_done = 0
        self._batch_processed = 0

        skipped = 0
        enqueued = 0
        # 断点续跑只对批量投递生效: 单条链接仍照常解析/读缓存, 避免用户重发链接却收不到内容。
        bulk = len(requests) > 1
        for req in requests:
            if bulk and not req.bypass_cache and req.url in self._done_urls:
                skipped += 1
                continue
            self._queue.put_nowait(req)
            enqueued += 1
        self._batch_total = enqueued
        self._batch_active = enqueued > 0
        if enqueued:
            logger.info(
                f"解析批次开始: 新增 {enqueued} 条"
                + (f", 跳过已完成 {skipped} 条" if skipped else "")
                + f", 当前待处理 {self.pending} 条"
            )
        elif skipped:
            logger.info(f"解析批次为空: {skipped} 条全部已完成, 跳过")
        return enqueued

    def pause(self) -> None:
        """只阻断新任务，进行中的任务不打断。"""
        self._resume_event.clear()
        logger.info("解析队列已暂停(进行中的任务不受影响)")

    def resume(self) -> None:
        self._resume_event.set()
        logger.info("解析队列已恢复")

    def clear_pending(self) -> int:
        """清空未执行任务，不影响已取出的任务；对应请求从本批总数移除。"""
        cleared = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
                cleared += 1
        if cleared:
            self._batch_total = max(self._batch_total - cleared, self._batch_processed)
            self._finish_batch_if_idle()
        logger.info(f"已清空待处理队列: {cleared} 条")
        return cleared

    def status(self) -> dict[str, int | bool]:
        return {
            "running": self._started,
            "paused": self.paused,
            "done": self._batch_done,
            "processed": self._batch_processed,
            "total": self._batch_total,
            "pending": max(self._batch_total - self._batch_processed, 0),
            "queued": self.pending,
            "concurrency": self.worker_count,
        }

    async def _load_done_urls(self) -> None:
        try:
            async with get_session() as session:
                rows = await session.execute(select(ParseQueueDone.url))
                self._done_urls = set(rows.scalars().all())
        except Exception as e:
            self._done_urls = set()
            logger.warning(f"载入已完成 URL 失败, 本次启动不去重: {e}")

    async def _worker_loop(self, idx: int) -> None:
        from plugins.parse.handlers import _handle_parse_request  # 延迟导入避免循环依赖

        while True:
            if self._stopping:
                return
            req = await self._queue.get()
            # 先取后等：pause 后已醒来的 worker 也不得开始新解析。
            await self._resume_event.wait()
            if self._stopping:
                self._queue.task_done()
                self._queue.put_nowait(req)
                return
            succeeded = False
            try:
                succeeded = await _handle_parse_request(req)
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception as e:
                logger.opt(exception=e).error(f"队列 worker#{idx} 解析异常: url={req.url}")
            self._queue.task_done()
            await self._record_result(req.url, succeeded=succeeded)

    def _finish_batch_if_idle(self) -> None:
        if self._batch_active and self.pending == 0 and self._batch_processed >= self._batch_total:
            self._batch_active = False
            logger.info(f"解析批次结束: 本批 {self._batch_total} 条处理完成, 成功 {self._batch_done} 条")

    async def _record_result(self, url: str, *, succeeded: bool) -> None:
        """成功与失败都结束进度；只有成功才写 done 集合并落库。"""
        self._batch_processed += 1
        if succeeded:
            self._batch_done += 1
            self._done_urls.add(url)
        logger.info(f"队列进度: 已完成 {self._batch_done} / 总数 {self._batch_total} (剩余 {max(self._batch_total - self._batch_processed, 0)}, 待处理 {self.pending})")
        self._finish_batch_if_idle()
        if not succeeded:
            return
        try:
            async with get_session() as session:
                async with session.begin():
                    stmt = sqlite_insert(ParseQueueDone).values(url=url).on_conflict_do_nothing()
                    await session.execute(stmt)
        except Exception as e:
            logger.warning(f"已完成 URL 落库失败(不影响解析): url={url}, error={e}")


parse_queue = ParseQueue()
