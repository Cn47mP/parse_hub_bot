import asyncio
import shutil
from functools import partial
from typing import Any

import pillow_heif
from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.handlers import ConnectHandler, DisconnectHandler
from pyrogram.types import BotCommand, BotCommandScopeChat

from core import bs, on_connect, on_disconnect, ws
from db.engine import close_db
from db.init import init_db
from i18n import ISO639_MAP
from log import logger, setup_logging
from plugins.admin.alert import send_cookie_alert
from plugins.helpers import ADMIN_COMMANDS, COMMANDS
from plugins.parse.queue import parse_queue
from services import cookie_health, parse_cache, persistent_cache
from utils.bili_patch import apply as apply_bili_patch
from utils.douyin_patch import apply as apply_douyin_patch
from utils.event_loop import setup_optimized_event_loop

pillow_heif.register_heif_opener()

setup_logging(debug=bs.debug)
if bs.debug:
    logger.enable("parsehub")

# parsehub 的 B 站视频详情请求未带 buvid3 指纹, 会被 B 站 412 风控拦截; 提前修补
apply_bili_patch()
apply_douyin_patch()

setup_optimized_event_loop()
loop = asyncio.new_event_loop()


class Bot(Client):
    def __init__(self) -> None:
        self.cfg = bs

        super().__init__(
            self.cfg.bot_session_name,
            api_id=self.cfg.api_id,
            api_hash=self.cfg.api_hash,
            bot_token=self.cfg.bot_token,
            plugins={"root": "plugins"},
            proxy=self.cfg.bot_proxy,
            ipv6=self.cfg.bot_ipv6,
            loop=loop,
            workdir=self.cfg.sessions_path,
        )

    async def start(self, *args: Any, **kwargs: Any) -> "Bot":
        self.init_watchdog()

        logger.info("初始化数据库...")
        await init_db()
        logger.success("数据库初始化完成")
        await parse_queue.start()

        # 启动时清空缓存：旧 file_id 缓存会导致同链接直接发旧视频（绕过转码），
        # 使新代码的 faststart/转码逻辑永远不生效。清空后首次请求必重新解析+转码。
        cleared_db = await persistent_cache.clear_all()
        await parse_cache.clear()
        logger.info(f"启动时清空缓存: persistent={cleared_db} 条, parse_cache 已清空")

        # 启动时清残留下载目录: 上一次进程被硬杀会留下未清理的 downloads
        if self.cfg.download_dir.exists() and not self.cfg.debug_skip_cleanup:
            shutil.rmtree(self.cfg.download_dir, ignore_errors=True)
            logger.info("启动时已清理残留 downloads 目录")

        parse_cache.start_cleanup()
        cookie_health.set_notifier(partial(send_cookie_alert, self))
        await super().start(*args, **kwargs)
        await self.set_menu()
        await self.set_admin_menu()
        return self

    async def stop(self, *args: Any, **kwargs: Any) -> Client:
        ws.exit_flag = True
        await parse_queue.stop()
        await super().stop(*args, **kwargs)
        await close_db()
        # 结束时清理下载残留
        if self.cfg.download_dir.exists() and not self.cfg.debug_skip_cleanup:
            shutil.rmtree(self.cfg.download_dir)
        return self

    def init_watchdog(self) -> None:
        self.add_handler(ConnectHandler(on_connect))
        self.add_handler(DisconnectHandler(on_disconnect))

    async def set_menu(self) -> None:
        commands = await self.get_bot_commands()
        if len(commands) == len(COMMANDS) and all(c.description in str(COMMANDS.values()) for c in commands):
            logger.debug("菜单无变化, 跳过设置")
            return

        for iso639, bcp47 in ISO639_MAP.items():
            tc = {k: v[bcp47] for k, v in COMMANDS.items()}
            await self.set_bot_commands(
                [BotCommand(command=k, description=v) for k, v in tc.items()],
                language_code=iso639,
            )
            logger.debug(f"{iso639 or '默认'} 菜单已设置: {tc}")
            await asyncio.sleep(0.5)

    async def set_admin_menu(self) -> None:
        """管理员的命令菜单额外带上管理命令"""
        lang = self.cfg.language
        commands = [BotCommand(command=k, description=v[lang]) for k, v in {**COMMANDS, **ADMIN_COMMANDS}.items()]
        expected = [(c.command, c.description) for c in commands]
        for uid in self.cfg.admin_ids:
            scope = BotCommandScopeChat(chat_id=uid)
            try:
                current = await self.get_bot_commands(scope=scope)
                if [(c.command, c.description) for c in current] == expected:
                    continue
                await self.set_bot_commands(commands, scope=scope)
                logger.debug(f"管理员 {uid} 菜单已设置")
            except RPCError as e:
                logger.warning(f"设置管理员 {uid} 菜单失败: {e}")


if __name__ == "__main__":
    bot = Bot()
    bot.run()
