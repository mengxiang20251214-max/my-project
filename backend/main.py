"""
VideoHub Pro — 主应用（Banner 轮播 + 视频文件上传）
"""
import os, re, uuid, logging, subprocess, shutil
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger("videohub")

import aiofiles
from fastapi import FastAPI, Request, Depends, Form, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import engine, get_db, Base
from .models import Video, Category, User, Banner, SiteSetting
from .auth import get_current_user, verify_password, get_password_hash, create_access_token
from .utils import extract_cover
from .api import videos as videos_router
from .api import categories as categories_router
from .api import users as users_router

# ── 常量 ────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(__file__)
STATIC_DIR  = os.path.join(BASE_DIR, "../static")
UPLOAD_DIR  = os.path.join(STATIC_DIR, "uploads")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
CHUNK_SIZE  = 1024 * 1024          # 1 MB 分块读写
ALLOWED_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".flv"}

# ── 多语言翻译字典 ────────────────────────────────────────────────────────────────
TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh": {
        "home":"首页","categories":"分类","search":"搜索",
        "login":"登录","logout":"退出登录","admin_panel":"管理后台",
        "search_placeholder":"搜索视频标题...",
        "all_categories":"全部分类","latest_videos":"最新视频",
        "no_videos":"暂无视频","no_results":"未找到相关视频",
        "prev_page":"上一页","next_page":"下一页",
        "sort_by":"排序","sort_latest":"最新","sort_views":"最多播放",
        "clear_filter":"清除筛选","video_detail":"视频详情",
        "views":"次播放","username":"用户名","password":"密码",
        "login_btn":"登录","login_error":"用户名或密码错误",
        "welcome":"欢迎回来","login_subtitle":"登录管理员账号",
        "filter_category":"按分类筛选","keyword":"关键词",
    },
    "en": {
        "home":"Home","categories":"Categories","search":"Search",
        "login":"Login","logout":"Logout","admin_panel":"Admin",
        "search_placeholder":"Search video titles...",
        "all_categories":"All Categories","latest_videos":"Latest Videos",
        "no_videos":"No videos","no_results":"No results found",
        "prev_page":"Prev","next_page":"Next",
        "sort_by":"Sort","sort_latest":"Latest","sort_views":"Most Viewed",
        "clear_filter":"Clear","video_detail":"Video Detail",
        "views":"views","username":"Username","password":"Password",
        "login_btn":"Sign In","login_error":"Invalid credentials",
        "welcome":"Welcome Back","login_subtitle":"Sign in to admin",
        "filter_category":"Filter by Category","keyword":"Keyword",
    },
    "id": {
        "home":"Beranda","categories":"Kategori","search":"Cari",
        "login":"Masuk","logout":"Keluar","admin_panel":"Admin",
        "search_placeholder":"Cari judul video...",
        "all_categories":"Semua Kategori","latest_videos":"Video Terbaru",
        "no_videos":"Tidak ada video","no_results":"Tidak ditemukan",
        "prev_page":"Sebelumnya","next_page":"Berikutnya",
        "sort_by":"Urutan","sort_latest":"Terbaru","sort_views":"Terbanyak",
        "clear_filter":"Hapus Filter","video_detail":"Detail Video",
        "views":"tayangan","username":"Nama Pengguna","password":"Kata Sandi",
        "login_btn":"Masuk","login_error":"Nama/kata sandi salah",
        "welcome":"Selamat Datang","login_subtitle":"Masuk ke akun admin",
        "filter_category":"Filter Kategori","keyword":"Kata Kunci",
    },
}


def get_language(request: Request) -> str:
    # 优先读 cookie（由前端 JS 语言切换按钮写入）
    cookie_lang = request.cookies.get("lang", "")
    if cookie_lang in TRANSLATIONS:
        return cookie_lang
    accept = request.headers.get("accept-language", "zh")
    primary = accept.split(",")[0].split(";")[0].split("-")[0].lower().strip()
    return primary if primary in TRANSLATIONS else "zh"


