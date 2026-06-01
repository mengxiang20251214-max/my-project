"""SQLAlchemy ORM 数据模型。"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from .database import Base


class User(Base):
    """管理员账号。"""
    __tablename__ = "users"

    id:            Mapped[int]  = mapped_column(Integer, primary_key=True, index=True)
    username:      Mapped[str]  = mapped_column(String(50), unique=True, index=True, nullable=False)
    email:         Mapped[str]  = mapped_column(String(120), unique=True, index=True, nullable=False)
    password_hash: Mapped[str]  = mapped_column(String(255), nullable=False)
    role:          Mapped[str]  = mapped_column(String(20), default="admin")
    is_active:     Mapped[bool] = mapped_column(Boolean, default=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    videos: Mapped[List["Video"]] = relationship("Video", back_populates="author")


class Category(Base):
    """视频分类。"""
    __tablename__ = "categories"

    id:   Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)

    videos: Mapped[List["Video"]] = relationship("Video", back_populates="category")


class Country(Base):
    """国家（二级菜单一级：国家，如 泰国/中国/日本）。"""
    __tablename__ = "countries"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name:       Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    slug:       Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    videos: Mapped[List["Video"]] = relationship("Video", back_populates="country")


class VideoType(Base):
    """视频类型/题材（二级菜单二级：动作/喜剧/恐怖）。

    注意：与 Video.video_type(url/upload) 完全不同，这里是内容题材。
    """
    __tablename__ = "video_types"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name:       Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    slug:       Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    videos: Mapped[List["Video"]] = relationship("Video", back_populates="vtype")


class Video(Base):
    """视频表：支持 URL 和本地上传两种来源。"""
    __tablename__ = "videos"

    id:          Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title:       Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # URL 来源（YouTube embed 或直链 mp4）
    video_url:   Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 本地上传文件路径（/static/uploads/videos/xxx.mp4）
    video_file:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # "url" = 使用 video_url, "upload" = 使用 video_file
    video_type:  Mapped[str] = mapped_column(String(10), default="url")
    cover_url:   Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    # 二级菜单：国家（一级）+ 题材类型（二级）
    country_id:  Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("countries.id"), nullable=True)
    type_id:     Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("video_types.id"), nullable=True)
    file_size:   Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 字节数
    user_id:     Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    views:       Mapped[int] = mapped_column(Integer, default=0)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    category: Mapped[Optional["Category"]]  = relationship("Category", back_populates="videos")
    country:  Mapped[Optional["Country"]]   = relationship("Country", back_populates="videos")
    vtype:    Mapped[Optional["VideoType"]] = relationship("VideoType", back_populates="videos")
    author:   Mapped[Optional["User"]]      = relationship("User", back_populates="videos")

    @property
    def playable_url(self) -> Optional[str]:
        """返回可播放的视频地址（优先本地文件）。"""
        if self.video_type == "upload":
            return self.video_file
        return self.video_url


class Banner(Base):
    """Banner 轮播表：顶部/左侧/右侧三个位置。"""
    __tablename__ = "banners"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title:      Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    image_url:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    link_url:   Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 媒体类型：image=普通图片  gif=动图  video=视频(mp4/webm)
    media_type: Mapped[str] = mapped_column(String(10), default="image")
    # top=顶部全宽轮播  left=左侧侧边栏  right=右侧侧边栏
    position:   Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    duration:   Mapped[int] = mapped_column(Integer, default=3000)  # autoplay 间隔 ms
    is_active:  Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SiteSetting(Base):
    """网站全局配置（键值对）。"""
    __tablename__ = "site_settings"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    key:        Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    value:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)
