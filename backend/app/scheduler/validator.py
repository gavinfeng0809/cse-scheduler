import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..rules import load_rules
from .state import load_previous_boundary
from ..models import (
    Employee,
    Shift,
    ScheduleAssignment,
    SchedulePeriod,
    EmployeeShiftPermission,
    EmployeeLeave,
    Holiday,
)


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _night_limit(employee) -> int:
    """【F3】员工级夜班连班上限。"""
    value = getattr(employee, "night_max_consecutive", None)
    return max(1, int(value or settings.night_default_max_consecutive))


def _night_rest(employee) -> int:
    """【F3】员工级"连班达标后强制休息天数"。"""
    value = getattr(employee, "night_rest_days", None)
    return max(0, int(value if value is not None else settings.night_default_rest_days))


def shift_interval(work_date, shift):
    """班次转真实 DateTime 区间（文档 §51）。跨午夜班次结束时间在次日。"""
    start = datetime.combine(work_date, shift.start_time)

    end_date = work_date
    if shift.crosses_midnight:
        end_date += timedelta(days=1)

    end = datetime.combine(end_date, shift.end_time)
    return start, end


def validate_period(db: Session, period_id):
    if isinstance(period_id, str):
        period_id = uuid.UUID(period_id)

    period = db.get(SchedulePeriod, period_id)

    if not period:
        return {
            "status": "FAIL",
            "errors": ["period not found"],
            "warnings": [],
        }

    # 【2026-09-22】读取可配置规则（与 solver 同源）
    rules = load_rules(db, period.team_id)
    settings.max_consecutive_work_days = int(rules["max_consecutive_work_days"])
    settings.minimum_rest_hours = int(rules["minimum_rest_hours"])
    settings.night_default_max_consecutive = int(rules["night_default_max_consecutive"])
    settings.night_default_rest_days = int(rules["night_default_rest_days"])
    settings.night_rest_allows_holiday_night = bool(rules["night_rest_allows_holiday_night"])
    settings.night_rest_allows_day = bool(rules["night_rest_allows_day"])

    employees = db.scalars(
        select(Employee).where(Employee.active.is_(True))
    ).all()

    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    assignments = db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == period.id
        )
    ).all()

    by_day = defaultdict(list)
    by_employee = defaultdict(dict)

    for row in assignments:
        by_day[row.work_date].append(row)
        by_employee[row.employee_id][row.work_date] = row

    permissions = defaultdict(set)

    for p in db.scalars(
        select(EmployeeShiftPermission).where(
            EmployeeShiftPermission.allowed.is_(True)
        )
    ):
        permissions[p.employee_id].add(p.shift_id)

    holidays = {
        x.holiday_date: x
        for x in db.scalars(
            select(Holiday).where(
                Holiday.team_id == period.team_id
            )
        )
    }

    # 【2026-10-26】本周期内的「指定休息 / 请假」
    leave_rows = db.scalars(
        select(EmployeeLeave).where(
            EmployeeLeave.status == "APPROVED",
            EmployeeLeave.start_date <= period.end_date,
            EmployeeLeave.end_date >= period.start_date,
        )
    ).all()

    leaves_by_emp: dict = {}

    for lv in leave_rows:
        leaves_by_emp.setdefault(lv.employee_id, []).append(lv)

    errors = []
    warnings = []

    # Coverage
    for d in daterange(period.start_date, period.end_date):
        holiday = holidays.get(d)

        if holiday and holiday.day_type == "HOLIDAY":
            # 节假日精确覆盖：C 恰好 3、Y 恰好 2，且不允许其它班次
            for code, required_count in rules["coverage_holiday"].items():
                actual = sum(
                    1
                    for row in by_day[d]
                    if row.shift_id
                    and row.shift_id in shifts
                    and shifts[row.shift_id].code == code
                )

                if actual != required_count:
                    errors.append(
                        {
                            "date": str(d),
                            "rule": f"{code}_COVERAGE",
                            "required": required_count,
                            "actual": actual,
                        }
                    )

            extra = sum(
                1
                for row in by_day[d]
                if row.shift_id
                and row.shift_id in shifts
                and shifts[row.shift_id].code not in ("C", "Y")
            )

            if extra:
                errors.append(
                    {
                        "date": str(d),
                        "rule": "HOLIDAY_EXTRA_SHIFT",
                        "actual": extra,
                    }
                )

            continue

        if holiday and holiday.day_type == "WORKDAY_TRANSFER":
            # 【业务口径】只有非 O1 班次有人数硬要求；O1 是弹性补充班次
            required = dict(rules["coverage_workday"])
        elif d.weekday() >= 5:
            # 周末精确覆盖：D 恰好 2、N 恰好 2，且不允许其它班次
            for code, required_count in rules["coverage_weekend"].items():
                actual = sum(
                    1
                    for row in by_day[d]
                    if row.shift_id
                    and row.shift_id in shifts
                    and shifts[row.shift_id].code == code
                )

                if actual != required_count:
                    errors.append(
                        {
                            "date": str(d),
                            "rule": f"{code}_COVERAGE",
                            "required": required_count,
                            "actual": actual,
                        }
                    )

            extra = sum(
                1
                for row in by_day[d]
                if row.shift_id
                and row.shift_id in shifts
                and shifts[row.shift_id].code not in ("D", "N")
            )

            if extra:
                errors.append(
                    {
                        "date": str(d),
                        "rule": "WEEKEND_EXTRA_SHIFT",
                        "actual": extra,
                    }
                )

            continue
        else:
            # 【业务口径】只有非 O1 班次有人数硬要求；O1 是弹性补充班次
            required = dict(rules["coverage_workday"])

        for code, minimum in required.items():
            actual = sum(
                1
                for row in by_day[d]
                if row.shift_id
                and row.shift_id in shifts
                and shifts[row.shift_id].code == code
            )

            if actual < minimum:
                errors.append(
                    {
                        "date": str(d),
                        "rule": f"{code}_COVERAGE",
                        "required": minimum,
                        "actual": actual,
                    }
                )

    # Permission
    for row in assignments:
        if row.shift_id and row.shift_id in shifts and row.shift_id not in permissions[row.employee_id]:
            errors.append(
                {
                    "date": str(row.work_date),
                    "rule": "PERMISSION",
                    "employee_id": str(row.employee_id),
                    "shift": shifts[row.shift_id].code,
                }
            )

    # 逐员工序列检查：连续工作 / 连续夜班 / 三连夜休息 / 真实最低休息
    for e in employees:
        dates = list(
            daterange(period.start_date, period.end_date)
        )

        flags = []  # (date, is_work, row)

        for d in dates:
            row = by_employee[e.id].get(d)
            is_work = bool(
                row and row.status == "WORK" and row.shift_id and row.shift_id in shifts
            )
            flags.append((d, is_work, row))

        # 【2026-10-26】指定休息 / 请假：这些天不允许排班
        for lv in leaves_by_emp.get(e.id, []):
            cur = max(lv.start_date, period.start_date)
            last = min(lv.end_date, period.end_date)
            while cur <= last:
                row = by_employee[e.id].get(cur)
                if row and row.status == "WORK":
                    errors.append(
                        {
                            "date": str(cur),
                            "rule": "LEAVE_VIOLATION",
                            "employee_id": str(e.id),
                            "employee": e.name,
                            "leave_type": lv.leave_type,
                        }
                    )
                cur = cur + timedelta(days=1)

        work_streak = 0
        night_streak = 0

        for d, is_work, row in flags:
            if not is_work:
                work_streak = 0
                night_streak = 0
                continue

            work_streak += 1

            # 【F3】连班只统计 N（counts_as_night_streak）；假日夜班 Y 不计入
            is_streak_night = shifts[row.shift_id].counts_as_night_streak

            if is_streak_night:
                night_streak += 1
            else:
                night_streak = 0

            if work_streak > settings.max_consecutive_work_days:
                errors.append(
                    {
                        "date": str(d),
                        "rule": "MAX_CONSECUTIVE_WORK",
                        "employee_id": str(e.id),
                        "employee": e.name,
                    }
                )

            if night_streak > _night_limit(e):
                errors.append(
                    {
                        "date": str(d),
                        "rule": "MAX_CONSECUTIVE_NIGHT",
                        "employee_id": str(e.id),
                        "employee": e.name,
                    }
                )

        # 【F3】连续 limit 个 N 之后必须有 rest 天"允许的休息状态"
        #（默认：休 或 假日夜班 Y；Y 不计入连班，业务确认）
        limit_e = _night_limit(e)
        rest_e = _night_rest(e)

        def _is_streak_night(row):
            return bool(
                row and row.status == "WORK" and row.shift_id in shifts
                and shifts[row.shift_id].counts_as_night_streak
            )

        # 【跨月修复 2026-09-21 通用化】上月已连 p 个 N：本周期续 t 个 N 使
        # 合计 p+t >= 3 后结束，则随后 rest 天必须"休 或 Y"
        prev_state = load_previous_boundary(db, period, e)
        cn_prev = min(prev_state.get("consecutive_night_shifts", 0) or 0, limit_e)
        p_prev = cn_prev

        if rest_e > 0:
            for t in range(max(0, 3 - p_prev), (limit_e - p_prev) + 1):
                if t >= len(flags):
                    break

                if not all(_is_streak_night(row) for _d, _w, row in flags[:t]):
                    continue

                if _is_streak_night(flags[t][2]):
                    continue  # 块仍在延续

                for j in range(t, min(len(flags), t + rest_e)):
                    d, is_work, row = flags[j]

                    if not is_work:
                        continue

                    sh = shifts[row.shift_id]

                    if sh.counts_as_night_streak:
                        allowed = False
                    elif sh.is_night:
                        allowed = bool(settings.night_rest_allows_holiday_night)
                    else:
                        allowed = bool(settings.night_rest_allows_day)

                    if not allowed:
                        errors.append(
                            {
                                "date": str(d),
                                "rule": "NIGHT_REST_AFTER_CROSS_BLOCK",
                                "employee_id": str(e.id),
                                "employee": e.name,
                            }
                        )

        # 【跨月修复】上月留下的强制休息天数：本月前 rest_remaining 天必须"休 或 Y"
        rest_prev = prev_state.get("night_rest_remaining", 0) or 0

        if rest_prev > 0:
            for j in range(min(rest_prev, len(flags))):
                d, is_work, row = flags[j]

                if not is_work:
                    continue

                sh = shifts[row.shift_id]

                if sh.counts_as_night_streak:
                    allowed = False
                elif sh.is_night:
                    allowed = bool(settings.night_rest_allows_holiday_night)
                else:
                    allowed = bool(settings.night_rest_allows_day)

                if not allowed:
                    errors.append(
                        {
                            "date": str(d),
                            "rule": "NIGHT_REST_CARRIED",
                            "employee_id": str(e.id),
                            "employee": e.name,
                        }
                    )

        # 【跨月修复】连续上班天数必须跨月连续：上月已连上 prev_work 天，
        # 本月前 (7-prev_work+1) 天里至少要休 1 天
        prev_work = prev_state.get("consecutive_work_days", 0) or 0
        allow_more = max(0, settings.max_consecutive_work_days - prev_work)

        if prev_work > 0 and allow_more + 1 <= len(flags):
            head_window = flags[:allow_more + 1]

            if all(is_work for _d, is_work, _row in head_window):
                errors.append(
                    {
                        "date": str(head_window[-1][0]),
                        "rule": "MAX_CONSECUTIVE_WORK_CROSS",
                        "employee_id": str(e.id),
                        "employee": e.name,
                    }
                )

        # 【跨月修复】上月最后一天是 O2 / D：本月首日不得再排同一班次
        last_code_prev = prev_state.get("last_shift_code")

        if last_code_prev in ("O2", "D") and flags:
            d0, is_work0, row0 = flags[0]

            if is_work0 and shifts[row0.shift_id].code == last_code_prev:
                errors.append(
                    {
                        "date": str(d0),
                        "rule": "SAME_SHIFT_CHAIN_CROSS",
                        "employee_id": str(e.id),
                        "employee": e.name,
                    }
                )

        if rest_e > 0:
            # 【修复】与 solver 保持一致：只要求夜班块完整，休息窗口截断到周期末尾
            # （否则周期末尾的夜班块之后几天校验不出来）
            for i in range(len(flags) - limit_e + 1):
                window = flags[i:i + limit_e]
                rest_window = flags[i + limit_e:i + limit_e + rest_e]

                if not all(_is_streak_night(row) for _d, _w, row in window):
                    continue

                for d, is_work, row in rest_window:
                    if not is_work:
                        continue

                    shift = shifts[row.shift_id]

                    if shift.counts_as_night_streak:
                        allowed = False
                    elif shift.is_night:
                        allowed = bool(settings.night_rest_allows_holiday_night)
                    else:
                        allowed = bool(settings.night_rest_allows_day)

                    if not allowed:
                        errors.append(
                            {
                                "date": str(d),
                                "rule": "NIGHT_REST_AFTER_BLOCK",
                                "employee_id": str(e.id),
                                "employee": e.name,
                            }
                        )

        # 真实最低休息时间（文档 §50/§51）：相邻两个班次的真实时间间隔
        prev_end = None
        prev_date = None

        for d, is_work, row in flags:
            if not is_work:
                prev_end = None
                prev_date = None
                continue

            shift = shifts[row.shift_id]
            start, end = shift_interval(d, shift)

            if prev_end is not None:
                rest_hours = (start - prev_end).total_seconds() / 3600

                if rest_hours < settings.minimum_rest_hours:
                    errors.append(
                        {
                            "date": str(d),
                            "rule": "REST_HOURS",
                            "employee_id": str(e.id),
                            "employee": e.name,
                            "rest": round(rest_hours, 1),
                            "required": settings.minimum_rest_hours,
                            "from": str(prev_date),
                        }
                    )

            prev_end = end
            prev_date = d

        # 【2026-09-24 新规则】单月夜班次数上限（只对"有夜班权限"的员工生效）
        month_cap = int(rules.get("night_month_max_shifts") or 0)

        if month_cap > 0:
            count_holiday_night = bool(rules.get("night_month_count_holiday_night"))
            cap_shift_ids = {
                sid
                for sid, s in shifts.items()
                if (s.is_night if count_holiday_night else s.counts_as_night_streak)
            }
            has_night_permission = any(
                sid in permissions[e.id]
                for sid, s in shifts.items()
                if s.is_night
            )
            night_count = sum(
                1
                for _d, is_work, row in flags
                if is_work and row is not None and row.shift_id in cap_shift_ids
            )

            if has_night_permission and night_count > month_cap:
                errors.append(
                    {
                        "date": str(period.end_date),
                        "rule": "NIGHT_MONTH_MAX",
                        "employee_id": str(e.id),
                        "employee": e.name,
                        "count": night_count,
                        "limit": month_cap,
                        # 兼容既有界面渲染：显示为"（需 9 实际 12）"
                        "required": month_cap,
                        "actual": night_count,
                    }
                )

        # 【2026-09-24 新规则 1】节假日上 C 班者，前一天（若为工作日）必须上 O3
        if bool(rules.get("holiday_day_shift_needs_prev_o3")):
            for d in dates:
                hol = holidays.get(d)

                if not hol or hol.day_type != "HOLIDAY":
                    continue

                row = by_employee[e.id].get(d)

                if not (row and row.status == "WORK" and row.shift_id in shifts):
                    continue
                if shifts[row.shift_id].code != "C":
                    continue

                prev = d - timedelta(days=1)

                if prev < period.start_date:
                    continue

                prev_hol = holidays.get(prev)

                if prev_hol and prev_hol.day_type == "HOLIDAY":
                    continue
                if prev_hol is None and prev.weekday() >= 5:
                    continue

                prow = by_employee[e.id].get(prev)
                prev_is_o3 = bool(
                    prow and prow.status == "WORK" and prow.shift_id in shifts
                    and shifts[prow.shift_id].code == "O3"
                )

                if not prev_is_o3:
                    errors.append(
                        {
                            "date": str(d),
                            "rule": "HOLIDAY_C_NEEDS_PREV_O3",
                            "employee_id": str(e.id),
                            "employee": e.name,
                            "prev": str(prev),
                        }
                    )

        # 【2026-09-24 新规则 2】D 班不允许连续 2 天
        if bool(rules.get("d_shift_no_consecutive")):

            def _code_of(r):
                if r and r.status == "WORK" and r.shift_id in shifts:
                    return shifts[r.shift_id].code
                return None

            for _i in range(len(flags) - 1):
                if _code_of(flags[_i][2]) == "D" and _code_of(flags[_i + 1][2]) == "D":
                    errors.append(
                        {
                            "date": str(flags[_i + 1][0]),
                            "rule": "D_NO_CONSECUTIVE",
                            "employee_id": str(e.id),
                            "employee": e.name,
                            "from": str(flags[_i][0]),
                        }
                    )

            if flags and (prev_state.get("last_shift_code") or "") == "D" and _code_of(flags[0][2]) == "D":
                errors.append(
                    {
                        "date": str(flags[0][0]),
                        "rule": "D_NO_CONSECUTIVE",
                        "employee_id": str(e.id),
                        "employee": e.name,
                        "cross_month": True,
                    }
                )

    # 【2026-09-24】该周期所在年份尚未配置法定节假日 → 排班会把假日当普通工作日、统计不计加班
    holiday_years = {d.year for d in holidays}
    missing_years = sorted(
        {d.year for d in daterange(period.start_date, period.end_date)} - holiday_years
    )

    if missing_years:
        warnings.append(
            {
                "rule": "HOLIDAY_CALENDAR_MISSING",
                "years": missing_years,
                "message": (
                    "、".join(str(y) for y in missing_years)
                    + " 年尚未配置法定节假日：排班会把法定假日当普通工作日，"
                    "节假日加班统计也会为 0。请到「节假日管理」联网同步或手工录入。"
                ),
            }
        )

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
    }