# ── FastAPI 初始化 ────────────────────────────────────────────────────────────────
app = FastAPI(title="VideoHub Pro", version="3.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)

app.include_router(videos_router.router,     prefix="/api")
app.include_router(categories_router.router, prefix="/api")
app.include_router(users_router.router,      prefix="/api")


# ── 启动事件 ─────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    # 确保上传目录存在
    for d in ["videos", "covers"]:
        os.makedirs(os.path.join(UPLOAD_DIR, d), exist_ok=True)
    db: Session = next(get_db())
    try:
        _seed_data(db)
        _seed_banners(db)
        _ensure_settings(db)
    finally:
        db.close()


def _migrate_db():
    """
    将旧版数据库结构迁移到 v3 新结构。
    策略：
    - 若 videos 表含旧列（likes / status / updated_at），执行全量重建迁移，保留内容
    - 若仅缺少新列（video_file/video_type/file_size），用 ALTER TABLE 补充
    """
    from sqlalchemy import text, inspect as sa_inspect

    insp = sa_inspect(engine)
    if "videos" not in insp.get_table_names():
        return  # create_all 尚未建表，跳过

    with engine.connect() as conn:
        video_cols = {c["name"] for c in insp.get_columns("videos")}

        # 检测到旧版列（任意一个存在就认为是旧结构）
        OLD_COLS = {"likes", "status", "updated_at"}
        if OLD_COLS & video_cols:
            # ── 全量重建：备份 → 新建 → 迁移数据 → 删备份 ──────────────────
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

            # 旧表可能已经包含 video_file/video_type/file_size（前一轮 ALTER 添加的）
            bak_cols = {c["name"] for c in insp.get_columns("_videos_bak")}
            sel_vfile  = "video_file"                    if "video_file"  in bak_cols else "NULL"
            sel_vtype  = "COALESCE(video_type, 'url')"  if "video_type"  in bak_cols else "'url'"
            sel_fsize  = "file_size"                     if "file_size"   in bak_cols else "NULL"

            conn.execute(text(f"""
                INSERT INTO videos
                    (id, title, description, video_url, video_file, video_type,
                     cover_url, category_id, file_size, user_id, views, created_at)
                SELECT
                    id, title, description, video_url,
                    {sel_vfile},
                    {sel_vtype},
                    cover_url, category_id,
                    {sel_fsize},
                    user_id,
                    COALESCE(views, 0),
                    created_at
                FROM _videos_bak
            """))
            conn.execute(text("DROP TABLE _videos_bak"))
            logger.info("videos 表已从旧结构迁移完成")

        else:
            # ── 仅补充缺失的新列 ────────────────────────────────────────────
            for col, ddl in [
                ("video_file", "ALTER TABLE videos ADD COLUMN video_file TEXT"),
                ("video_type", "ALTER TABLE videos ADD COLUMN video_type VARCHAR(10) DEFAULT 'url'"),
                ("file_size",  "ALTER TABLE videos ADD COLUMN file_size INTEGER"),
            ]:
                if col not in video_cols:
                    conn.execute(text(ddl))
                    logger.info("videos 表新增列: %s", col)

        # site_settings: 补充 updated_at 列
        if "site_settings" in insp.get_table_names():
            ss_cols = {c["name"] for c in insp.get_columns("site_settings")}
            if "updated_at" not in ss_cols:
                conn.execute(text("ALTER TABLE site_settings ADD COLUMN updated_at DATETIME"))
                logger.info("site_settings 表新增列: updated_at")

        conn.commit()


def _seed_data(db: Session):
    if db.query(User).count() > 0:
        return
    admin = User(username="admin", email="admin@example.com",
                 password_hash=get_password_hash("admin123"), role="admin")
    db.add(admin)
    db.flush()

    cats = {}
    for name, slug in [("科技","technology"),("教育","education"),
                        ("娱乐","entertainment"),("生活","lifestyle"),("游戏","gaming")]:
        c = Category(name=name, slug=slug)
        db.add(c); db.flush()
        cats[slug] = c

    for k, v in {"site_name":"VideoHub Pro","site_title":"视频博客",
                  "site_description":"专业视频博客平台",
                  "site_keywords":"视频,博客","footer_text":"© 2024 VideoHub Pro.",
                  "site_icon":""}.items():
        db.add(SiteSetting(key=k, value=v))

    # 示例视频
    sample = [
        ("Python 全栈开发完整教程","https://www.youtube.com/embed/rfscVS0vtbw",
         "https://picsum.photos/seed/py/640/360","technology"),
        ("JavaScript 现代特性解析","https://www.youtube.com/embed/W6NZfCO5SIk",
         "https://picsum.photos/seed/js/640/360","technology"),
        ("日本东京 Vlog 樱花季","https://www.youtube.com/embed/GibiNy4d4gc",
         "https://picsum.photos/seed/tokyo/640/360","lifestyle"),
        ("量子计算技术革命","https://www.youtube.com/embed/JhHMJCUmq28",
         "https://picsum.photos/seed/quantum/640/360","education"),
        ("原神 4.4 版本全攻略","https://www.youtube.com/embed/SFQkwAMYMBo",
         "https://picsum.photos/seed/game/640/360","gaming"),
        ("法式料理家庭版食谱","https://www.youtube.com/embed/kFBMRxNFe1M",
         "https://picsum.photos/seed/cook/640/360","lifestyle"),
        ("Docker Kubernetes 部署实战","https://www.youtube.com/embed/s_o8dwzRlu4",
         "https://picsum.photos/seed/docker/640/360","technology"),
        ("2024 年最值得看的电影","https://www.youtube.com/embed/ByXuk9QqQkk",
         "https://picsum.photos/seed/movie/640/360","entertainment"),
        ("机器学习入门教程","https://www.youtube.com/embed/aircAruvnKk",
         "https://picsum.photos/seed/ml/640/360","education"),
        ("冰岛极光追逐之旅","https://www.youtube.com/embed/N-4CEb1UNcQ",
         "https://picsum.photos/seed/ice/640/360","lifestyle"),
        ("Lo-fi 音乐制作教学","https://www.youtube.com/embed/jfKfPfyJRdk",
         "https://picsum.photos/seed/music/640/360","entertainment"),
        ("街头艺术创作记录","https://www.youtube.com/embed/4XlTPGJmHFI",
         "https://picsum.photos/seed/art/640/360","entertainment"),
    ]
    for title, video_url, cover_url, cat_slug in sample:
        db.add(Video(title=title, video_url=video_url, video_type="url",
                     cover_url=cover_url, category_id=cats[cat_slug].id,
                     user_id=admin.id))
    db.commit()


def _ensure_settings(db: Session):
    """确保升级旧版时必要的配置键存在。"""
    defaults = {"site_title": "视频博客", "site_icon": ""}
    changed = False
    for k, v in defaults.items():
        if not db.query(SiteSetting).filter(SiteSetting.key == k).first():
            db.add(SiteSetting(key=k, value=v))
            changed = True
    if changed:
        db.commit()


def _seed_banners(db: Session):
    """Banner 示例数据——与用户种子独立，确保每个位置都有轮播内容。"""
    if db.query(Banner).count() > 0:
        return
    for b in [
        Banner(position="top",  title="顶部广告 A", duration=4000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/top-a/1200/200",  link_url="#"),
        Banner(position="top",  title="顶部广告 B", duration=4000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/top-b/1200/200",  link_url="#"),
        Banner(position="left", title="左侧广告 A", duration=5000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/left-a/260/400",  link_url="#"),
        Banner(position="left", title="左侧广告 B", duration=5000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/left-b/260/400",  link_url="#"),
        Banner(position="right",title="右侧广告 A", duration=5000, sort_order=0, is_active=True,
               image_url="https://picsum.photos/seed/right-a/260/400", link_url="#"),
        Banner(position="right",title="右侧广告 B", duration=5000, sort_order=1, is_active=True,
               image_url="https://picsum.photos/seed/right-b/260/400", link_url="#"),
    ]:
        db.add(b)
    db.commit()


# ── 工具函数 ─────────────────────────────────────────────────────────────────────

def _get_site_settings(db: Session) -> dict:
    return {r.key: r.value for r in db.query(SiteSetting).all()}


def _get_banners(db: Session) -> dict:
    """获取各位置所有激活 Banner，按 sort_order 排序。"""
    rows = (db.query(Banner)
            .filter(Banner.is_active == True)
            .order_by(Banner.sort_order)
            .all())
    result: dict = {"top": [], "left": [], "right": []}
    for b in rows:
        if b.position in result:
            result[b.position].append(b)
    return result


def _base_ctx(request: Request, db: Session, current_user=None) -> dict:
    lang = get_language(request)
    t = TRANSLATIONS.get(lang, TRANSLATIONS["zh"])
    settings = _get_site_settings(db)
    return {
        "request": request, "current_user": current_user,
        "categories": db.query(Category).all(),
        "banners": _get_banners(db),
        "t": t, "lang": lang, "settings": settings,
        "site_name": settings.get("site_name", "VideoHub Pro"),
    }


def _admin_ctx(request: Request, db: Session, current_user: User, **extra) -> dict:
    settings = _get_site_settings(db)
    return {"request": request, "current_user": current_user,
            "settings": settings, "site_name": settings.get("site_name","VideoHub Pro"), **extra}


def _require_admin(current_user, request: Request):
    if not current_user or current_user.role != "admin":
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=302)
    return None


async def _save_video_file(file: UploadFile) -> tuple[str, int, str]:
    """
    流式写入上传的视频文件。
    Returns: (相对URL, 字节数, 绝对路径)
    """
    ext = os.path.splitext(file.filename or "video")[1].lower() or ".mp4"
    if ext not in ALLOWED_VIDEO_EXT:
        ext = ".mp4"
    stem = uuid.uuid4().hex          # 纯主干，供封面提取使用
    filename = f"{stem}{ext}"
    videos_dir = os.path.join(UPLOAD_DIR, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    dest = os.path.abspath(os.path.join(videos_dir, filename))
    size = 0
    try:
        async with aiofiles.open(dest, "wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                await f.write(chunk)
                size += len(chunk)
    except Exception as exc:
        logger.exception("保存视频文件失败 dest=%s : %s", dest, exc)
        if os.path.exists(dest):
            os.remove(dest)
        raise
    return f"/static/uploads/videos/{filename}", size, dest


def _fmt_size(n: Optional[int]) -> str:
    if not n:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


# ── 健康检查 ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════════════════════
# 前台路由
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    page: int = Query(1, ge=1),
    category_id: Optional[int] = Query(None),
    sort: str = Query("created_at"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    PAGE_SIZE = 12
    q = db.query(Video)
    if category_id:
        q = q.filter(Video.category_id == category_id)
    sort_col = Video.views if sort == "views" else Video.created_at
    q = q.order_by(sort_col.desc())
    total = q.count()
    videos = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    selected_cat = db.query(Category).filter(Category.id == category_id).first() if category_id else None

    ctx = _base_ctx(request, db, current_user)
    ctx.update({"videos": videos, "page": page, "total": total,
                "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
                "category_id": category_id, "selected_cat": selected_cat, "sort": sort})
    return templates.TemplateResponse(request, "frontend/index.html", ctx)


@app.get("/video/{video_id}", response_class=HTMLResponse)
def video_detail(
    video_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    video.views += 1
    db.commit()
    related = (db.query(Video)
               .filter(Video.id != video_id, Video.category_id == video.category_id)
               .order_by(Video.created_at.desc()).limit(6).all()
               ) if video.category_id else []
    ctx = _base_ctx(request, db, current_user)
    ctx.update({"video": video, "related": related, "fmt_size": _fmt_size})
    return templates.TemplateResponse(request, "frontend/video.html", ctx)


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = Query(""),
    category_id: Optional[int] = Query(None),
    sort: str = Query("created_at"),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    PAGE_SIZE = 12
    query = db.query(Video)
    if q.strip():
        query = query.filter(Video.title.ilike(f"%{q.strip()}%"))
    if category_id:
        query = query.filter(Video.category_id == category_id)
    sort_col = Video.views if sort == "views" else Video.created_at
    query = query.order_by(sort_col.desc())
    total = query.count()
    videos = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    ctx = _base_ctx(request, db, current_user)
    ctx.update({"videos": videos, "q": q, "category_id": category_id, "sort": sort,
                "page": page, "total": total,
                "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)})
    return templates.TemplateResponse(request, "frontend/search.html", ctx)


@app.get("/category/{slug}", response_class=HTMLResponse)
def category_page(
    slug: str, request: Request, page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    cat = db.query(Category).filter(Category.slug == slug).first()
    if not cat:
        raise HTTPException(status_code=404)
    PAGE_SIZE = 12
    q = db.query(Video).filter(Video.category_id == cat.id).order_by(Video.created_at.desc())
    total = q.count()
    videos = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    ctx = _base_ctx(request, db, current_user)
    ctx.update({"category": cat, "videos": videos, "page": page, "total": total,
                "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)})
    return templates.TemplateResponse(request, "frontend/category.html", ctx)


# ── 认证 ────────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db)
    ctx["next"] = next
    return templates.TemplateResponse(request, "frontend/login.html", ctx)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...), password: str = Form(...), next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        ctx = _base_ctx(request, db)
        t = TRANSLATIONS.get(get_language(request), TRANSLATIONS["zh"])
        ctx.update({"error": t["login_error"], "next": next})
        return templates.TemplateResponse(request, "frontend/login.html", ctx, status_code=401)
    token = create_access_token({"sub": user.username})
    resp = RedirectResponse(url=next if next.startswith("/") else "/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, max_age=86400 * 7, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ════════════════════════════════════════════════════════════════════════════════
# 管理后台路由
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(
    request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    stats = {
        "videos":     db.query(Video).count(),
        "categories": db.query(Category).count(),
        "banners":    db.query(Banner).filter(Banner.is_active == True).count(),
    }
    recent = db.query(Video).order_by(Video.created_at.desc()).limit(6).all()
    ctx = _admin_ctx(request, db, current_user, stats=stats, recent=recent,
                     fmt_size=_fmt_size)
    return templates.TemplateResponse(request, "admin/dashboard.html", ctx)


# ── 视频管理 ─────────────────────────────────────────────────────────────────────

@app.get("/admin/videos", response_class=HTMLResponse)
def admin_videos(
    request: Request, page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    PAGE_SIZE = 15
    total = db.query(Video).count()
    videos = (db.query(Video).order_by(Video.created_at.desc())
              .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
    ctx = _admin_ctx(request, db, current_user,
        videos=videos, categories=db.query(Category).all(),
        page=page, total=total, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        fmt_size=_fmt_size)
    return templates.TemplateResponse(request, "admin/videos.html", ctx)


@app.post("/admin/videos/add")
async def admin_add_video(
    request: Request,
    title: str = Form(""),
    video_type: str = Form("url"),
    video_url: str = Form(""),
    cover_url: str = Form(""),
    description: str = Form(""),
    category_id: Optional[str] = Form(None),   # 用 str 接收，避免 Pydantic v2 空字符串报错
    video_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r

    # 安全转换 category_id：空字符串 → None
    cat_id: Optional[int] = int(category_id) if (category_id or "").strip().isdigit() else None

    final_url, final_file, file_size = None, None, None
    try:
        if video_type == "upload" and video_file and video_file.filename:
            ext = os.path.splitext(video_file.filename)[1].lower()
            if ext in ALLOWED_VIDEO_EXT:
                final_file, file_size, video_abs = await _save_video_file(video_file)
                if not title.strip():
                    title = os.path.splitext(video_file.filename)[0]
                # 未手动提供封面时，尝试自动提取
                if not cover_url.strip():
                    stem = os.path.splitext(os.path.basename(video_abs))[0]
                    auto_cov = extract_cover(
                        video_abs, os.path.join(UPLOAD_DIR, "covers"), stem
                    )
                    if auto_cov:
                        cover_url = auto_cov
        else:
            final_url = video_url.strip() or None

        if not title.strip():
            title = "未命名视频"

        if final_url or final_file:
            db.add(Video(
                title=title.strip(), video_url=final_url, video_file=final_file,
                video_type=video_type, cover_url=cover_url.strip() or None,
                description=description.strip() or None,
                category_id=cat_id, user_id=current_user.id, file_size=file_size,
            ))
            db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("添加视频失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"添加视频失败：{exc}")

    return RedirectResponse(url="/admin/videos", status_code=302)


@app.post("/admin/videos/batch")
async def admin_batch_upload(
    request: Request,
    category_id: Optional[str] = Form(None),   # str 接收，避免空字符串报错
    title_prefix: str = Form(""),
    video_files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """批量上传视频文件（每个文件创建一条视频记录）。"""
    r = _require_admin(current_user, request)
    if r: return r

    cat_id: Optional[int] = int(category_id) if (category_id or "").strip().isdigit() else None
    count = 0
    try:
        for i, vf in enumerate(video_files):
            if not vf.filename:
                continue
            ext = os.path.splitext(vf.filename)[1].lower()
            if ext not in ALLOWED_VIDEO_EXT:
                continue
            file_path, file_size, video_abs = await _save_video_file(vf)
            base_name = os.path.splitext(vf.filename)[0]
            title = f"{title_prefix} {i+1}".strip() if title_prefix else base_name
            # 批量上传时自动提取封面
            stem = os.path.splitext(os.path.basename(video_abs))[0]
            auto_cov = extract_cover(
                video_abs, os.path.join(UPLOAD_DIR, "covers"), stem
            )
            db.add(Video(
                title=title, video_file=file_path, video_type="upload",
                cover_url=auto_cov,
                category_id=cat_id, user_id=current_user.id, file_size=file_size,
            ))
            count += 1
        if count:
            db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("批量上传失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"批量上传失败：{exc}")

    return RedirectResponse(url="/admin/videos", status_code=302)


@app.post("/admin/videos/{vid}/edit")
async def admin_edit_video(
    vid: int, request: Request,
    title: str = Form(...),
    video_type: str = Form("url"),
    video_url: str = Form(""),
    cover_url: str = Form(""),
    description: str = Form(""),
    category_id: Optional[str] = Form(None),   # str 接收，避免空字符串报错
    video_file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r

    cat_id: Optional[int] = int(category_id) if (category_id or "").strip().isdigit() else None
    try:
        v = db.query(Video).filter(Video.id == vid).first()
        if v:
            v.title = title.strip()
            v.description = description.strip() or None
            v.cover_url = cover_url.strip() or None
            v.category_id = cat_id
            v.video_type = video_type
            if video_type == "upload" and video_file and video_file.filename:
                ext = os.path.splitext(video_file.filename)[1].lower()
                if ext in ALLOWED_VIDEO_EXT:
                    v.video_file, v.file_size, video_abs = await _save_video_file(video_file)
                    v.video_url = None
                    # 重新上传文件后，若无封面则自动提取
                    if not v.cover_url:
                        stem = os.path.splitext(os.path.basename(video_abs))[0]
                        auto_cov = extract_cover(
                            video_abs, os.path.join(UPLOAD_DIR, "covers"), stem
                        )
                        if auto_cov:
                            v.cover_url = auto_cov
            elif video_type == "url":
                v.video_url = video_url.strip() or None
            db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("编辑视频失败 vid=%s: %s", vid, exc)
        raise HTTPException(status_code=500, detail=f"编辑视频失败：{exc}")

    return RedirectResponse(url="/admin/videos", status_code=302)


@app.post("/admin/videos/{vid}/delete")
def admin_delete_video(
    vid: int, request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    v = db.query(Video).filter(Video.id == vid).first()
    if v:
        # 删除本地文件
        if v.video_file and v.video_file.startswith("/static/"):
            local = os.path.join(BASE_DIR, "..", v.video_file.lstrip("/"))
            if os.path.isfile(local):
                os.remove(local)
        db.delete(v)
        db.commit()
    return RedirectResponse(url="/admin/videos", status_code=302)


# ── 分类管理 ─────────────────────────────────────────────────────────────────────

@app.get("/admin/categories", response_class=HTMLResponse)
def admin_categories(
    request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    cats = db.query(Category).all()
    ctx = _admin_ctx(request, db, current_user, categories=cats,
        cat_counts={c.id: db.query(Video).filter(Video.category_id == c.id).count() for c in cats})
    return templates.TemplateResponse(request, "admin/categories.html", ctx)


@app.post("/admin/categories/add")
def admin_add_category(
    request: Request, name: str = Form(...), db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    slug = re.sub(r"[^\w\-]", "", re.sub(r"\s+", "-", name.lower().strip())) or "cat"
    if not db.query(Category).filter(Category.slug == slug).first():
        db.add(Category(name=name.strip(), slug=slug))
        db.commit()
    return RedirectResponse(url="/admin/categories", status_code=302)


@app.post("/admin/categories/{cid}/delete")
def admin_del_category(
    cid: int, request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    cat = db.query(Category).filter(Category.id == cid).first()
    if cat:
        db.query(Video).filter(Video.category_id == cid).update({"category_id": None})
        db.delete(cat); db.commit()
    return RedirectResponse(url="/admin/categories", status_code=302)


# ── Banner 管理 ──────────────────────────────────────────────────────────────────

@app.get("/admin/banners", response_class=HTMLResponse)
def admin_banners(
    request: Request,
    pos: str = Query(""),        # 位置筛选：top/left/right 或空=全部
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    q = db.query(Banner)
    if pos in ("top", "left", "right"):
        q = q.filter(Banner.position == pos)
    banners = q.order_by(Banner.position, Banner.sort_order).all()
    ctx = _admin_ctx(request, db, current_user, banners=banners, pos_filter=pos)
    return templates.TemplateResponse(request, "admin/banners.html", ctx)


@app.post("/admin/banners/add")
def admin_add_banner(
    request: Request,
    position:   str = Form(...),
    title:      str = Form(""),
    image_url:  str = Form(""),
    link_url:   str = Form(""),
    sort_order: int = Form(0),
    duration:   int = Form(3000),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    if position in ("top", "left", "right"):
        db.add(Banner(position=position, title=title.strip() or None,
                      image_url=image_url.strip() or None,
                      link_url=link_url.strip() or None,
                      sort_order=sort_order, duration=max(500, duration)))
        db.commit()
    return RedirectResponse(url="/admin/banners", status_code=302)


@app.post("/admin/banners/{bid}/edit")
def admin_edit_banner(
    bid: int, request: Request,
    title:      str = Form(""),
    image_url:  str = Form(""),
    link_url:   str = Form(""),
    sort_order: int = Form(0),
    duration:   int = Form(3000),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    b = db.query(Banner).filter(Banner.id == bid).first()
    if b:
        b.title      = title.strip() or None
        b.image_url  = image_url.strip() or None
        b.link_url   = link_url.strip() or None
        b.sort_order = sort_order
        b.duration   = max(500, duration)
        db.commit()
    return RedirectResponse(url="/admin/banners", status_code=302)


@app.post("/admin/banners/{bid}/toggle")
def admin_toggle_banner(
    bid: int, request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    b = db.query(Banner).filter(Banner.id == bid).first()
    if b:
        b.is_active = not b.is_active
        db.commit()
    return RedirectResponse(url="/admin/banners", status_code=302)


@app.post("/admin/banners/{bid}/delete")
def admin_del_banner(
    bid: int, request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    b = db.query(Banner).filter(Banner.id == bid).first()
    if b:
        db.delete(b); db.commit()
    return RedirectResponse(url="/admin/banners", status_code=302)


# ── 系统设置 ─────────────────────────────────────────────────────────────────────

@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request, db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
    saved: bool = Query(False),
):
    r = _require_admin(current_user, request)
    if r: return r
    return templates.TemplateResponse(request, "admin/settings.html",
                                      _admin_ctx(request, db, current_user, saved=saved))


@app.post("/admin/settings")
def admin_settings_save(
    request: Request,
    site_name: str = Form(""), site_title: str = Form(""),
    site_description: str = Form(""), site_keywords: str = Form(""),
    footer_text: str = Form(""),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    r = _require_admin(current_user, request)
    if r: return r
    for k, v in {
        "site_name": site_name, "site_title": site_title,
        "site_description": site_description,
        "site_keywords": site_keywords, "footer_text": footer_text,
    }.items():
        row = db.query(SiteSetting).filter(SiteSetting.key == k).first()
        if row:
            row.value = v
        else:
            db.add(SiteSetting(key=k, value=v))
    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=302)


@app.post("/admin/settings/favicon")
async def admin_favicon_upload(
    request: Request,
    favicon_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """上传网站图标，保存到 static/ 目录并记录到 site_settings。"""
    r = _require_admin(current_user, request)
    if r: return r
    ext = os.path.splitext(favicon_file.filename or "favicon")[1].lower()
    if ext not in {".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp"}:
        ext = ".ico"
    dest_name = f"favicon{ext}"
    dest_path = os.path.abspath(os.path.join(STATIC_DIR, dest_name))
    try:
        async with aiofiles.open(dest_path, "wb") as f:
            while True:
                chunk = await favicon_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                await f.write(chunk)
    except Exception as exc:
        logger.exception("保存 Favicon 失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"上传失败：{exc}")
    icon_url = f"/static/{dest_name}"
    row = db.query(SiteSetting).filter(SiteSetting.key == "site_icon").first()
    if row:
        row.value = icon_url
    else:
        db.add(SiteSetting(key="site_icon", value=icon_url))
    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=302)
