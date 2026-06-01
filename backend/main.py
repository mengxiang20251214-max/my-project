"""
VideoHub Pro — 后端 JSON API（前后端分离版）
纯 REST/JSON，无模板渲染，前端由 blog-video-frontend 独立提供。
"""
import os, re, uuid, logging
from datetime import datetime
from typing import Optional, List

import aiofiles
from fastapi import FastAPI, Depends, Form, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import engine, get_db, Base
from .models import Video, Category, User, Banner, SiteSetting, Country, VideoType
from .auth import (get_current_user, require_admin,
                   verify_password, get_password_hash, create_access_token)
from .utils import extract_cover, save_banner_file, BANNER_ALLOWED_EXT
from .api import videos as videos_router
from .api import categories as categories_router
from .api import users as users_router

logger = logging.getLogger("videohub")

# ── 常量 ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "../static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
CHUNK_SIZE = 1024 * 1024
ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".flv"}

# ── 应用初始化 ────────────────────────────────────────────────────────────────
app = FastAPI(title="VideoHub Pro API", version="4.0.0",
              docs_url="/docs", redoc_url="/redoc")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境改为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(videos_router.router,     prefix="/api")
app.include_router(categories_router.router, prefix="/api")
app.include_router(users_router.router,      prefix="/api")


# ── Pydantic 输入模型 ─────────────────────────────────────────────────────────
class CategoryCreate(BaseModel):
    name: str

class ChangePassword(BaseModel):
    old_password: str
    new_password: str

class SettingsUpdate(BaseModel):
    site_name: Optional[str] = None
    site_description: Optional[str] = None
    site_keywords: Optional[str] = None
    footer_text: Optional[str] = None


# ── 启动事件 ─────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    for d in ["videos", "covers", "banners"]:
        os.makedirs(os.path.join(UPLOAD_DIR, d), exist_ok=True)
    db: Session = next(get_db())
    try:
        _seed_data(db)
        _seed_banners(db)
        _seed_taxonomy(db)
    finally:
        db.close()


def _migrate_db():
    from sqlalchemy import text, inspect as sa_inspect
    insp = sa_inspect(engine)
    if "videos" not in insp.get_table_names():
        return
    with engine.connect() as conn:
        video_cols = {c["name"] for c in insp.get_columns("videos")}
        OLD_COLS = {"likes", "status", "updated_at"}
        if OLD_COLS & video_cols:
            conn.execute(text("DROP TABLE IF EXISTS _videos_bak"))
            conn.execute(text("ALTER TABLE videos RENAME TO _videos_bak"))
            conn.execute(text("""
                CREATE TABLE videos (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       VARCHAR(200) NOT NULL,
                    description TEXT,
                    video_url   VARCHAR(500),
                    video_file  VARCHAR(500),
                    video_type  VARCHAR(10)  NOT NULL DEFAULT 'url',
                    cover_url   VARCHAR(500),
                    category_id INTEGER REFERENCES categories(id),
                    file_size   INTEGER,
                    user_id     INTEGER REFERENCES users(id),
                    views       INTEGER NOT NULL DEFAULT 0,
                    created_at  DATETIME
                )
            """))
            bak_cols = {c["name"] for c in insp.get_columns("_videos_bak")}
            sel_vfile = "video_file"                   if "video_file" in bak_cols else "NULL"
            sel_vtype = "COALESCE(video_type, 'url')"  if "video_type" in bak_cols else "'url'"
            sel_fsize = "file_size"                    if "file_size"  in bak_cols else "NULL"
            conn.execute(text(f"""
                INSERT INTO videos
                    (id, title, description, video_url, video_file, video_type,
                     cover_url, category_id, file_size, user_id, views, created_at)
                SELECT id, title, description, video_url,
                       {sel_vfile}, {sel_vtype}, cover_url, category_id,
                       {sel_fsize}, user_id, COALESCE(views, 0), created_at
                FROM _videos_bak
            """))
            conn.execute(text("DROP TABLE _videos_bak"))
        else:
            for col, ddl in [
                ("video_file", "ALTER TABLE videos ADD COLUMN video_file TEXT"),
                ("video_type", "ALTER TABLE videos ADD COLUMN video_type VARCHAR(10) DEFAULT 'url'"),
                ("file_size",  "ALTER TABLE videos ADD COLUMN file_size INTEGER"),
            ]:
                if col not in video_cols:
                    conn.execute(text(ddl))

        # videos：二级菜单新增列（国家 + 题材类型）
        video_cols = {c["name"] for c in sa_inspect(engine).get_columns("videos")}
        for col, ddl in [
            ("country_id", "ALTER TABLE videos ADD COLUMN country_id INTEGER"),
            ("type_id",    "ALTER TABLE videos ADD COLUMN type_id INTEGER"),
        ]:
            if col not in video_cols:
                conn.execute(text(ddl))

        # banners：新增 media_type 列
        if "banners" in sa_inspect(engine).get_table_names():
            banner_cols = {c["name"] for c in sa_inspect(engine).get_columns("banners")}
            if "media_type" not in banner_cols:
                conn.execute(text("ALTER TABLE banners ADD COLUMN media_type VARCHAR(10) DEFAULT 'image'"))

        conn.commit()


