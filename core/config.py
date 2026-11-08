from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import make_url

load_dotenv()


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(...)
    api_id: str = Field(...)
    api_hash: str = Field(...)
    bot_proxy: str | None = Field(default=None)
    bot_ipv6: bool = Field(default=False, description="使用 IPv6 连接 Telegram (IPv6-only 服务器需开启)")
    data_path: Path = Field(default=Path("data"))
    language: str = Field(default="zh-hans")
    admin_ids: Annotated[list[int], NoDecode] = Field(default=[], description="Bot 管理员 ID, 逗号分隔")
    allowed_users: Annotated[list[int], NoDecode] = Field(
        default=[223347749, 145047838],
        description="白名单用户 ID, 逗号分隔; 空列表=不限制",
    )
    cache_max_entries: int = Field(default=30000, ge=0, description="缓存最大条数, 0 为不限制")
    max_concurrent_downloads: int = Field(
        default=8, ge=1,
        description="最大并发处理条数(同时下载/转码/上传), 限制 downloads 磁盘峰值",
    )
    cache_disabled: bool = Field(default=False, description="禁用缓存")

    # ---- 自动播放安全转码 ----
    # 发送前是否强制把视频转成 TG 可流式播放的 H.264+AAC+faststart MP4。
    # 开启后保证手机/桌面端信息流静音自动播放，代价是 HEVC/AV1 源可能需软件转码（耗时）。
    # 关闭后完全退回旧行为（仅 remux 换壳，HEVC 源仍无法自动播放）。
    autoplay_safe_transcode: bool = Field(
        default=True,
        description="发送前强制转码为可流式播放的 H.264 MP4; 关闭则退回旧行为",
    )
    # 无硬件编码器时，超过该时长/体积的 HEVC 源跳过转码直发原文件（性能与体验取舍），避免 bot 卡死
    autoplay_skip_transcode_max_duration: float = Field(
        default=600, gt=0, description="超过该时长(秒)且无硬件编码时跳过转码直发"
    )
    autoplay_skip_transcode_max_size: int = Field(
        default=500 * 1000**2, gt=0, description="超过该体积(字节)且无硬件编码时跳过转码直发"
    )

    rate_limit_enabled: bool = Field(default=False, description="启用解析速率限制")
    rate_limit_burst: int = Field(default=5, ge=0, description="突发请求阈值, 0 为不限制")
    rate_limit_burst_window: float = Field(default=60, gt=0, description="突发请求统计窗口, 单位秒")
    rate_limit_cooldown: float = Field(default=180, gt=0, description="触发限速后的冷却时间, 单位秒")
    rate_limit_throttle: int = Field(default=1, ge=0, description="冷却期内允许解析次数, 0 为禁止解析")
    rate_limit_throttle_window: float = Field(default=5, gt=0, description="冷却期内允许解析次数对应的统计窗口, 单位秒")
    # chat_id 在 RATE_LIMIT_BURST_WINDOW 秒内达到 RATE_LIMIT_BURST 次解析后，进入 RATE_LIMIT_COOLDOWN 秒冷却期;
    # 冷却期内每 RATE_LIMIT_THROTTLE_WINDOW 秒最多允许 RATE_LIMIT_THROTTLE 次解析.

    download_dir: Path = Path("downloads")

    database_url: str = Field(default="sqlite+aiosqlite:///data/db/database.db")

    debug: bool = Field(default=False)
    debug_skip_cleanup: bool = Field(default=False, description="跳过资源清理")

    demo_mode: bool = Field(default=False, description="启用演示模式")

    def model_post_init(self, __context: Any) -> None:
        """模型初始化后的操作"""
        self.sessions_path.mkdir(parents=True, exist_ok=True)
        self.config_path.mkdir(parents=True, exist_ok=True)

        url = make_url(self.database_url)
        if url.get_backend_name() == "sqlite" and url.database:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    @property
    def sessions_path(self) -> Path:
        return self.data_path / "sessions"

    @property
    def config_path(self) -> Path:
        return self.data_path / "config"

    @property
    def bot_session_name(self) -> str:
        return f"bot_{self.bot_token.split(':')[0]}"

    @field_validator("admin_ids", "allowed_users", mode="before")
    @classmethod
    def admin_ids_init(cls, v: str | list[int]) -> list[int] | list[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    @field_validator("data_path", mode="before")
    @classmethod
    def data_path_init(cls, v: str | Path) -> Path:
        p = Path(v) if isinstance(v, str) else v
        p.mkdir(exist_ok=True, parents=True)
        return p


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        env_prefix="WD_",
    )
    exit_flag: bool = Field(default=False)
    """退出标志"""
    reconnect_grace_seconds: int = Field(default=120)
    """主连接断开后等待自动重连的时间(秒), 超时仍未恢复则结束进程交给容器重启"""


bs = BotSettings()  # type: ignore[call-arg]
ws = WatchdogSettings()

# 白名单：从 .env 的 ALLOWED_USERS 读取（逗号分隔），空列表 = 不限制（所有用户可用）
# 改白名单只需编辑 .env 的 ALLOWED_USERS 并重启服务，无需改代码
ALLOWED_PARSE_USERS: list[int] = bs.allowed_users
