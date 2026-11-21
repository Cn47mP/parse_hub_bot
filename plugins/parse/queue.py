from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core import bs
from db import get_session
from db.models.message_batch import ParseMessageBatchDone
from db.models.queue import ParseQueueDone
from log import logger
from plugins.parse.context import ParseRequest

logger = logger.bind(name="ParseQueue")


def message_batch_key(msg: object) -> str:
    """同一条原始消息的唯一键：chat_id:msg_id（跨进程重启稳定）。"""
    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None)
    msg_id = getattr(msg, "id", None)
    return f"{chat_id}:{msg_id}"


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
        self._pending_urls: set[str] = set()
        self._batch_active = False
        self._batch_total = 0
        self._batch_done = 0
        self._batch_processed = 0
        # 消息级批次完成判定（进程内，按 chat_id:msg_id 归并）
        # msg_key -> 该消息内出现过的全部链接
        self._message_urls: dict[str, set[str]] = {}
        # msg_key -> {url: 未结束的阶段任务数}
        self._message_stages: dict[str, dict[str, int]] = {}
        # 本进程内被登记过的消息（未登记的消息保持「单链接即时删除」旧行为）
        self._message_seen: set[str] = set()
        # msg_key -> 已确认成功的链接
        self._message_done: dict[str, set[str]] = {}
        # 已就删除做出最终判定的 (msg_key, url)
        self._message_decided: set[tuple[str, str]] = set()

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
        self._pending_urls.clear()
        self._message_urls.clear()
        self._message_stages.clear()
        self._message_seen.clear()
        self._message_done.clear()
        self._message_decided.clear()
        logger.info("解析队列已停止")

    async def submit(self, requests: list[ParseRequest]) -> int:
        """只入队、不创建任务；bypass_cache 强制重解析，返回实际入队数。

        已完成 URL 的去重只在批量投递(>1 条)时生效, 单条投递不受断点续跑影响。
        多链接消息: 本批请求覆盖该消息内的全部链接; 若其中已有链接跳过(断点续跑/队列内重复),
        说明该链接早已完成, 一并计入消息批次, 使整条消息可以统一自动删除。
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
        skipped_dup = 0
        enqueued = 0
        # 断点续跑只对批量投递生效: 单条链接仍照常解析/读缓存, 避免用户重发链接却收不到内容。
        bulk = len(requests) > 1
        # msg_key -> 无需在本批执行即视为已完成的链接
        msg_presettled: dict[str, list[str]] = {}
        for req in requests:
            key = message_batch_key(req.msg)
            if key:
                self._message_urls.setdefault(key, set()).add(req.url)
                self._message_seen.add(key)
            if bulk and not req.bypass_cache and req.url in self._done_urls:
                skipped += 1
                msg_presettled.setdefault(key, []).append(req.url)
                continue
            # 队列内去重: 同一 URL 已在待处理队列中(无论单条还是批量)一律不再入队。
            if req.url in self._pending_urls:
                skipped_dup += 1
                msg_presettled.setdefault(key, []).append(req.url)
                continue
            self._pending_urls.add(req.url)
            # 之前被跳过(断点续跑/队列内重复)的链接视为已完成，参与消息批次判定。
            presettled = msg_presettled.get(key)
            if key and presettled:
                self._message_done.setdefault(key, set()).update(presettled)
            self._register_message_stage(key, req.url)
            self._queue.put_nowait(req)
            enqueued += 1
        self._batch_total = enqueued
        self._batch_active = enqueued > 0
        if enqueued:
            logger.info(
                f"解析批次开始: 新增 {enqueued} 条"
                + (f", 跳过已完成 {skipped} 条" if skipped else "")
                + (f", 跳过排队中重复 {skipped_dup} 条" if skipped_dup else "")
                + f", 当前待处理 {self.pending} 条"
            )
        elif skipped or skipped_dup:
            logger.info(
                f"解析批次为空: 跳过已完成 {skipped} 条, 跳过排队中重复 {skipped_dup} 条"
            )
        return enqueued

    def _register_message_stage(self, msg_key: str, url: str) -> None:
        """登记一次待执行的链接阶段任务。

        一个链接可能被拆成多个阶段任务(handle_parse 内替换/再次入队)，每个阶段入队时 +1、
        结束时 -1；只有该消息的所有阶段任务都归零后才做批次判定。
        """
        if not msg_key:
            return
        self._message_seen.add(msg_key)
        stages = self._message_stages.setdefault(msg_key, {})
        stages[url] = stages.get(url, 0) + 1

    async def _settle_message(self, req: ParseRequest, *, succeeded: bool) -> None:
        """按「原始消息」统计完成情况，全部阶段任务结束后统一判定一次。

        默认策略「全部成功才删除」：全部成功时只删除一次原始消息；
        有任一链接失败则保留原消息，便于用户重试。单链接消息行为与原来一致。
        """
        key = message_batch_key(req.msg)
        if not key:
            return
        path = (key, req.url)
        stages = self._message_stages.get(key)
        if stages:
            left = stages.get(req.url, 0) - 1
            if left > 0:
                stages[req.url] = left
                if succeeded:
                    self._message_done.setdefault(key, set()).add(req.url)
                if req.delete_share_url_msg:
                    logger.info(
                        f"分享链接消息删除推迟到该链接全部任务结束: chat_id={req.chat_id}, msg_id={req.msg.id}, 剩余阶段={left}"
                    )
                return
            stages.pop(req.url, None)
            if not stages:
                self._message_stages.pop(key, None)
        if succeeded:
            self._message_done.setdefault(key, set()).add(req.url)
        if key not in self._message_seen:
            # 本队列未登记的消息：保持旧行为，单链接成功即删除。
            if succeeded:
                await self._delete_share_message(req, log_extra=f", 链接数=1")
            return
        if path in self._message_decided:
            self._message_done.pop(key, None)
            return
        done = self._message_done.get(key, set())
        if self._message_stages.get(key) or (self._message_urls.get(key, set()) & self._pending_urls):
            # 该消息仍有阶段任务排队或执行中，等最后一个结束时再判定。
            return
        urls = done | self._message_urls.pop(key, set())
        self._message_seen.discard(key)
        if not urls <= done:
            logger.info(
                f"多链接消息存在失败链接, 保留原消息: chat_id={req.chat_id}, msg_id={req.msg.id}, 失败数={len(urls - done)}"
            )
            self._message_done.pop(key, None)
            return
        if req.delete_share_url_msg and await self._delete_share_message(req, log_extra=f", 链接数={len(urls)}"):
            self._message_decided.add(path)
        self._message_done.pop(key, None)
        await self._persist_message_batch(key, urls)

    async def _delete_share_message(self, req: ParseRequest, *, log_extra: str = "") -> bool:
        """删除原始分享链接消息；每条消息整批最多删除一次。"""
        if not req.delete_share_url_msg:
            return False
        try:
            await req.msg.delete()
        except Exception as e:
            logger.warning(f"删除分享链接消息失败(忽略): chat_id={req.chat_id}, msg_id={req.msg.id}, error={e}")
            return False
        logger.info(f"已删除分享链接消息: chat_id={req.chat_id}, msg_id={req.msg.id}{log_extra}")
        return True

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
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._pending_urls.discard(req.url)
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
        await self._prune_message_batch_done()

    async def _prune_message_batch_done(self, keep_days: int = 7) -> None:
        """清理 7 天前的多链接消息批次记录，避免表无限增长。"""
        try:
            cutoff = datetime.now(tz=UTC) - timedelta(days=keep_days)
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        delete(ParseMessageBatchDone).where(ParseMessageBatchDone.finished_at < cutoff)
                    )
        except Exception as e:
            logger.warning(f"清理多链接消息批次记录失败(不影响解析): {e}")

    async def _worker_loop(self, idx: int) -> None:
        from plugins.parse.handlers import _handle_parse_request  # 延迟导入避免循环依赖

        while True:
            if self._stopping:
                return
            req = await self._queue.get()
            self._pending_urls.discard(req.url)
            # 先取后等：pause 后已醒来的 worker 也不得开始新解析。
            await self._resume_event.wait()
            if self._stopping:
                self._queue.task_done()
                self._pending_urls.add(req.url)
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
            await self._settle_message(req, succeeded=succeeded)

    def _finish_batch_if_idle(self) -> None:
        if self._batch_active and self.pending == 0 and self._batch_processed >= self._batch_total:
            self._batch_active = False
            logger.info(f"解析批次结束: 本批 {self._batch_total} 条处理完成, 成功 {self._batch_done} 条")

    async def _persist_message_batch(self, msg_key: str, urls: set[str]) -> None:
        """把该消息内已完成链接落库，供跨进程重启后的完成判定。"""
        try:
            async with get_session() as session:
                async with session.begin():
                    for url in urls:
                        stmt = (
                            sqlite_insert(ParseMessageBatchDone)
                            .values(msg_key=msg_key, url=url)
                            .on_conflict_do_nothing()
                        )
                        await session.execute(stmt)
        except Exception as e:
            logger.warning(f"多链接消息批次落库失败(不影响解析): msg_key={msg_key}, error={e}")

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
