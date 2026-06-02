"""
VideoHub Pro — 后端 JSON API（前后端分离版）
纯 REST/JSON，无模板渲染，前端由 blog-video-frontend 独立提供。
"""
import os, re, uuid, logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, List

import aiofiles
from fastapi import (FastAPI, Depends, Form, HTTPException, Query,
                     UploadFile, File, Response, BackgroundTasks)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from .database import engine, get_db, Base, SessionLocal
from .models import Video, Category, User, Banner, SiteSetting, Country, VideoType
from .auth import (get_current_user, require_admin,
                   verify_password, get_password_hash, create_access_token)
from .utils import extract_cover, save_banner_file, BANNER_ALLOWED_EXT
from .storage import STORAGE
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

# ── CORS 允许的前端来源 ──────────────────────────────────────────────────────────
# 显式白名单：本地开发用。生产域名通过下面的正则统一放行，避免改域名后忘了加白名单
# 导致整个前端被浏览器 CORS 拦截（曾经发生过：实际域名是 *-zeta-mocha.vercel.app，
# 而白名单里只写了 blog-frontend.vercel.app，导致线上前端一个接口都调不通）。
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5500",              # VSCode Live Server 默认端口
    "http://127.0.0.1:5500",
]
# 放行任意 Vercel 部署（正式 + 预览环境，如 blog-frontend-xxx.vercel.app）。
# 额外可通过环境变量 EXTRA_CORS_ORIGIN 追加一个自定义域名。
ALLOWED_ORIGIN_REGEX = r"https://([a-z0-9-]+\.)*vercel\.app"
_extra = os.getenv("EXTRA_CORS_ORIGIN")
if _extra:
    ALLOWED_ORIGINS.append(_extra.strip())

# ── 数据库自动备份调度 ────────────────────────────────────────────────────────
_scheduler = None


