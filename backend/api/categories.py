"""分类 API 路由（简化版）。"""
import re
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
    cats = db.query(Category).all()
    return [
        CategoryOut(id=c.id, name=c.name, slug=c.slug,
                    video_count=db.query(Video).filter(Video.category_id == c.id).count())
        for c in cats
    ]
