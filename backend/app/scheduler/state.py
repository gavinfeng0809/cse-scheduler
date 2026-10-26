from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    BoundaryState,
    Employee,
    Holiday,
    ScheduleAssignment,
    SchedulePeriod,
    Shift,
)


def night_limit(employee) -> int:
    """【F3】员工级夜班连班上限（未配置时用全局缺省）。"""
    value = getattr(employee, "night_max_consecutive", None)
    return max(1, int(value or settings.night_default_max_consecutive))


def night_rest_days(employee) -> int:
    """【F3】员工级"连班达标后强制休息天数"（0 表示不强制）。"""
    value = getattr(employee, "night_rest_days", None)
    return max(0, int(value if value is not None else settings.night_default_rest_days))


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def load_previous_boundary(
    db: Session,
    period: SchedulePeriod,
    employee: Employee,
):
    previous = db.scalar(
        select(SchedulePeriod)
        .where(
            SchedulePeriod.team_id == period.team_id,
            SchedulePeriod.end_date < period.start_date,
        )
        .order_by(SchedulePeriod.end_date.desc())
    )

    if not previous:
        return {
            "consecutive_work_days": 0,
            "consecutive_night_shifts": 0,
            "night_rest_remaining": 0,
            "last_shift_code": None,
        }

    state = db.scalar(
        select(BoundaryState).where(
            BoundaryState.period_id == previous.id,
            BoundaryState.employee_id == employee.id,
        )
    )

    if state:
        return {
            "consecutive_work_days": state.consecutive_work_days,
            "consecutive_night_shifts": state.consecutive_night_shifts,
            "night_rest_remaining": state.night_rest_remaining,
            "last_shift_code": state.last_shift_code,
        }

    return calculate_boundary_from_assignments(
        db,
        previous,
        employee,
    )


def calculate_boundary_from_assignments(
    db: Session,
    period: SchedulePeriod,
    employee: Employee,
):
    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    rows = db.scalars(
        select(ScheduleAssignment)
        .where(
            ScheduleAssignment.period_id == period.id,
            ScheduleAssignment.employee_id == employee.id,
        )
    ).all()

    work_by_date = {
        r.work_date: r
        for r in rows
        if r.status == "WORK" and r.shift_id
    }

    limit = night_limit(employee)
    rest_days = night_rest_days(employee)

    consecutive_work = 0
    consecutive_night = 0
    rest_remaining = 0
    last_shift_code = None

    # 【F3 重写】按日正序重放。原实现有两个确定性错误：
    #   1) 遇到非夜班班次就 break，导致"末尾 5 天白班"只算出连续 1 天；
    #   2) 遇到 OFF 立刻 break，导致"N N N OFF"（已消耗 1 天强制休息）
    #      被算成 night_rest_remaining=0，下周期首日就能排班。
    for d in daterange(period.start_date, period.end_date):
        if rest_remaining > 0:
            rest_remaining -= 1

        row = work_by_date.get(d)
        shift = shifts.get(row.shift_id) if row else None

        if shift is None:
            consecutive_work = 0
            consecutive_night = 0
            continue

        consecutive_work += 1
        last_shift_code = shift.code

        # 连班只统计 N（counts_as_night_streak）；Y 不计入
        if shift.counts_as_night_streak:
            consecutive_night += 1
            if consecutive_night >= limit:
                rest_remaining = rest_days
        else:
            consecutive_night = 0

    return {
        "consecutive_work_days": consecutive_work,
        "consecutive_night_shifts": consecutive_night,
        "night_rest_remaining": rest_remaining,
        "last_shift_code": last_shift_code,
    }


def generate_boundary_states(db: Session, period: SchedulePeriod):
    """下一阶段 ⑤：根据周期内排班正向模拟，为每个员工生成期末 Boundary State。

    - 历史导入只存工作日（稀疏），因此"当日无 WORK 记录"即视为 OFF；
    - rest_remaining 在 OFF 日递减；night_streak 达到 3 时置 3；
    - 期末快照即下一个周期 Solver 的 prefix 输入。
    返回生成的记录数。
    """
    employees = db.scalars(
        select(Employee).where(Employee.active.is_(True))
    ).all()

    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    rows = db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == period.id
        )
    ).all()

    work_by_emp_date = {
        (r.employee_id, r.work_date): r
        for r in rows
        if r.status == "WORK" and r.shift_id
    }

    holidays = {
        x.holiday_date: x
        for x in db.scalars(
            select(Holiday).where(Holiday.team_id == period.team_id)
        )
    }

    dates = list(daterange(period.start_date, period.end_date))

    # 幂等：先清掉旧快照
    db.execute(
        delete(BoundaryState).where(BoundaryState.period_id == period.id)
    )

    count = 0

    for e in employees:
        work_streak = 0
        night_streak = 0
        rest_remaining = 0
        last_shift_code = None
        work_units = Decimal("0")
        weekend_count = 0
        holiday_count = 0

        limit = night_limit(e)
        rest_days = night_rest_days(e)

        for d in dates:
            row = work_by_emp_date.get((e.id, d))

            # 【F3】强制休息窗口按自然日递减：上白班或上 Y 同样会消耗窗口
            #（原实现只在"当日完全没有记录"时递减，会把窗口拖长）
            if rest_remaining > 0:
                rest_remaining -= 1

            if not row:
                work_streak = 0

                # 【F3 修正 2026-09-21】连班块（>=3）结束即进入强制休息：
                # 本日是休息窗口的第 1 天，剩余 rest_days-1 天继续禁止上 N。
                # 原实现只在"连到个人上限"时才置 rest_remaining，于是上限为 4 的人
                # 做完 3 连块后剩余休息天数不会带到下周期（实测：员工乙 11/22 三连后
                # 跨月只休 2 天就上 O2）。
                if night_streak >= 3 and rest_remaining == 0 and rest_days > 0:
                    rest_remaining = rest_days - 1

                night_streak = 0
                continue

            shift = shifts.get(row.shift_id)

            if not shift:
                work_streak = 0
                night_streak = 0
                continue

            work_streak += 1

            # 【F3】连班只统计 N（counts_as_night_streak）；假日夜班 Y 不计入
            if shift.counts_as_night_streak:
                night_streak += 1
            else:
                # 【F3 修正】连班块（>=3）因上白班而结束：从次日起强制休息
                if night_streak >= 3 and rest_remaining == 0 and rest_days > 0:
                    rest_remaining = rest_days
                night_streak = 0

            # 【F3】按人配置的连班上限与强制休息天数
            if night_streak >= limit:
                rest_remaining = rest_days

            last_shift_code = shift.code
            work_units += shift.paid_hours

            holiday = holidays.get(d)
            if holiday and holiday.day_type == "HOLIDAY":
                holiday_count += 1
            elif d.weekday() >= 5:
                weekend_count += 1

        db.add(
            BoundaryState(
                period_id=period.id,
                employee_id=e.id,
                boundary_date=period.end_date,
                last_shift_code=last_shift_code,
                consecutive_work_days=work_streak,
                consecutive_night_shifts=night_streak,
                night_rest_remaining=rest_remaining,
                work_units=work_units,
                weekend_shift_count=weekend_count,
                holiday_shift_count=holiday_count,
            )
        )
        count += 1

    db.flush()
    return count
