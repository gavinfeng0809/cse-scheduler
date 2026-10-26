"""用户与管理员管理接口（仅 admin 角色可用）。

- 列出用户、新增管理员/用户、改角色、停用/启用、重置密码；
- 支持"从员工添加"：选一名在职员工，自动用其邮箱作登录名、姓名作显示名；
- 安全护栏：不能删/停用自己；不能让系统失去最后一名在用管理员；密码至少 8 位。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import get_current_user, hash_password
from .database import get_db
from .models import AuditLog, Employee, User

ROLES = ("admin", "planner", "viewer")
ROLE_LABELS = {"admin": "管理员", "planner": "排班员", "viewer": "只读"}

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")


def require_admin(user: User = Depends(get_current_user)) -> User:
    if (user.role or "").lower() != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


router = APIRouter(dependencies=[Depends(require_admin)])


def _out(u: User) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name or u.username,
        "role": u.role,
        "role_label": ROLE_LABELS.get(u.role, u.role),
        "active": bool(u.active),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _active_admin_count(db: Session, exclude_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(User).where(
        User.role == "admin", User.active.is_(True)
    )
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int(db.scalar(stmt) or 0)


class UserIn(BaseModel):
    username: str | None = None
    password: str
    display_name: str | None = None
    role: str = "viewer"
    employee_id: str | None = None


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    role: str | None = None
    active: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    return [_out(u) for u in rows]


@router.get("/users/roles")
def list_roles():
    return [{"value": k, "label": v} for k, v in ROLE_LABELS.items()]


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db)):
    """新增用户/管理员。可只传 employee_id（自动取邮箱与姓名）。"""
    username = (body.username or "").strip()
    display_name = (body.display_name or "").strip() or None

    if body.employee_id:
        try:
            eid = uuid.UUID(body.employee_id)
        except ValueError:
            raise HTTPException(400, "非法 employee id")

        emp = db.get(Employee, eid)

        if not emp:
            raise HTTPException(404, "员工不存在")

        if not username:
            username = (emp.email or emp.employee_no or emp.name or "").strip()

        if not display_name:
            display_name = emp.name

    if not USERNAME_RE.match(username or ""):
        raise HTTPException(
            400, "登录名需为 3~64 位字母/数字/._@-（从员工添加时用其邮箱）"
        )

    if len(body.password or "") < 8:
        raise HTTPException(400, "密码至少 8 位")

    role = (body.role or "viewer").lower()

    if role not in ROLES:
        raise HTTPException(400, f"角色只允许 {'/'.join(ROLES)}")

    exists = db.scalar(select(User).where(User.username == username))

    if exists:
        raise HTTPException(400, f"登录名 {username} 已存在")

    user = User(
        username=username,
        password_hash=hash_password(body.password),
        role=role,
        display_name=display_name or username,
        active=True,
    )
    db.add(user)
    db.flush()

    db.add(
        AuditLog(
            action="CREATE_USER",
            entity_type="User",
            entity_id=user.id,
            after_data={"username": username, "role": role},
        )
    )
    db.commit()
    db.refresh(user)

    return _out(user)


@router.put("/users/{user_id}")
def update_user(user_id: str, body: UserUpdateIn, db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "非法 user id")

    user = db.get(User, uid)

    if not user:
        raise HTTPException(404, "用户不存在")

    before = {"role": user.role, "active": bool(user.active), "display_name": user.display_name}
    losing_admin = False

    if body.role is not None:
        role = body.role.lower()

        if role not in ROLES:
            raise HTTPException(400, f"角色只允许 {'/'.join(ROLES)}")

        if user.role == "admin" and role != "admin":
            losing_admin = True

        user.role = role

    if body.active is not None:
        if body.active is False and user.role == "admin":
            losing_admin = True

        user.active = body.active

    if losing_admin and _active_admin_count(db, exclude_id=user.id) == 0:
        db.rollback()
        raise HTTPException(400, "至少需要保留一名在用管理员，操作已拒绝")

    if body.display_name is not None:
        user.display_name = body.display_name.strip() or user.username

    db.add(
        AuditLog(
            action="UPDATE_USER",
            entity_type="User",
            entity_id=user.id,
            before_data=before,
            after_data={"role": user.role, "active": bool(user.active)},
        )
    )
    db.commit()

    return _out(user)


@router.put("/users/{user_id}/password")
def reset_password(user_id: str, body: ResetPasswordIn, db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "非法 user id")

    user = db.get(User, uid)

    if not user:
        raise HTTPException(404, "用户不存在")

    if len(body.new_password or "") < 8:
        raise HTTPException(400, "密码至少 8 位")

    user.password_hash = hash_password(body.new_password)

    db.add(
        AuditLog(
            action="RESET_PASSWORD",
            entity_type="User",
            entity_id=user.id,
            after_data={"username": user.username},
        )
    )
    db.commit()

    return {"status": "ok", "username": user.username}


@router.delete("/users/{user_id}")
def deactivate_user(
    user_id: str,
    me: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """停用用户（保留审计记录，不做物理删除）。"""
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "非法 user id")

    user = db.get(User, uid)

    if not user:
        raise HTTPException(404, "用户不存在")

    if user.id == me.id:
        raise HTTPException(400, "不能停用当前登录的账号")

    if user.role == "admin" and _active_admin_count(db, exclude_id=user.id) == 0:
        raise HTTPException(400, "至少需要保留一名在用管理员，操作已拒绝")

    user.active = False

    db.add(
        AuditLog(
            action="DEACTIVATE_USER",
            entity_type="User",
            entity_id=user.id,
            after_data={"username": user.username},
        )
    )
    db.commit()

    return {"status": "ok", "username": user.username, "active": False}