def _start_backup_scheduler():
    """按 BACKUP_* 环境变量启动每日自动备份（仅 Postgres + BACKUP_ENABLED 时）。"""
    global _scheduler
    if os.getenv("BACKUP_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "on"):
        logger.info("BACKUP_ENABLED 未开启，跳过自动备份调度")
        return
    from .backup_db import is_postgres, run_backup
    if not is_postgres():
        logger.info("当前不是 PostgreSQL，跳过自动备份调度")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        cron = os.getenv("BACKUP_SCHEDULE", "0 2 * * *")
        tz = os.getenv("BACKUP_TZ", "UTC")
        sched = BackgroundScheduler(timezone=tz)
        sched.add_job(run_backup, CronTrigger.from_crontab(cron, timezone=tz),
                      id="db_backup", replace_existing=True, misfire_grace_time=3600,
                      coalesce=True, max_instances=1)
        sched.start()
        _scheduler = sched
        logger.info("已启动数据库自动备份：cron='%s' tz=%s", cron, tz)
    except Exception as exc:
        logger.exception("启动备份调度失败: %s", exc)


def _stop_backup_scheduler():
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


# ── 应用初始化 ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表 / 迁移 / 播种种子数据（取代已弃用的 @app.on_event）。"""
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
    _start_backup_scheduler()
    yield
    _stop_backup_scheduler()


app = FastAPI(title="VideoHub Pro API", version="4.0.0",
              docs_url="/docs", redoc_url="/redoc", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,            # 本地开发白名单
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,  # 生产/预览：任意 *.vercel.app
    allow_credentials=False,                  # 用 Authorization 头鉴权，不依赖 cookie
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


# ── 启动迁移 / 种子 ──────────────────────────────────────────────────────────
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


# ── 文件上传工具（统一走 storage：本地磁盘 / R2）────────────────────────────────
async def _save_video_file(file: UploadFile) -> tuple[str, int, str]:
    """流式保存视频：先落到 STORAGE.temp_dir，再 persist 到存储。

    返回 (公开URL, 字节数, 本地可读路径)。本地路径供 ffmpeg 抽封面，
    用完后调用方需 STORAGE.release(local) 释放（本地存储 no-op，R2 删临时文件）。
    """
    ext = os.path.splitext(file.filename or "video")[1].lower() or ".mp4"
    if ext not in ALLOWED_VIDEO_EXT:
        ext = ".mp4"
    name = f"{uuid.uuid4().hex}{ext}"
    tmp = os.path.abspath(os.path.join(STORAGE.temp_dir, name))
    os.makedirs(STORAGE.temp_dir, exist_ok=True)
    size = 0
    try:
        async with aiofiles.open(tmp, "wb") as f:
            while chunk := await file.read(CHUNK_SIZE):
                await f.write(chunk)
                size += len(chunk)
    except Exception as exc:
        logger.exception("保存视频文件失败: %s", exc)
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    url, local = STORAGE.persist(tmp, f"videos/{name}", "video/mp4")
    return url, size, local


def _make_cover(video_local: str) -> Optional[str]:
    """从本地视频文件抽一帧封面，落库到存储，返回封面公开 URL（失败返回 None）。"""
    stem = os.path.splitext(os.path.basename(video_local))[0]
    jpg = extract_cover(video_local, STORAGE.temp_dir, stem)
    if not jpg:
        return None
    return STORAGE.save_file(jpg, f"covers/{os.path.basename(jpg)}", "image/jpeg")


def _needs_cover(v: Video) -> bool:
    """该视频是否「缺一张真正的封面」：本地上传、且没有自有封面（空 / 占位图）。"""
    if v.video_type != "upload":
        return False
    c = (v.cover_url or "").strip()
    return (not c) or ("picsum.photos" in c)


def _cover_bg(video_id: int, video_local: Optional[str] = None) -> None:
    """后台任务：为上传视频提取封面并写回 DB（独立 session）。

    video_local 来自 persist（批量上传时直接复用，省一次下载）；
    为 None 时（补全场景）通过 STORAGE.fetch_local 按 URL 取本地副本。
    """
    db = SessionLocal()
    fetched_temp = None
    try:
        v = db.query(Video).filter(Video.id == video_id).first()
        if not v or not v.video_file:
            return
        path = video_local
        if path is None:
            got = STORAGE.fetch_local(v.video_file)
            if not got:
                return
            path, is_temp = got
            if is_temp:
                fetched_temp = path
        url = _make_cover(path)
        if url:
            v.cover_url = url
            db.commit()
            logger.info("封面已生成 video_id=%s -> %s", video_id, url)
    except Exception as exc:
        logger.exception("后台封面提取失败 video_id=%s: %s", video_id, exc)
    finally:
        if video_local is not None:
            STORAGE.release(video_local)          # persist 给的本地路径
        elif fetched_temp and os.path.isfile(fetched_temp):
            os.remove(fetched_temp)               # fetch_local 下载的临时文件
        db.close()


def _is_safe_url(u: Optional[str]) -> bool:
    """只允许 http(s):// 绝对地址或 / 开头的相对路径，挡掉 javascript: 等 XSS 向量。"""
    if not u:
        return True                       # 空值合法（可选字段）
    u = u.strip().lower()
    return u.startswith(("http://", "https://", "/"))


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
def public_banners(response: Response, db: Session = Depends(get_db)):
    """获取所有启用的 Banner，按位置分组。"""
    # 公开数据变化不频繁，给 60s CDN/浏览器缓存，减轻首页每次访问的 DB 压力
    response.headers["Cache-Control"] = "public, max-age=60"
    rows = (db.query(Banner).filter(Banner.is_active == True)
            .order_by(Banner.position, Banner.sort_order).all())
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
def public_settings(response: Response, db: Session = Depends(get_db)):
    """获取网站公开配置。"""
    response.headers["Cache-Control"] = "public, max-age=60"
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
    recent = (db.query(Video).options(joinedload(Video.category))
              .order_by(Video.created_at.desc()).limit(6).all())
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
    videos = (q.options(joinedload(Video.category))
              .order_by(Video.created_at.desc())
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
                final_file, file_size, video_local = await _save_video_file(video_file)
                if not title.strip():
                    title = os.path.splitext(video_file.filename)[0]
                try:
                    if not cover_url.strip():
                        auto = _make_cover(video_local)   # 单个上传：同步抽封面，立即就绪
                        if auto:
                            cover_url = auto
                finally:
                    STORAGE.release(video_local)
        else:
            final_url = video_url.strip() or None
            if not _is_safe_url(final_url):
                raise HTTPException(400, "Invalid video URL (must start with http://, https:// or /)")

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
    background_tasks: BackgroundTasks,
    category_id:  Optional[str] = Form(None),
    title_prefix: str = Form(""),
    video_files:  List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    cat_id = int(category_id) if (category_id or "").strip().isdigit() else None
    added = []
    new_videos = []          # (Video, local_path) 供后台抽封面
    try:
        for i, vf in enumerate(video_files):
            if not vf.filename:
                continue
            ext = os.path.splitext(vf.filename)[1].lower()
            if ext not in ALLOWED_VIDEO_EXT:
                continue
            fp, fs, local = await _save_video_file(vf)
            base = os.path.splitext(vf.filename)[0]
            title = f"{title_prefix} {i+1}".strip() if title_prefix else base
            v = Video(title=title, video_file=fp, video_type="upload",
                      cover_url=None, category_id=cat_id,
                      user_id=current_user.id, file_size=fs)
            db.add(v)
            new_videos.append((v, local))
            added.append(title)
        if added:
            db.commit()
            # 封面提取改到后台：批量上传立即返回，封面随后逐个生成。
            # 复用 persist 留下的本地副本（local）省去重新下载；任务结束会 release。
            for v, local in new_videos:
                db.refresh(v)
                background_tasks.add_task(_cover_bg, v.id, local)
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
                old_file = v.video_file
                v.video_file, v.file_size, video_local = await _save_video_file(video_file)
                v.video_url = None
                try:
                    if not v.cover_url:
                        auto = _make_cover(video_local)
                        if auto:
                            v.cover_url = auto
                finally:
                    STORAGE.release(video_local)
                if old_file:                      # 换了新文件，删掉旧的，避免存储垃圾
                    STORAGE.delete(old_file)
        elif video_type == "url":
            nu = video_url.strip() or None
            if not _is_safe_url(nu):
                raise HTTPException(400, "Invalid video URL (must start with http://, https:// or /)")
            v.video_url = nu
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
    # 删除存储里的视频文件与自动提取的封面（外链 cover 不归我们管，storage 会忽略）
    if v.video_file:
        STORAGE.delete(v.video_file)
    if v.cover_url:
        STORAGE.delete(v.cover_url)
    db.delete(v); db.commit()
    return {"ok": True}


# ── 封面提取 / 补全 ───────────────────────────────────────────────────────────
@app.post("/api/admin/videos/{vid}/cover", tags=["admin"])
def admin_regenerate_cover(
    vid: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """为单个「上传」视频重新提取封面（同步，立即返回新封面 URL）。

    本地存储直接读文件；R2 存储会先把视频下载到临时文件再抽帧，用完即删。
    """
    v = db.query(Video).filter(Video.id == vid).first()
    if not v:
        raise HTTPException(404, "Video not found")
    if v.video_type != "upload" or not v.video_file:
        raise HTTPException(400, "Only uploaded videos support cover extraction")
    got = STORAGE.fetch_local(v.video_file)
    if not got:
        raise HTTPException(404, "Video file not found in storage")
    local, is_temp = got
    try:
        cover = _make_cover(local)
    finally:
        if is_temp and os.path.isfile(local):
            os.remove(local)
    if not cover:
        raise HTTPException(
            422, "Cover extraction failed (ffmpeg not installed or video unreadable)")
    v.cover_url = cover
    db.commit(); db.refresh(v)
    return {"ok": True, "cover_url": cover}


@app.post("/api/admin/covers/backfill", tags=["admin"])
def admin_backfill_covers(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """给所有「缺封面的上传视频」批量补全封面：后台逐个提取，接口立即返回任务数。"""
    pending = [v.id for v in db.query(Video).filter(Video.video_type == "upload").all()
               if _needs_cover(v)]
    for vid in pending:
        background_tasks.add_task(_cover_bg, vid)   # 无本地副本，任务内按 URL 取
    return {"ok": True, "scheduled": len(pending)}


# ── 数据库备份 ────────────────────────────────────────────────────────────────
@app.post("/api/admin/backup", tags=["admin"])
async def admin_run_backup(_: User = Depends(require_admin)):
    """手动触发一次数据库备份（导出 → 上传 R2 → 清理旧备份）。"""
    from starlette.concurrency import run_in_threadpool
    from .backup_db import run_backup
    res = await run_in_threadpool(run_backup)   # pg_dump 是阻塞调用，丢线程池里跑
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Backup failed"))
    return res


@app.get("/api/admin/backups", tags=["admin"])
def admin_list_backups(_: User = Depends(require_admin)):
    """列出 R2 上的全部数据库备份（按时间倒序）。"""
    from .backup_db import list_backups
    return {"items": list_backups()}


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


async def _resolve_banner_media(
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
        # 流式落到临时文件 → 交给存储层落库（本地 move / R2 上传）
        tmp, mt, file_ext = await save_banner_file(media_file, STORAGE.temp_dir)
        url = STORAGE.save_file(tmp, f"banners/{uuid.uuid4().hex}{file_ext}")
        return url, mt
    url = (image_url or "").strip() or None
    if not _is_safe_url(url):
        raise HTTPException(400, "Invalid media URL (must start with http://, https:// or /)")
    mt = media_type if media_type in ("image", "gif", "video") else "image"
    return url, mt


@app.post("/api/admin/banners", tags=["admin"])
async def admin_add_banner(
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
        link = link_url.strip() or None
        if not _is_safe_url(link):
            raise HTTPException(400, "Invalid link URL (must start with http://, https:// or /)")
        url, mt = await _resolve_banner_media(media_file, image_url, media_type)
        b = Banner(position=position, title=title.strip() or None,
                   image_url=url, link_url=link,
                   media_type=mt, sort_order=sort_order, duration=max(500, duration))
        db.add(b); db.commit(); db.refresh(b)
        return {"ok": True, "id": b.id, "image_url": url, "media_type": mt}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Add banner failed: %s", exc)
        raise HTTPException(500, f"Add banner failed: {exc}")


@app.post("/api/admin/banners/{bid}/edit", tags=["admin"])
async def admin_edit_banner(
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
        link = link_url.strip() or None
        if not _is_safe_url(link):
            raise HTTPException(400, "Invalid link URL (must start with http://, https:// or /)")
        old_media = b.image_url
        url, mt = await _resolve_banner_media(media_file, image_url, media_type)
        # 换了媒体（上传新文件或改了 URL）就删掉旧的存储文件
        if old_media and old_media != url:
            STORAGE.delete(old_media)
        b.title      = title.strip() or None
        b.image_url  = url
        b.link_url   = link
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


@app.post("/api/admin/banners/{bid}/move", tags=["admin"])
def admin_move_banner(
    bid: int,
    dir: str = Query(..., pattern="^(up|down)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """上移/下移 Banner：与同一位置相邻的那条交换 sort_order（服务端原子完成）。"""
    b = db.query(Banner).filter(Banner.id == bid).first()
    if not b:
        raise HTTPException(404, "Banner not found")
    # 同位置、按 (sort_order, id) 排好序的邻居
    siblings = (db.query(Banner).filter(Banner.position == b.position)
                .order_by(Banner.sort_order, Banner.id).all())
    idx = next((i for i, x in enumerate(siblings) if x.id == bid), None)
    swap_idx = idx - 1 if dir == "up" else idx + 1
    if idx is None or swap_idx < 0 or swap_idx >= len(siblings):
        return {"ok": True, "moved": False}   # 已经在顶/底，无需移动
    other = siblings[swap_idx]
    b.sort_order, other.sort_order = other.sort_order, b.sort_order
    # sort_order 相等时再用 id 兜底排序会失效，确保两者不同
    if b.sort_order == other.sort_order:
        other.sort_order += (1 if dir == "up" else -1)
    db.commit()
    return {"ok": True, "moved": True}


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
    if b.image_url:
        STORAGE.delete(b.image_url)          # 清掉存储里的 Banner 媒体文件
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
