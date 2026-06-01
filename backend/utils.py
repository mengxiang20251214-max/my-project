"""共享工具函数（封面提取、Banner 文件保存等）。"""
import logging
import os
import shutil
import subprocess
import uuid
from typing import Optional

import aiofiles

logger = logging.getLogger("videohub.utils")

# 流式落盘的分块大小（1MB）；Banner 视频可能很大，分块写避免一次性读进内存
CHUNK_SIZE = 1024 * 1024

# Banner 允许的上传格式 → 媒体类型
BANNER_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
BANNER_GIF_EXT   = {".gif"}
BANNER_VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
BANNER_ALLOWED_EXT = BANNER_IMAGE_EXT | BANNER_GIF_EXT | BANNER_VIDEO_EXT


def banner_media_type_for(ext: str) -> str:
    """根据扩展名推断 Banner 媒体类型：image / gif / video。"""
    ext = ext.lower()
    if ext in BANNER_VIDEO_EXT:
        return "video"
    if ext in BANNER_GIF_EXT:
        return "gif"
    return "image"


async def save_banner_file(file, temp_dir: str) -> tuple[str, str, str]:
    """
    异步流式把上传的 Banner 文件（图片/GIF/视频）写到临时目录，交给存储层落库。

    Banner 允许任意大小，所以用 aiofiles 分块写入，避免整文件读进内存、
    也不阻塞事件循环。落库（本地 move / R2 上传）由调用方的 STORAGE 完成。

    Args:
        file:     FastAPI UploadFile
        temp_dir: 临时目录（STORAGE.temp_dir）

    Returns:
        (临时文件绝对路径, media_type, 扩展名)，如 ("/.../xxx.mp4", "video", ".mp4")

    Raises:
        ValueError: 扩展名不被允许
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in BANNER_ALLOWED_EXT:
        raise ValueError(f"不支持的 Banner 文件类型: {ext or '未知'}")
    os.makedirs(temp_dir, exist_ok=True)
    dest = os.path.abspath(os.path.join(temp_dir, f"{uuid.uuid4().hex}{ext}"))
    try:
        await file.seek(0)
        async with aiofiles.open(dest, "wb") as out:
            while chunk := await file.read(CHUNK_SIZE):
                await out.write(chunk)
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    return dest, banner_media_type_for(ext), ext


def extract_cover(video_abs: str, covers_dir: str, stem: str) -> Optional[str]:
    """
    使用 ffmpeg 从视频文件中提取一帧作为封面图（JPEG 格式）。

    策略：
    1. 优先取第 2 秒画面（适合大多数视频）
    2. 回退：取第 0 帧（针对时长 < 2 秒的短视频）
    3. ffmpeg 不可用或出错 → 返回 None，调用方使用默认占位图

    Args:
        video_abs:  视频文件绝对路径
        covers_dir: 封面输出目录（绝对路径，自动创建）
        stem:       文件名主干，封面命名为 {stem}_cover.jpg

    Returns:
        生成的封面 **本地文件绝对路径**（由调用方交给存储层落库），或 None。
        （注意：不再返回 URL —— URL 由 storage 决定，本地/R2 不同）
    """
    ffmpeg_bin = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg_bin:
        logger.info(
            "ffmpeg 未安装，跳过自动封面提取。"
            "请从 https://ffmpeg.org/download.html 安装后重启服务。"
        )
        return None

    if not os.path.isfile(video_abs):
        logger.warning("视频文件不存在，无法提取封面: %s", video_abs)
        return None

    os.makedirs(covers_dir, exist_ok=True)
    cover_name = f"{stem}_cover.jpg"
    cover_abs = os.path.abspath(os.path.join(covers_dir, cover_name))

    def _run(seek_args: list[str]) -> bool:
        """执行 ffmpeg 命令，返回是否成功生成有效图片。"""
        cmd = (
            [ffmpeg_bin, "-y", "-i", video_abs]
            + seek_args
            + ["-vframes", "1", "-q:v", "3", cover_abs]
        )
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                timeout=60,
            )
            return (
                res.returncode == 0
                and os.path.isfile(cover_abs)
                and os.path.getsize(cover_abs) > 0
            )
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg 提取封面超时（60 s），视频: %s", video_abs)
        except FileNotFoundError:
            logger.error("ffmpeg 可执行文件丢失: %s", ffmpeg_bin)
        except Exception as exc:
            logger.warning("ffmpeg 异常: %s", exc)
        return False

    # 第一次尝试：第 2 秒
    if _run(["-ss", "00:00:02"]):
        logger.debug("封面提取成功（第 2 秒）: %s", cover_abs)
        return cover_abs

    # 回退：第 0 帧（短视频 / 纯音频等边缘情况）
    if _run([]):
        logger.debug("封面提取成功（第 0 帧）: %s", cover_abs)
        return cover_abs

    logger.warning("ffmpeg 封面提取失败，视频: %s", video_abs)
    return None
