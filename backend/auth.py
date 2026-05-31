"""JWT 认证与密码管理工具。"""
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from . import models
from .database import get_db

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-please-change-in-prod-32chars")
ALGORITHM: str  = os.getenv("ALGORITHM", "HS256")
TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# auto_error=False：Bearer Token 不存在时不抛异常，允许 cookie 降级认证
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ── 密码工具 ────────────────────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码与哈希值是否匹配。"""
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def get_password_hash(password: str) -> str:
    """对密码进行 bcrypt 哈希处理。"""
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


# ── JWT 工具 ────────────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT 访问令牌。

    Args:
        data: 载荷数据，通常包含 {"sub": username}
        expires_delta: 自定义过期时长

    Returns:
        编码后的 JWT 字符串
    """
    payload = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=TOKEN_EXPIRE_MINUTES))
    payload.update({"exp": expire})
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """解码 JWT 令牌，失败返回 None。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── 依赖注入 ────────────────────────────────────────────────────────────────────

def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[models.User]:
    """从 Cookie 或 Bearer Token 中提取当前用户（不强制登录）。

    优先读取 HttpOnly Cookie 中的 access_token，
    Cookie 不存在时回退到 Authorization: Bearer 头。
    """
    active_token = request.cookies.get("access_token") or token
    if not active_token:
        return None
    payload = decode_token(active_token)
    if not payload:
        return None
    username: str = payload.get("sub", "")
    if not username:
        return None
    user = db.query(models.User).filter(models.User.username == username).first()
    return user if (user and user.is_active) else None


def require_user(
    current_user: Optional[models.User] = Depends(get_current_user),
) -> models.User:
    """强制要求已登录，未登录抛出 401。"""
    if not current_user:
        raise HTTPException(status_code=401, detail="请先登录")
    return current_user


def require_admin(
    current_user: Optional[models.User] = Depends(get_current_user),
) -> models.User:
    """强制要求管理员角色，未授权抛出 403。"""
    if not current_user:
        raise HTTPException(status_code=401, detail="请先登录")
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="权限不足")
    return current_user
