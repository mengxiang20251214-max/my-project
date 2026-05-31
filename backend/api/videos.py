"""视频 API（简化版，支持 url/upload 两种类型）。"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Video

router = APIRouter()


class VideoOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    video_url: Optional[str]
    video_file: Optional[str]
    video_type: str
    cover_url: Optional[str]
    category_id: Optional[int]
    category_name: Optional[str]
    views: int
    file_size: Optional[int]
    created_at: str

    class Config:
        from_attributes = True


def _out(v: Video) -> VideoOut:
    return VideoOut(
        id=v.id, title=v.title, description=v.description,
        video_url=v.video_url, video_file=v.video_file, video_type=v.video_type,
        cover_url=v.cover_url, category_id=v.category_id,
        category_name=v.category.name if v.category else None,
        views=v.views, file_size=v.file_size,
        created_at=v.created_at.strftime("%Y-%m-%d %H:%M"),
    )


@router.get("/videos", response_model=dict, tags=["videos"])
def list_videos(
    page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=50),
    category_id: Optional[int] = Query(None), db: Session = Depends(get_db),
):
    q = db.query(Video)
    if category_id:
        q = q.filter(Video.category_id == category_id)
    total = q.count()
    items = q.order_by(Video.created_at.desc()).offset((page-1)*page_size).limit(page_size).all()
    return {"total": total, "page": page,
            "pages": max(1, (total+page_size-1)//page_size),
            "items": [_out(v) for v in items]}


@router.get("/videos/search", response_model=dict, tags=["videos"])
def search_videos(
    q: str = Query(""), category_id: Optional[int] = Query(None),
    sort: str = Query("created_at"), page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50), db: Session = Depends(get_db),
):
    query = db.query(Video)
    if q.strip():
        query = query.filter(Video.title.ilike(f"%{q.strip()}%"))
    if category_id:
        query = query.filter(Video.category_id == category_id)
    query = query.order_by(Video.views.desc() if sort == "views" else Video.created_at.desc())
    total = query.count()
    items = query.offset((page-1)*page_size).limit(page_size).all()
    return {"total": total, "page": page,
            "pages": max(1, (total+page_size-1)//page_size),
            "items": [_out(v) for v in items]}


@router.get("/videos/{video_id}", response_model=VideoOut, tags=["videos"])
def get_video(video_id: int, db: Session = Depends(get_db)):
    v = db.query(Video).filter(Video.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="视频不存在")
    return _out(v)


@router.post("/videos/{video_id}/view", tags=["videos"])
def record_view(video_id: int, db: Session = Depends(get_db)):
    v = db.query(Video).filter(Video.id == video_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="视频不存在")
    v.views += 1
    db.commit()
    return {"views": v.views}
