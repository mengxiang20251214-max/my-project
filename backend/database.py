"""数据库连接与会话管理。"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./blog_video.db")

# 一些平台（含 Railway/Heroku）给的是旧式 postgres:// 前缀，
# SQLAlchemy 2.0 只认 postgresql:// —— 这里统一规范化，避免 "Can't load plugin" 报错。
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,   # 连接池取连接前先 ping，避免 Postgres 空闲连接被服务端掐断后报错
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """ORM 声明基类。"""
    pass


def get_db():
    """FastAPI 依赖：提供数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