def _seed_data(db: Session):
    if db.query(User).count() > 0:
        return
    admin = User(username="admin", email="admin@example.com",
                 password_hash=get_password_hash("admin123"), role="admin")
    db.add(admin); db.flush()
    cats = {}
    for name, slug in [("科技","technology"),("教育","education"),
                       ("娱乐","entertainment"),("生活","lifestyle"),("游戏","gaming")]:
        c = Category(name=name, slug=slug)
        db.add(c); db.flush()
        cats[slug] = c
    for k, v in {"site_name":"VideoHub Pro","site_description":"专业视频博客平台",
                 "site_keywords":"视频,博客","footer_text":"© 2024 VideoHub Pro."}.items():
        db.add(SiteSetting(key=k, value=v))
    for title, url, cover, cat in [
        ("Python 全栈开发","https://www.youtube.com/embed/rfscVS0vtbw","https://picsum.photos/seed/py/640/360","technology"),
        ("JavaScript 现代特性","https://www.youtube.com/embed/W6NZfCO5SIk","https://picsum.photos/seed/js/640/360","technology"),
        ("东京 Vlog 樱花季","https://www.youtube.com/embed/GibiNy4d4gc","https://picsum.photos/seed/tokyo/640/360","lifestyle"),
        ("量子计算入门","https://www.youtube.com/embed/JhHMJCUmq28","https://picsum.photos/seed/quantum/640/360","education"),
        ("原神全攻略","https://www.youtube.com/embed/SFQkwAMYMBo","https://picsum.photos/seed/game/640/360","gaming"),
        ("法式料理食谱","https://www.youtube.com/embed/kFBMRxNFe1M","https://picsum.photos/seed/cook/640/360","lifestyle"),
        ("Docker 部署实战","https://www.youtube.com/embed/s_o8dwzRlu4","https://picsum.photos/seed/docker/640/360","technology"),
        ("2024 最佳电影","https://www.youtube.com/embed/ByXuk9QqQkk","https://picsum.photos/seed/movie/640/360","entertainment"),
        ("机器学习入门","https://www.youtube.com/embed/aircAruvnKk","https://picsum.photos/seed/ml/640/360","education"),
        ("冰岛极光之旅","https://www.youtube.com/embed/N-4CEb1UNcQ","https://picsum.photos/seed/ice/640/360","lifestyle"),
        ("Lo-fi 音乐制作","https://www.youtube.com/embed/jfKfPfyJRdk","https://picsum.photos/seed/music/640/360","entertainment"),
        ("街头艺术记录","https://www.youtube.com/embed/4XlTPGJmHFI","https://picsum.photos/seed/art/640/360","entertainment"),
    ]:
        db.add(Video(title=title, video_url=url, video_type="url",
                     cover_url=cover, category_id=cats[cat].id, user_id=admin.id))
    db.commit()


