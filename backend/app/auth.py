"""登录鉴权（JWT）：登录 / 当前用户 / 修改密码 + 业务 API 的认证依赖。

- 令牌：HS256 JWT，24 小时有效，载荷含用户 id/用户名/角色；
- 密码：bcrypt 加盐哈希（直接用 bcrypt 库，避免 passlib 与 bcrypt 4.x 的版本探测噪音）；
- 种子账号：由 init_data 创建（users 空表时建 admin，初始密码取
  环境变量 ADMIN_INITIAL_PASSWORD，必须显式提供）。
"""
from datetime import datetime, timedelta, timezone
import uuid

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User


router = APIRouter()

bearer = HTTPBearer(auto_error=False)

# 【2026-10-10】登录有效期与滑动续期：
#   原实现固定 24 小时且不续期，用户隔一天回来就被静默踢出登录。
#   现在：有效期 72 小时；只要在用系统，令牌剩余不足一半时自动换新（见 get_current_user），
#   因此正常使用不会掉线，只有连续 72 小时完全不用才需要重新登录。
TOKEN_HOURS = 72
TOKEN_REFRESH_BEFORE_HOURS = TOKEN_HOURS / 2


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_current_user(
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """业务 API 的认证依赖：无令牌/过期/伪造/停用一律 401。"""
    if credentials is None:
        raise HTTPException(401, "未登录")

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "无效令牌")

    user = db.get(User, uuid.UUID(payload["sub"]))

    if not user or not user.active:
        raise HTTPException(401, "账号不可用")

    # 【2026-10-10】滑动续期：剩余有效期不足一半时，顺手签发新令牌放进响应头，
    # 前端 api.ts 会自动更新本地令牌——只要在有效期内用过系统，就不会被踢出登录。
    exp = payload.get("exp")

    if exp:
        remaining = datetime.fromtimestamp(exp, tz=timezone.utc) - datetime.now(timezone.utc)

        if remaining < timedelta(hours=TOKEN_REFRESH_BEFORE_HOURS):
            response.headers["X-Refresh-Token"] = create_token(user)

    return user


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))

    if not user or not user.active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")

    return {
        "token": create_token(user),
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name or user.username,
    }


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name or user.username,
    }


@router.post("/auth/change-password")
def change_password(
    body: PasswordIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")

    if len(body.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")

    user.password_hash = hash_password(body.new_password)
    db.commit()

    return {"status": "ok"}
