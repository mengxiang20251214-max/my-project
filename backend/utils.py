"""共享工具函数（封面提取、Banner 文件保存等）。"""
import logging
import os
import shutil
import subprocess
import uuid
from typing import Optional

logger = logging.getLogger("videohub.utils")

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


def save_banner_file(file, banners_dir: str) -> tuple[str, str]:
    """
    保存上传的 Banner 文件（图片/GIF/视频）到 banners_dir。

    Args:
        file:        FastAPI UploadFile（.file 为同步文件对象）
        banners_dir: 保存目录绝对路径（自动创建）

    Returns:
        (相对 URL, media_type)，如 ("/static/uploads/banners/xxx.mp4", "video")

    Raises:
        ValueError: 扩展名不被允许
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in BANNER_ALLOWED_EXT:
        raise ValueError(f"不支持的 Banner 文件类型: {ext or '未知'}")
    os.makedirs(banners_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.abspath(os.path.join(banners_dir, filename))
    # UploadFile.file 为标准文件对象，用 shutil 同步落盘（Banner 文件通常不大）
    file.file.seek(0)
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return f"/static/uploads/banners/{filename}", banner_media_type_for(ext)


def extract_cover(video_abs: str, covers_dir: str, stem: str) -> Optional[str]:
    """
    使用 ffmpeg 从视频文件中提取一帧作为封面图（JPEG 格式）。

    策略：
    1. 优先取第 2 秒画面（适合大多数视频）
    2. 回退：取第 0 帧（针对时长 < 2 秒的短视频）
    3. ffmpeg 不可用或出错 → 返回 None，调用方使用默认占位图

    Args:
        video_abs:  视频文件绝对路径
        covers_dir: 封面保存目录（绝对路径，自动创建）
        stem:       文件名主干，封面命名为 {stem}_cover.jpg

    Returns:
        封面的 URL 相对路径（如 /static/uploads/covers/abc_cover.jpg），或 None
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
        return f"/static/uploads/covers/{cover_name}"

    # 回退：第 0 帧（短视频 / 纯音频等边缘情况）
    if _run([]):
        logger.debug("封面提取成功（第 0 帧）: %s", cover_abs)
        return f"/static/uploads/covers/{cover_name}"

    logger.warning("ffmpeg 封面提取失败，视频: %s", video_abs)
    return None