def _seed_banners(db: Session):
    if db.query(Banner).count() > 0:
        return
    for b in [
        Banner(position="top",  title="顶部广告 A", duration=4000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/top-a/1200/200", link_url="#"),
        Banner(position="top",  title="顶部广告 B", duration=4000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/top-b/1200/200", link_url="#"),
        Banner(position="left", title="左侧广告 A", duration=5000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/left-a/260/400", link_url="#"),
        Banner(position="left", title="左侧广告 B", duration=5000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/left-b/260/400", link_url="#"),
        Banner(position="right",title="右侧广告 A", duration=5000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/right-a/260/400",link_url="#"),
        Banner(position="right",title="右侧广告 B", duration=5000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/right-b/260/400",link_url="#"),
    ]:
        db.add(b)
    db.commit()


def _seed_taxonomy(db: Session):
    """种子：二级菜单的国家 + 视频题材类型。"""
    if db.query(Country).count() == 0:
        for i, (name, slug) in enumerate([
            ("泰国", "thailand"), ("中国", "china"), ("日本", "japan"),
            ("韩国", "korea"), ("美国", "usa"),
        ]):
            db.add(Country(name=name, slug=slug, sort_order=i))
    if db.query(VideoType).count() == 0:
        for i, (name, slug) in enumerate([
            ("动作", "action"), ("喜剧", "comedy"), ("恐怖", "horror"),
            ("爱情", "romance"), ("科幻", "scifi"), ("纪录", "documentary"),
        ]):
            db.add(VideoType(name=name, slug=slug, sort_order=i))
    db.commit()


# ── 文件上传工具 ──────────────────────────────────────────────────────────────
async def _save_video_file(file: UploadFile) -> tuple[str, int, str]:
    """流式保存视频文件，返回 (相对URL, 字节数, 绝对路径)。"""
    ext = os.path.splitext(file.filename or "video")[1].lower() or ".mp4"
    if ext not in ALLOWED_VIDEO_EXT:
        ext = ".mp4"
    stem = uuid.uuid4().hex
    filename = f"{stem}{ext}"
    videos_dir = os.path.join(UPLOAD_DIR, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    dest = os.path.abspath(os.path.join(videos_dir, filename))
    size = 0
    try:
        async with aiofiles.open(dest, "wb") as f:
            while chunk := await file.read(CHUNK_SIZE):
                await f.write(chunk)
                size += len(chunk)
    except Exception as exc:
        logger.exception("保存视频文件失败: %s", exc)
        if os.path.exists(dest):
            os.remove(dest)
        raise
    return f"/static/uploads/videos/{filename}", size, dest


def _fmt_size(n: Optional[int]) -> str:
    if not n: return ""
    if n < 1024: return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


def _video_dict(v: Video) -> dict:
    return {
        "id": v.id, "title": v.title, "description": v.description,
        "video_url": v.video_url, "video_file": v.video_file,
        "video_type": v.video_type,
        "cover_url": v.cover_url or f"https://picsum.photos/seed/{v.id}/640/360",
        "category_id": v.category_id,
        "category_name": v.category.name if v.category else None,
        "category_slug": v.category.slug if v.category else None,
        "views": v.views,
        "file_size": v.file_size,
        "file_size_str": _fmt_size(v.file_size),
        "created_at": v.created_at.strftime("%Y-%m-%d") if v.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 健康检查
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# 公开 API
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/public/banners", tags=["public"])
def public_banners(db: Session = Depends(get_db)):
    """获取所有启用的 Banner，按位置分组。"""
    rows = (db.query(Banner).filter(Banner.is_active == True)
            .order_by(Banner.sort_order).all())
    result: dict = {"top": [], "left": [], "right": []}
    for b in rows:
        if b.position in result:
            result[b.position].append({
                "id": b.id, "title": b.title,
                "image_url": b.image_url, "link_url": b.link_url,
                "media_type": b.media_type or "image",
                "duration": b.duration,
            })
    return result


@app.get("/api/public/settings", tags=["public"])
def public_settings(db: Session = Depends(get_db)):
    """获取网站公开配置。"""
    return {r.key: r.value for r in db.query(SiteSetting).all()}


# ══════════════════════════════════════════════════════════════════════════════
# 管理 API（需要 admin 角色 JWT）
# ══════════════════════════════════════════════════════════════════════════════

# ── 仪表盘 ───────────────────────────────────────────────────────────────────
@app.get("/api/admin/stats", tags=["admin"])
def admin_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    recent = (db.query(Video).order_by(Video.created_at.desc()).limit(6).all())
    return {
        "videos":     db.query(Video).count(),
        "categories": db.query(Category).count(),
        "banners":    db.query(Banner).filter(Banner.is_active == True).count(),
        "recent":     [_video_dict(v) for v in recent],
    }


# ── 视频管理 ─────────────────────────────────────────────────────────────────
@app.get("/api/admin/videos", tags=["admin"])
def admin_list_videos(
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=50),
    title: Optional[str] = Query(None),          # 标题模糊搜索
    category_id: Optional[int] = Query(None),    # 分类筛选
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = db.query(Video)
    if title and title.strip():
        q = q.filter(Video.title.ilike(f"%{title.strip()}%"))
    if category_id:
        q = q.filter(Video.category_id == category_id)
    total = q.count()
    videos = (q.order_by(Video.created_at.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": [_video_dict(v) for v in videos],
        "categories": [{"id": c.id, "name": c.name} for c in db.query(Category).all()],
    }


@app.post("/api/admin/videos/add", tags=["admin"])
async def admin_add_video(
    title:        str  = Form(""),
    video_type:   str  = Form("url"),
    video_url:    str  = Form(""),
    cover_url:    str  = Form(""),
    description:  str  = Form(""),
    category_id:  Optional[str] = Form(None),
    video_file:   Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    cat_id = int(category_id) if (category_id or "").strip().isdigit() else None
    final_url, final_file, file_size = None, None, None
    try:
        if video_type == "upload" and video_file and video_file.filename:
            ext = os.path.splitext(video_file.filename)[1].lower()
            if ext in ALLOWED_VIDEO_EXT:
                final_file, file_size, video_abs = await _save_video_file(video_file)
                if not title.strip():
                    title = os.path.splitext(video_file.filename)[0]
                if not cover_url.strip():
                    stem = os.path.splitext(os.path.basename(video_abs))[0]
                    auto = extract_cover(video_abs, os.path.join(UPLOAD_DIR, "covers"), stem)
                    if auto:
                        cover_url = auto
        else:
            final_url = video_url.strip() or None

        if not title.strip():
            title = "未命名视频"

        if not (final_url or final_file):
            raise HTTPException(400, "Please provide a video URL or upload a file")

        v = Video(
            title=title.strip(), video_url=final_url, video_file=final_file,
            video_type=video_type, cover_url=cover_url.strip() or None,
            description=description.strip() or None,
            category_id=cat_id, user_id=current_user.id, file_size=file_size,
        )
        db.add(v); db.commit(); db.refresh(v)
        return {"ok": True, "video": _video_dict(v)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Add video failed: %s", exc)
        raise HTTPException(500, f"Add video failed: {exc}")


@app.post("/api/admin/videos/batch", tags=["admin"])
async def admin_batch_upload(
    category_id:  Optional[str] = Form(None),
    title_prefix: str = Form(""),
    video_files:  List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    cat_id = int(category_id) if (category_id or "").strip().isdigit() else None
    added = []
    try:
        for i, vf in enumerate(video_files):
            if not vf.filename:
                continue
            ext = os.path.splitext(vf.filename)[1].lower()
            if ext not in ALLOWED_VIDEO_EXT:
                continue
            fp, fs, va = await _save_video_file(vf)
            base = os.path.splitext(vf.filename)[0]
            title = f"{title_prefix} {i+1}".strip() if title_prefix else base
            stem = os.path.splitext(os.path.basename(va))[0]
            cover = extract_cover(va, os.path.join(UPLOAD_DIR, "covers"), stem)
            v = Video(title=title, video_file=fp, video_type="upload",
                      cover_url=cover, category_id=cat_id,
                      user_id=current_user.id, file_size=fs)
            db.add(v)
            added.append(title)
        if added:
            db.commit()
        return {"ok": True, "count": len(added), "titles": added}
    except Exception as exc:
        logger.exception("Batch upload failed: %s", exc)
        raise HTTPException(500, f"Batch upload failed: {exc}")


@app.post("/api/admin/videos/{vid}/edit", tags=["admin"])
async def admin_edit_video(
    vid: int,
    title:       str = Form(...),
    video_type:  str = Form("url"),
    video_url:   str = Form(""),
    cover_url:   str = Form(""),
    description: str = Form(""),
    category_id: Optional[str] = Form(None),
    video_file:  Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    v = db.query(Video).filter(Video.id == vid).first()
    if not v:
        raise HTTPException(404, "Video not found")
    cat_id = int(category_id) if (category_id or "").strip().isdigit() else None
    try:
        v.title       = title.strip()
        v.description = description.strip() or None
        v.cover_url   = cover_url.strip() or None
        v.category_id = cat_id
        v.video_type  = video_type
        if video_type == "upload" and video_file and video_file.filename:
            ext = os.path.splitext(video_file.filename)[1].lower()
            if ext in ALLOWED_VIDEO_EXT:
                v.video_file, v.file_size, va = await _save_video_file(video_file)
                v.video_url = None
                if not v.cover_url:
                    stem = os.path.splitext(os.path.basename(va))[0]
                    auto = extract_cover(va, os.path.join(UPLOAD_DIR, "covers"), stem)
                    if auto:
                        v.cover_url = auto
        elif video_type == "url":
            v.video_url = video_url.strip() or None
        db.commit(); db.refresh(v)
        return {"ok": True, "video": _video_dict(v)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Edit video failed: %s", exc)
        raise HTTPException(500, f"Edit video failed: {exc}")


@app.delete("/api/admin/videos/{vid}", tags=["admin"])
def admin_delete_video(
    vid: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    v = db.query(Video).filter(Video.id == vid).first()
    if not v:
        raise HTTPException(404, "Video not found")
    if v.video_file and v.video_file.startswith("/static/"):
        local = os.path.join(BASE_DIR, "..", v.video_file.lstrip("/"))
        if os.path.isfile(local):
            os.remove(local)
    db.delete(v); db.commit()
    return {"ok": True}


# ── 分类管理 ─────────────────────────────────────────────────────────────────
@app.post("/api/admin/categories", tags=["admin"])
def admin_add_category(
    body: CategoryCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    slug = re.sub(r"[^\w\-]", "", re.sub(r"\s+", "-", body.name.lower().strip())) or "cat"
    if db.query(Category).filter(Category.slug == slug).first():
        raise HTTPException(409, "Category already exists")
    c = Category(name=body.name.strip(), slug=slug)
    db.add(c); db.commit(); db.refresh(c)
    return {"ok": True, "category": {"id": c.id, "name": c.name, "slug": c.slug}}


@app.delete("/api/admin/categories/{cid}", tags=["admin"])
def admin_del_category(
    cid: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(404, "Category not found")
    db.query(Video).filter(Video.category_id == cid).update({"category_id": None})
    db.delete(cat); db.commit()
    return {"ok": True}


# ── Banner 管理 ──────────────────────────────────────────────────────────────
@app.get("/api/admin/banners", tags=["admin"])
def admin_list_banners(
    pos: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    q = db.query(Banner)
    if pos in ("top", "left", "right"):
        q = q.filter(Banner.position == pos)
    banners = q.order_by(Banner.position, Banner.sort_order).all()
    return [{"id": b.id, "title": b.title, "image_url": b.image_url,
             "link_url": b.link_url, "position": b.position,
             "media_type": b.media_type or "image",
             "sort_order": b.sort_order, "duration": b.duration,
             "is_active": b.is_active,
             "created_at": b.created_at.strftime("%Y-%m-%d") if b.created_at else None}
            for b in banners]


def _resolve_banner_media(
    media_file: Optional[UploadFile],
    image_url: str,
    media_type: str,
) -> tuple[Optional[str], str]:
    """
    解析 Banner 媒体来源：优先上传文件，其次填写的 URL。
    返回 (最终 URL, media_type)。上传文件时 media_type 由扩展名自动判定。
    """
    if media_file and media_file.filename:
        ext = os.path.splitext(media_file.filename)[1].lower()
        if ext not in BANNER_ALLOWED_EXT:
            raise HTTPException(400, f"Unsupported banner file type: {ext or 'unknown'}")
        url, mt = save_banner_file(media_file, os.path.join(UPLOAD_DIR, "banners"))
        return url, mt
    url = (image_url or "").strip() or None
    mt = media_type if media_type in ("image", "gif", "video") else "image"
    return url, mt


@app.post("/api/admin/banners", tags=["admin"])
def admin_add_banner(
    position:   str = Form(...),
    title:      str = Form(""),
    image_url:  str = Form(""),
    link_url:   str = Form(""),
    media_type: str = Form("image"),
    sort_order: int = Form(0),
    duration:   int = Form(3000),
    media_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if position not in ("top", "left", "right"):
        raise HTTPException(400, "position must be top/left/right")
    try:
        url, mt = _resolve_banner_media(media_file, image_url, media_type)
        b = Banner(position=position, title=title.strip() or None,
                   image_url=url, link_url=link_url.strip() or None,
                   media_type=mt, sort_order=sort_order, duration=max(500, duration))
        db.add(b); db.commit(); db.refresh(b)
        return {"ok": True, "id": b.id, "image_url": url, "media_type": mt}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Add banner failed: %s", exc)
        raise HTTPException(500, f"Add banner failed: {exc}")


@app.post("/api/admin/banners/{bid}/edit", tags=["admin"])
def admin_edit_banner(
    bid: int,
    title:      str = Form(""),
    image_url:  str = Form(""),
    link_url:   str = Form(""),
    media_type: str = Form("image"),
    sort_order: int = Form(0),
    duration:   int = Form(3000),
    media_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    b = db.query(Banner).filter(Banner.id == bid).first()
    if not b:
        raise HTTPException(404, "Banner not found")
    try:
        url, mt = _resolve_banner_media(media_file, image_url, media_type)
        b.title      = title.strip() or None
        b.image_url  = url
        b.link_url   = link_url.strip() or None
        b.media_type = mt
        b.sort_order = sort_order
        b.duration   = max(500, duration)
        db.commit()
        return {"ok": True, "image_url": url, "media_type": mt}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Edit banner failed: %s", exc)
        raise HTTPException(500, f"Edit banner failed: {exc}")


@app.post("/api/admin/banners/{bid}/toggle", tags=["admin"])
def admin_toggle_banner(
    bid: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    b = db.query(Banner).filter(Banner.id == bid).first()
    if not b:
        raise HTTPException(404, "Banner not found")
    b.is_active = not b.is_active
    db.commit()
    return {"ok": True, "is_active": b.is_active}


@app.delete("/api/admin/banners/{bid}", tags=["admin"])
def admin_del_banner(
    bid: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    b = db.query(Banner).filter(Banner.id == bid).first()
    if not b:
        raise HTTPException(404, "Banner not found")
    db.delete(b); db.commit()
    return {"ok": True}


# ── 系统设置 ─────────────────────────────────────────────────────────────────
@app.get("/api/admin/settings", tags=["admin"])
def admin_get_settings(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    return {r.key: r.value for r in db.query(SiteSetting).all()}


@app.post("/api/admin/settings", tags=["admin"])
def admin_save_settings(
    body: SettingsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    for k, v in updates.items():
        row = db.query(SiteSetting).filter(SiteSetting.key == k).first()
        if row:
            row.value = v
        else:
            db.add(SiteSetting(key=k, value=v))
    db.commit()
    return {"ok": True}


# ── 修改密码 ─────────────────────────────────────────────────────────────────
@app.post("/api/admin/change-password", tags=["admin"])
def admin_change_password(
    body: ChangePassword,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not verify_password(body.old_password, current_user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    if body.new_password == body.old_password:
        raise HTTPException(400, "New password must differ from the current one")
    current_user.password_hash = get_password_hash(body.new_password)
    db.commit()
    return {"ok": True}
