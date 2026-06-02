"""
分片上传：支持任意大小视频。与现有一次性上传 / 批量上传并存，互不影响。

流程：
  POST /api/upload/init      → 创建 upload_id + 临时目录，返回分片大小
  POST /api/upload/chunk     → 接收单个分片（按 index 存为独立文件）
  GET  /api/upload/progress/{upload_id} → 已收分片数 / 总数 / 百分比
  POST /api/upload/complete  → 按序合并 → STORAGE 落库(R2/本地) → 抽封面 → 建 Video 记录

设计要点：
- 分片状态**全部落盘**（每片一个 `{index}.part` 文件 + `meta.json`），
  不依赖进程内存 → 天然支持乱序 / 并发上传，多 worker 共享同一容器文件系统也安全。
- 分片原子落地（先写 `.tmp` 再 `os.replace`），避免半截分片被当成完整片。
- upload_id 限定 32 位 hex，杜绝路径穿越。
- 超过 24h 未完成的上传目录在下次 init 时自动清理。
- complete 用同步 def，FastAPI 自动在线程池执行，合并大文件不阻塞事件循环。
"""
import json
import logging
import os
import re
import shutil
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Form, File, UploadFile, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from .models import Video, User
from .auth import require_admin
from .storage import STORAGE
from .utils import extract_cover

logger = logging.getLogger("videohub.chunk")
router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".flv"}
CHUNK_SIZE = 5 * 1024 * 1024          # 5MB/片（与前端一致）
COPY_BUF = 1024 * 1024                # 合并/落盘缓冲
ABANDON_TTL = 24 * 3600              # 超过 24h 未完成的上传目录会被清理
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _chunk_root() -> str:
    return os.path.join(STORAGE.temp_dir, "chunks")


def _safe_id(uid: str) -> str:
    if not uid or not _ID_RE.match(uid):
        raise HTTPException(400, "invalid upload_id")
    return uid


def _dir(uid: str) -> str:
    return os.path.join(_chunk_root(), uid)


def _meta_path(uid: str) -> str:
    return os.path.join(_dir(uid), "meta.json")


def _read_meta(uid: str) -> dict:
    p = _meta_path(uid)
    if not os.path.isfile(p):
        raise HTTPException(404, "upload not found or expired")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _count_parts(uid: str, total: int) -> int:
    d = _dir(uid)
    return sum(1 for i in range(total) if os.path.isfile(os.path.join(d, f"{i}.part")))


def _sweep_expired() -> None:
    """清理超过 TTL 仍未完成的上传目录（防止临时盘堆积）。"""
    root = _chunk_root()
    if not os.path.isdir(root):
        return
    now = time.time()
    for name in os.listdir(root):
        d = os.path.join(root, name)
        try:
            if os.path.isdir(d) and now - os.path.getmtime(d) > ABANDON_TTL:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _make_cover(video_local: str) -> Optional[str]:
    """从本地视频抽一帧封面落库，返回封面 URL（与 main._make_cover 同逻辑，避免循环依赖）。"""
    stem = os.path.splitext(os.path.basename(video_local))[0]
    jpg = extract_cover(video_local, STORAGE.temp_dir, stem)
    if not jpg:
        return None
    return STORAGE.save_file(jpg, f"covers/{os.path.basename(jpg)}", "image/jpeg")


# ── 接口 ──────────────────────────────────────────────────────────────────────
@router.post("/init")
def upload_init(
    filename: str = Form(""),
    total_chunks: int = Form(...),
    total_size: int = Form(0),
    _: User = Depends(require_admin),
):
    """初始化上传：创建临时目录 + 元信息，返回 upload_id。"""
    if total_chunks < 1:
        raise HTTPException(400, "total_chunks must be >= 1")
    _sweep_expired()
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_VIDEO_EXT:
        ext = ".mp4"
    uid = uuid.uuid4().hex
    os.makedirs(_dir(uid), exist_ok=True)
    meta = {
        "upload_id": uid, "filename": filename, "ext": ext,
        "total_chunks": int(total_chunks), "total_size": int(total_size),
        "created": time.time(),
    }
    with open(_meta_path(uid), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return {"ok": True, "upload_id": uid, "chunk_size": CHUNK_SIZE}


@router.post("/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    """接收单个分片，按 index 原子落地为独立文件。"""
    uid = _safe_id(upload_id)
    meta = _read_meta(uid)
    total = meta["total_chunks"]
    if chunk_index < 0 or chunk_index >= total:
        raise HTTPException(400, "chunk_index out of range")
    dest = os.path.join(_dir(uid), f"{chunk_index}.part")
    tmp = dest + ".tmp"
    try:
        with open(tmp, "wb") as f:
            while True:
                buf = await chunk.read(COPY_BUF)
                if not buf:
                    break
                f.write(buf)
        os.replace(tmp, dest)          # 原子替换，避免并发/中断留下半截分片
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    received = _count_parts(uid, total)
    return {"ok": True, "received": received, "total": total}


@router.get("/progress/{upload_id}")
def upload_progress(upload_id: str, _: User = Depends(require_admin)):
    """查询上传进度。"""
    uid = _safe_id(upload_id)
    meta = _read_meta(uid)
    total = meta["total_chunks"]
    received = _count_parts(uid, total)
    return {
        "upload_id": uid, "received": received, "total": total,
        "percent": round(received * 100 / total, 1) if total else 0,
        "complete": received >= total,
    }


@router.post("/complete")
def upload_complete(
    upload_id:   str = Form(...),
    title:       str = Form(""),
    cover_url:   str = Form(""),
    description: str = Form(""),
    category_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """合并所有分片 → 落库存储 → 抽封面 → 建 Video 记录。"""
    uid = _safe_id(upload_id)
    meta = _read_meta(uid)
    total = meta["total_chunks"]
    missing = [i for i in range(total) if not os.path.isfile(os.path.join(_dir(uid), f"{i}.part"))]
    if missing:
        raise HTTPException(400, f"missing chunks: {missing[:10]}{'…' if len(missing) > 10 else ''}")

    ext = meta.get("ext") or ".mp4"
    merged = os.path.join(STORAGE.temp_dir, f"{uid}{ext}")
    try:
        with open(merged, "wb") as out:
            for i in range(total):
                with open(os.path.join(_dir(uid), f"{i}.part"), "rb") as pf:
                    shutil.copyfileobj(pf, out, COPY_BUF)
        size = os.path.getsize(merged)

        url, local = STORAGE.persist(merged, f"videos/{uid}{ext}", "video/mp4")
        final_cover = cover_url.strip() or None
        try:
            if not final_cover:
                final_cover = _make_cover(local)
        finally:
            STORAGE.release(local)

        cat_id = int(category_id) if (category_id or "").strip().isdigit() else None
        ttl = title.strip() or os.path.splitext(meta.get("filename") or "")[0] or "未命名视频"
        v = Video(
            title=ttl, video_file=url, video_type="upload",
            cover_url=final_cover, description=description.strip() or None,
            category_id=cat_id, user_id=current_user.id, file_size=size,
        )
        db.add(v); db.commit(); db.refresh(v)
        shutil.rmtree(_dir(uid), ignore_errors=True)
        logger.info("分片上传完成 video_id=%s size=%d", v.id, size)
        return {"ok": True, "video": {
            "id": v.id, "title": v.title, "video_file": v.video_file,
            "cover_url": v.cover_url, "file_size": size,
        }}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("合并/落库失败 upload_id=%s: %s", uid, exc)
        if os.path.exists(merged):
            try:
                os.remove(merged)
            except OSError:
                pass
        raise HTTPException(500, f"complete failed: {exc}")
