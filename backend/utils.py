"""共享工具函数（封面提取等）。"""
import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger("videohub.utils")


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
