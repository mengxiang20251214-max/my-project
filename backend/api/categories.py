"""分类 API 路由（简化版）。"""
import re
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Category, Video
from ..auth import require_admin

router = APIRouter()


class CategoryOut(BaseModel):
    id: int
    name: str
    slug: str
    video_count: int = 0

    class Config:
        from_attributes = True


@router.get("/categories", response_model=List[CategoryOut], tags=["categories"])
def list_categories(db: Session = Depends(get_db)):
    # 单条聚合查询拿到每个分类的视频数，避免「N 个分类 → N 次 count」的 N+1
    counts = dict(
        db.query(Video.category_id, func.count(Video.id))
        .group_by(Video.category_id)
        .all()
    )
    cats = db.query(Category).order_by(Category.id).all()
    return [
        CategoryOut(id=c.id, name=c.name, slug=c.slug,
                    video_count=counts.get(c.id, 0))
        for c in cats
    ]
