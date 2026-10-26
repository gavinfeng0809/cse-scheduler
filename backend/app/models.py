import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def uuid4():
    return uuid.uuid4()


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(50), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    employee_no: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    legacy_tier: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_target_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    monthly_target_days: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    # 显示顺序：按用户上传表格的行序（小在前）；新增员工排最后
    sort_order: Mapped[int] = mapped_column(Integer, default=9999)
    # 【F3 员工级夜班策略】业务确认：有人 4 连休 3、有人 3 连休 3/2。
    # 连班上限与"达标后的强制休息天数"按人配置，不再是全局 3/3。
    # 初值由 scripts/derive_night_policy.py 按历史反推，人工确认后置 confirmed=True。
    night_max_consecutive: Mapped[int] = mapped_column(Integer, default=3)
    night_rest_days: Mapped[int] = mapped_column(Integer, default=3)
    night_policy_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("team_id", "employee_no"),
    )


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    crosses_midnight: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    is_night: Mapped[bool] = mapped_column(Boolean, default=False)
    # 【修复】文档原代码缺失此字段，但 solver 按 Shift.active.is_(True) 查询会直接 AttributeError
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 【F3】是否计入"夜班连班"：N=True；假日夜班 Y=False（业务确认：Y 不参与连班计数）。
    # 注意仍保留 is_night=True，因为 N/Y 都是 21:00–09:00，都要参与最低休息计算。
    counts_as_night_streak: Mapped[bool] = mapped_column(Boolean, default=False)


class EmployeeShiftPermission(Base):
    __tablename__ = "employee_shift_permissions"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shifts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)


class EmployeeLeave(Base):
    __tablename__ = "employee_leaves"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE")
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    leave_type: Mapped[str] = mapped_column(String(50), default="ANNUAL")
    status: Mapped[str] = mapped_column(String(30), default="APPROVED")
    reason: Mapped[str | None] = mapped_column(Text)


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    holiday_date: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(100))
    day_type: Mapped[str] = mapped_column(String(30))
    # 【2026-09-24】工资倍数：3 = 法定节假日（三倍），2 = 休息日/调休日（双倍），
    # 1 = 普通工作日（调休上班日）。用于"全年节假日加班"统计与界面维护。
    pay_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")


class SchedulePeriod(Base):
    __tablename__ = "schedule_periods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    dependency_status: Mapped[str] = mapped_column(String(30), default="VALID")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleAssignment(Base):
    __tablename__ = "schedule_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schedule_periods.id", ondelete="CASCADE")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    work_date: Mapped[date] = mapped_column(Date)
    shift_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shifts.id"))
    status: Mapped[str] = mapped_column(String(30), default="OFF")
    source: Mapped[str] = mapped_column(String(30), default="AUTO")
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("period_id", "employee_id", "work_date"),
    )


class BoundaryState(Base):
    __tablename__ = "schedule_boundary_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schedule_periods.id", ondelete="CASCADE")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))

    boundary_date: Mapped[date] = mapped_column(Date)

    last_shift_code: Mapped[str | None] = mapped_column(String(20))
    last_shift_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_shift_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    consecutive_work_days: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_night_shifts: Mapped[int] = mapped_column(Integer, default=0)
    night_rest_remaining: Mapped[int] = mapped_column(Integer, default=0)

    work_units: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    weekend_shift_count: Mapped[int] = mapped_column(Integer, default=0)
    holiday_shift_count: Mapped[int] = mapped_column(Integer, default=0)


class ScheduleLock(Base):
    __tablename__ = "schedule_locks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    period_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schedule_periods.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    work_date: Mapped[date] = mapped_column(Date)
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schedule_assignments.id"))
    reason: Mapped[str | None] = mapped_column(Text)


class ScheduleRevision(Base):
    __tablename__ = "schedule_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    period_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("schedule_periods.id"))
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSONB)


class EmployeePreference(Base):
    __tablename__ = "employee_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employees.id"))
    preference_date: Mapped[date | None] = mapped_column(Date)
    shift_code: Mapped[str | None] = mapped_column(String(20))
    preference_type: Mapped[str] = mapped_column(String(30))
    weight: Mapped[int] = mapped_column(Integer, default=10)


class ScheduleRule(Base):
    __tablename__ = "schedule_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    rule_key: Mapped[str] = mapped_column(String(100))
    rule_value: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="viewer")
    display_name: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before_data: Mapped[dict | None] = mapped_column(JSONB)
    after_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
