import os
import uuid
from datetime import date, timedelta, datetime

from collections import defaultdict

from ortools.sat.python import cp_model
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AuditLog,
    Employee,
    Shift,
    EmployeeShiftPermission,
    EmployeeLeave,
    Holiday,
    ScheduleAssignment,
    SchedulePeriod,
)
from .state import load_previous_boundary, generate_boundary_states
from ..rules import load_rules


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_leave(employee_id, d, leaves):
    for x in leaves.get(employee_id, []):
        if x.start_date <= d <= x.end_date and x.status == "APPROVED":
            return True
    return False


def _night_limit(employee) -> int:
    """【F3】员工级夜班连班上限。"""
    value = getattr(employee, "night_max_consecutive", None)
    return max(1, int(value or settings.night_default_max_consecutive))


def _night_rest(employee) -> int:
    """【F3】员工级"连班达标后强制休息天数"。"""
    value = getattr(employee, "night_rest_days", None)
    return max(0, int(value if value is not None else settings.night_default_rest_days))


def solve_period(db: Session, period_id, disabled=None):
    # 【F2 修复】消融开关改为显式参数，不再写 os.environ：
    # 进程级全局状态会让并发求解互相污染，且异常路径不恢复。
    _disabled = {str(x).strip() for x in (disabled or []) if str(x).strip()}

    def _dis(name):
        return name in _disabled

    # 【修复】Celery 任务传 str，db.get 对 UUID 主键要求 uuid.UUID
    if isinstance(period_id, str):
        period_id = uuid.UUID(period_id)

    period = db.get(SchedulePeriod, period_id)

    if not period:
        return {"status": "ERROR", "message": "period not found"}

    period.status = "SOLVING"
    db.commit()

    # 【2026-09-22】读取团队级可配置规则（Web 界面可改，无需改代码）
    rules = load_rules(db, period.team_id)

    # 规则同步到 settings：夜间策略/休息窗口/最低休息等下游逻辑统一从这里取值
    settings.night_default_max_consecutive = int(rules["night_default_max_consecutive"])
    settings.night_default_rest_days = int(rules["night_default_rest_days"])
    settings.night_rest_allows_holiday_night = bool(rules["night_rest_allows_holiday_night"])
    settings.night_rest_allows_day = bool(rules["night_rest_allows_day"])
    settings.max_consecutive_work_days = int(rules["max_consecutive_work_days"])
    settings.minimum_rest_hours = int(rules["minimum_rest_hours"])

    employees = db.scalars(
        select(Employee).where(Employee.active.is_(True))
    ).all()

    shifts = db.scalars(
        select(Shift).where(Shift.active.is_(True))
    ).all()

    shift_by_code = {x.code: x for x in shifts}

    # 【F3】班次分类必须在这里定义——rest_prefix / 最低休息等段落都在
    # 函数更早的位置引用它们（放在使用点之后会 UnboundLocalError）：
    #   night_shifts   全部夜班（N+Y，21:00–09:00）——最低休息计算
    #   streak_nights  计入"连班"的夜班（只有 N）——连班上限/块结构/强制休息
    #   holiday_nights 假日夜班（Y，不计入连班）——休息窗口内允许出现
    night_shifts = [s for s in shifts if s.is_night]
    streak_nights = [s for s in shifts if s.counts_as_night_streak]
    holiday_nights = [s for s in shifts if s.is_night and not s.counts_as_night_streak]
    day_shifts = [s for s in shifts if not s.is_night]

    dates = list(daterange(period.start_date, period.end_date))

    # 【2026-09-22】工作日填充目标（按团队规模自动缩放：22 人→20、17 人→15）
    WORKDAY_FILL_TARGET = max(1, len(employees) - int(rules["workday_headcount_slack"]))

    permissions = defaultdict(set)

    for p in db.scalars(
        select(EmployeeShiftPermission).where(
            EmployeeShiftPermission.allowed.is_(True)
        )
    ):
        permissions[p.employee_id].add(p.shift_id)

    leaves = defaultdict(list)

    for leave in db.scalars(
        select(EmployeeLeave).where(
            EmployeeLeave.status == "APPROVED"
        )
    ):
        leaves[leave.employee_id].append(leave)

    holidays = {
        x.holiday_date: x
        for x in db.scalars(
            select(Holiday).where(
                Holiday.team_id == period.team_id
            )
        )
    }

    # 固定排班（文档 §54 + 补全模式）：
    # - locked assignment 永远不能被修改；
    # - HISTORY / MANUAL 来源的已有班次视为用户认可的事实，同样固定；
    # - 只有 AUTO 且未锁定的行才会被重排覆盖。
    # 已有班次的日期不再强加 coverage（历史事实优先），空白日期按标准规则补全。
    fixed_map = {}
    for r in db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == period.id
        )
    ).all():
        if r.locked or r.source != "AUTO":
            fixed_map[(r.employee_id, r.work_date)] = r.shift_id

    fixed_dates = {d for (_e, d) in fixed_map}

    model = cp_model.CpModel()

    # x[e,d,s] = employee e on shift s at date d
    x = {}

    # off[e,d]
    off = {}

    employee_index = {
        e.id: i
        for i, e in enumerate(employees)
    }

    date_index = {
        d: i
        for i, d in enumerate(dates)
    }

    for e in employees:
        for d in dates:
            off[e.id, d] = model.NewBoolVar(
                f"off_{employee_index[e.id]}_{d}"
            )

            for s in shifts:
                x[e.id, d, s.id] = model.NewBoolVar(
                    f"x_{employee_index[e.id]}_{d}_{s.code}"
                )

    # 每个人每天只能一个状态
    for e in employees:
        for d in dates:
            vars_day = [
                x[e.id, d, s.id]
                for s in shifts
            ]

            model.Add(
                sum(vars_day) + off[e.id, d] == 1
            )

            # Leave = 强制休息
            # 【修复】文档原文写反成 off == 0，与"每天恰好一个状态"矛盾，
            # 只要存在请假记录整个模型必然 INFEASIBLE。
            if is_leave(e.id, d, leaves):
                model.Add(off[e.id, d] == 1)
                for s in shifts:
                    model.Add(x[e.id, d, s.id] == 0)

    # 权限
    for e in employees:
        allowed = permissions[e.id]

        for d in dates:
            for s in shifts:
                if s.id not in allowed:
                    model.Add(x[e.id, d, s.id] == 0)

    # Boundary State
    rest_prefix_by_emp = {}

    for e in employees:
        state = load_previous_boundary(db, period, e)

        work_prefix = state["consecutive_work_days"]
        night_prefix = state["consecutive_night_shifts"]
        rest_prefix = state["night_rest_remaining"]
        rest_prefix_by_emp[e.id] = rest_prefix

        if rest_prefix > 0:
            for d in dates[:rest_prefix]:
                # 补全模式：该日已有固定班次时跳过（历史事实优先，
                # 跨期休息以历史实际排班为准）
                if (e.id, d) in fixed_map:
                    continue

                if settings.night_rest_allows_holiday_night and holiday_nights:
                    # 【F3】休息窗口内允许假日夜班 Y（Y 不计入连班）
                    model.Add(
                        off[e.id, d]
                        + sum(x[e.id, d, s.id] for s in holiday_nights)
                        >= 1
                    )
                else:
                    model.Add(off[e.id, d] == 1)

        # 如果上周期已经连续夜班，则当前周期继续计算
        # 这里只做 prefix 约束，完整序列由下面窗口约束负责。

        # 【跨月修复】连续上班天数必须跨月连续：上月已连上 work_prefix 天，
        # 本月前 (limit-work_prefix+1) 天里至少要休 1 天。
        # 原实现只在 work_prefix >= 7 时强制首日休 —— work_prefix=5 时
        # 会出现"跨月连上 8 天"（实测某员工）。
        work_limit = int(rules["max_consecutive_work_days"])

        if work_prefix > 0:
            allow_more = max(0, work_limit - work_prefix)
            span = dates[:allow_more + 1]

            if span and not all((e.id, d) in fixed_map for d in span):
                model.Add(sum(off[e.id, d] for d in span) >= 1)

        # 【跨月修复】上月最后一天是 O2 / D：本周期首日不得再排同一班次
        # （周期内的 O2→O2 / D→D 硬禁止原本不跨月）
        last_code_prev = state.get("last_shift_code")

        if last_code_prev in ("O2", "D"):
            prev_shift = shift_by_code.get(last_code_prev)

            if prev_shift and (e.id, dates[0]) not in fixed_map:
                model.Add(x[e.id, dates[0], prev_shift.id] == 0)

    # 【2026-09-22 新增】工作日最低在岗硬约束：消除"月初人手缺口"
    #（实测 2026-12 的 11/26 只有 10~11 人在岗，而 12/07 之后是 15 人）。
    # 下限按**当天真实可用人数**收敛：已被跨月强制休息/请假占掉的人先扣除，
    # 因此不会因为"当天本来就没人"而 INFEASIBLE。
    for d in dates:
        hol_floor = holidays.get(d)

        if hol_floor is not None and hol_floor.day_type == "HOLIDAY":
            continue

        if d.weekday() >= 5:
            continue

        forced_off = sum(1 for e in employees if is_leave(e.id, d, leaves))
        offset_days = (d - period.start_date).days

        for e in employees:
            if offset_days < (rest_prefix_by_emp.get(e.id, 0) or 0):
                forced_off += 1

        available = len(employees) - forced_off
        floor_d = min(
            WORKDAY_FILL_TARGET - 1,
            max(0, available - int(rules["workday_min_headcount_slack"])),
        )

        if floor_d > 0:
            model.Add(
                sum(1 - off[e.id, d] for e in employees) >= floor_d
            )

    # 固定约束：locked / HISTORY / MANUAL 班次强制进解
    for (e_id, d), sid in fixed_map.items():
        if e_id not in employee_index or d not in date_index:
            continue

        if sid is None:
            model.Add(off[e_id, d] == 1)
        else:
            for s in shifts:
                model.Add(x[e_id, d, s.id] == (1 if s.id == sid else 0))

    # 【2026-09-24 新规则 1】节假日上白班 C 的人，紧邻前一天若是工作日，当天必须上 O3
    if bool(rules.get("holiday_day_shift_needs_prev_o3")):
        c_shift = shift_by_code.get("C")
        o3_shift = shift_by_code.get("O3")

        if c_shift and o3_shift:
            for h in dates:
                hol = holidays.get(h)

                if not hol or hol.day_type != "HOLIDAY":
                    continue

                prev = h - timedelta(days=1)

                if prev not in date_index:
                    continue        # 前一天落在周期外（跨月），无法约束

                prev_hol = holidays.get(prev)

                if prev_hol and prev_hol.day_type == "HOLIDAY":
                    continue        # 前一天也是假日 → 不是"工作日"

                if prev_hol is None and prev.weekday() >= 5:
                    continue        # 前一天是周末且无调休上班 → 不是"工作日"

                for e in employees:
                    if c_shift.id not in permissions[e.id]:
                        continue    # 没 C 权限，天然不违反
                    if o3_shift.id not in permissions[e.id]:
                        model.Add(x[e.id, h, c_shift.id] == 0)
                        continue
                    model.Add(x[e.id, h, c_shift.id] <= x[e.id, prev, o3_shift.id])

    # 【2026-09-24 新规则 2】D 班不允许连续 2 天（跨月同样生效）
    if bool(rules.get("d_shift_no_consecutive")):
        d_shift = shift_by_code.get("D")

        if d_shift:
            for e in employees:
                if d_shift.id not in permissions[e.id]:
                    continue

                for i in range(len(dates) - 1):
                    d1, d2 = dates[i], dates[i + 1]

                    if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                        continue

                    model.Add(x[e.id, d1, d_shift.id] + x[e.id, d2, d_shift.id] <= 1)

                prev_state = load_previous_boundary(db, period, e)

                if (prev_state.get("last_shift_code") or "") == "D" and dates:
                    if (e.id, dates[0]) not in fixed_map:
                        model.Add(x[e.id, dates[0], d_shift.id] == 0)

    # 工作日/周末/节假日 Coverage
    # 补全模式：所有日期都施加 coverage；固定班次（x 被强制为 1）
    # 自动计入 sum，即历史已贡献的部分天然满足，缺口由自由员工补足。
    for d in dates:
        holiday = holidays.get(d)

        if holiday and holiday.day_type == "HOLIDAY":
            # 节假日精确覆盖（用户确认规则）：只排 3 个 C + 2 个 Y，
            # 其余员工一律休息（禁止 O1/O2/O3/D/N）
            for code, count in rules["coverage_holiday"].items():
                shift = shift_by_code.get(code)

                if not shift:
                    model.Add(0 >= count)
                    continue

                model.Add(
                    sum(
                        x[e.id, d, shift.id]
                        for e in employees
                    ) == count
                )

            holiday_codes = set(rules["coverage_holiday"])

            for s in shifts:
                if s.code in holiday_codes:
                    continue

                model.Add(
                    sum(
                        x[e.id, d, s.id]
                        for e in employees
                    ) == 0
                )

            continue

        if holiday and holiday.day_type == "WORKDAY_TRANSFER":
            # 覆盖需求来自规则（界面可改）；O1 是弹性补充班次，不设硬性人数
            required = dict(rules["coverage_workday"])
        elif d.weekday() >= 5:
            # 周末精确覆盖（用户确认规则）：只排 2 个 D + 2 个 N，
            # 其余员工一律休息（禁止 O1/O2/O3/C/Y）
            for code, count in rules["coverage_weekend"].items():
                shift = shift_by_code.get(code)

                if not shift:
                    model.Add(0 >= count)
                    continue

                model.Add(
                    sum(
                        x[e.id, d, shift.id]
                        for e in employees
                    ) == count
                )

            weekend_codes = set(rules["coverage_weekend"])

            for s in shifts:
                if s.code in weekend_codes:
                    continue

                model.Add(
                    sum(
                        x[e.id, d, s.id]
                        for e in employees
                    ) == 0
                )

            continue
        else:
            # 覆盖需求来自规则（界面可改）；O1 是弹性补充班次，不设硬性人数
            required = dict(rules["coverage_workday"])

        for code, count in required.items():
            shift = shift_by_code.get(code)

            if not shift:
                model.Add(0 >= count)
                continue

            model.Add(
                sum(
                    x[e.id, d, shift.id]
                    for e in employees
                ) >= count
            )

    # 连续工作 <= 7
    # 补全模式：窗口内全部为固定班次时跳过（历史事实优先，无法追溯修改）
    for e in employees:
        for i in range(len(dates) - 7):
            window = dates[i:i+8]

            if all((e.id, wd) in fixed_map for wd in window):
                continue

            model.Add(
                sum(
                    1 - off[e.id, d]
                    for d in window
                ) <= 7
            )

    # 班次分类已上移到本函数开头（见 shift_by_code 之后）。

    # 真实最低休息时间（文档 §50/§51）：夜班 21:00→次日09:00，
    # 次日任何白班（O1/O2/O3/D/C）休息都 < 11 小时，必须禁止。
    # 两端都是固定班次的历史组合跳过（历史事实优先）。
    for e in employees:
        for i in range(len(dates) - 1):
            d1, d2 = dates[i], dates[i + 1]

            if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                continue

            for n in night_shifts:
                for ds in day_shifts:
                    model.Add(
                        x[e.id, d1, n.id] + x[e.id, d2, ds.id] <= 1
                    )

    # 【F3】连班上限按人配置，且只统计 N（Y 不计入）
    for e in employees:
        limit_e = _night_limit(e)

        for i in range(len(dates) - limit_e):
            window = dates[i:i + limit_e + 1]

            if all((e.id, wd) in fixed_map for wd in window):
                continue

            model.Add(
                sum(
                    x[e.id, d, s.id]
                    for d in window
                    for s in streak_nights
                ) <= limit_e
            )

        # 【F3 跨月】上月已连续 cn 个 N：本周期前 (limit-cn+1) 天不能再全排 N，
        # 否则跨月会凑出 limit+1 连（周期内窗口看不到上月，必须补这一段）
        prev_state = load_previous_boundary(db, period, e)
        # 上限取 limit_e：cn=limit 时需要 k=limit 那一条（span=1, bound=0），
        # 即"首日不得再上 N"，否则跨月会凑出 limit+1 连。
        cn_prev_win = min(
            prev_state.get("consecutive_night_shifts", 0) or 0, limit_e
        )

        for k in range(1, max(0, cn_prev_win) + 1):
            span = limit_e - k + 1
            if span <= 0 or span > len(dates):
                continue
            if all((e.id, wd) in fixed_map for wd in dates[:span]):
                continue
            model.Add(
                sum(
                    x[e.id, d, s.id]
                    for d in dates[:span]
                    for s in streak_nights
                ) <= limit_e - k
            )

    # 【2026-09-24 新规则】单月夜班次数上限（硬约束）
    #   统计口径：默认只算夜班 N（streak_nights）；开启 night_month_count_holiday_night 后连 Y 一起算。
    #   只约束"有夜班权限"的员工；上限 0 = 不限制。
    month_cap = int(rules.get("night_month_max_shifts") or 0)

    if month_cap > 0:
        cap_shifts = (
            list(night_shifts)
            if bool(rules.get("night_month_count_holiday_night"))
            else list(streak_nights)
        )

        for e in employees:
            # 按该员工实际有权限的班次求和（无权限的 x 变量不存在）
            cap_allowed = [s for s in cap_shifts if s.id in permissions[e.id]]

            if not cap_allowed:
                continue

            model.Add(
                sum(
                    x[e.id, d, s.id]
                    for d in dates
                    for s in cap_allowed
                ) <= month_cap
            )

    # 【修复 2026-09-21】夜班块（>=3 连）结束后必须连休 rest 天。
    # 原实现以"连续 N 达到个人上限"为触发条件：上限为 4 的人做完 3 连块后
    # 完全不触发休息 —— 实测 18 个夜班块只休 1 天就上白班（用户反馈
    # "夜班后没有连休"）。现按"块结束后"触发：
    #   对每个 3 连窗口 i..i+2，其后第 k 天（k=3..3+rest-1）必须满足
    #   「是休/Y」或「块仍在延续（i+3..k 全是 N）」，
    #   后者允许上限 4 的人接着上第 4 个夜班，休息从块真正结束处算起。
    for e in employees:
        rest_e = _night_rest(e)

        if rest_e <= 0 or not streak_nights:
            continue

        ei = employee_index[e.id]

        def _nsum(day):
            return sum(x[e.id, day, s.id] for s in streak_nights)

        def _allowed_rest(day):
            expr = off[e.id, day]

            if settings.night_rest_allows_holiday_night and holiday_nights:
                expr = expr + sum(x[e.id, day, s.id] for s in holiday_nights)

            if settings.night_rest_allows_day and day_shifts:
                expr = expr + sum(x[e.id, day, s.id] for s in day_shifts)

            return expr

        for i in range(len(dates) - 2):
            if all((e.id, wd) in fixed_map for wd in dates[i:i + 3]):
                continue

            three = model.NewBoolVar(f"three_{ei}_{i}")
            head_sum = _nsum(dates[i]) + _nsum(dates[i + 1]) + _nsum(dates[i + 2])

            for j in range(3):
                model.Add(three <= _nsum(dates[i + j]))
            model.Add(three >= head_sum - 2)

            for k in range(3, 3 + rest_e):
                idx = i + k

                if idx >= len(dates):
                    break

                day = dates[idx]

                if (e.id, day) in fixed_map:
                    continue

                run = list(range(i + 3, idx + 1))

                if len(run) == 1:
                    cont = _nsum(dates[run[0]])
                else:
                    cont = model.NewBoolVar(f"cont_{ei}_{i}_{k}")
                    for j in run:
                        model.Add(cont <= _nsum(dates[j]))
                    model.Add(cont >= sum(_nsum(dates[j]) for j in run) - (len(run) - 1))

                model.Add(three <= cont + _allowed_rest(day))

    # 【新增】夜班块结构（用户规则）：夜班不允许散乱，尽量 3+ 连块。
    # 【软惩罚设计】硬规则（禁孤立/N_N 缺口/2 连断头三条线性约束）与
    # 工时对齐组合在"块+休3"结构下过紧导致 INFEASIBLE（实测），
    # 故回到三类软惩罚（-300/个，与均等同级主导块结构）：
    #   pen1: 块起点但次日断（孤立夜班）
    #   pen2: 块起点但第 3 天断（2 连块）
    #   pen3: N_N 缺口（前后夜班中间空）
    # 【位置教训】此段必须在 solver.Solve() 之前——曾有一版被插到 Solve 之后，
    # 约束根本没进模型，导致块约束"静默失效"。
    # 【2026-09-21 架构重构】软惩罚在工时对齐等新约束挤压下失灵（碎片依旧），
    # 改为硬规则组合（块起点延伸 + 禁 N_N 缺口）+
    # 首日虚拟前夜（head_prev = 上月连续夜班数）实现真正的跨月块衔接。
    # 【二次修正】禁孤立（a）与延续/覆盖组合 INFEASIBLE（实测），a 软化为惩罚。
    night_pen = []

    if night_shifts:
        for e in employees:
            ei = employee_index[e.id]

            limit_n = _night_limit(e)

            nunion = {}
            for d in dates:
                v = model.NewIntVar(0, 1, f"nuni_{ei}_{d}")
                # 【F3】块结构与连班计数只基于 N；假日夜班 Y 不计入
                model.Add(v == sum(x[e.id, d, s.id] for s in streak_nights))
                nunion[d] = v

            # 【跨月衔接】读取上周期期末状态：
            #   cn_prev = 期末连续夜班数（1/2 = 未满 3 连的块，3 = 完整块）
            #   last_code = 期末最后班次
            state = load_previous_boundary(db, period, e)
            cn_prev = min(
                state.get("consecutive_night_shifts", 0) or 0, limit_n
            )
            last_code = state.get("last_shift_code")

            # 【跨月规则一】块延续（2026-09-21 改为**硬约束**）：期末夜班块未满
            # 3 连（1/2 个）→ 本周期首日继续夜班补满 3 连。
            # 原为软奖励（-400/天），但压不过"出勤天数均等(-800/天)"，
            # 于是月末常留下 1~2 连残块（实测：某夜班员工 11/25 孤立夜班 → 次日休、
            # 12 月也未延续）。现改为硬约束；遇法定假日（禁止排 N）自动放弃。
            if cn_prev in (1, 2) and not _dis("carry"):
                need = 3 - cn_prev
                print(f"[CARRYOVER] {e.name} cn={cn_prev} 延续 {need} 天夜班（硬约束）")
                for j in range(min(need, len(dates))):
                    d = dates[j]

                    if (e.id, d) in fixed_map:
                        continue

                    hol = holidays.get(d)

                    if hol is not None and hol.day_type == "HOLIDAY":
                        break

                    model.Add(nunion[d] == 1)
                # 延续块满 3 连后次日禁白（只能夜班或休 = 三连夜休 3 的开始；
                # 连夜≤3 的硬约束会自然使其成为休班日）。
                j2 = need
                if j2 < len(dates) and (e.id, dates[j2]) not in fixed_map:
                    model.Add(nunion[dates[j2]] + off[e.id, dates[j2]] == 1)

            # 【跨月修复 2026-09-21 通用化】上月已连 p 个 N（p<=limit）：
            # 本周期若以 t 个 N 续下去、使合计 p+t >= 3（构成完整夜班块）后结束，
            # 则随后 rest 天必须"休 或 Y"。
            # 原实现只处理"续到个人上限(limit)"一种情况，于是：
            #   ① 上限为 4 的人做 3 连块结束后无人强制休息；
            #   ② 上月已连 1 个、本月再续 2 个（合计 3 连）后结束也不休息
            #      （实测：某夜班员工 10/25 N + 10/26,10/27 N → 10/29 就上了 O2）。
            rest_n = _night_rest(e)
            limit_e = _night_limit(e)
            p_prev = min(cn_prev, limit_e)

            if rest_n > 0 and streak_nights:

                def _nsum_x(day):
                    return sum(x[e.id, day, s.id] for s in streak_nights)

                def _allowed_x(day):
                    expr = off[e.id, day]

                    if settings.night_rest_allows_holiday_night and holiday_nights:
                        expr = expr + sum(x[e.id, day, s.id] for s in holiday_nights)

                    if settings.night_rest_allows_day and day_shifts:
                        expr = expr + sum(x[e.id, day, s.id] for s in day_shifts)

                    return expr

                for t in range(max(0, 3 - p_prev), (limit_e - p_prev) + 1):
                    if t >= len(dates):
                        break

                    if t == 0:
                        all_n = None
                    elif t == 1:
                        all_n = _nsum_x(dates[0])
                    else:
                        all_n = model.NewBoolVar(f"alln_{ei}_{t}")
                        for j in range(t):
                            model.Add(all_n <= _nsum_x(dates[j]))
                        model.Add(
                            all_n
                            >= sum(_nsum_x(dates[j]) for j in range(t)) - (t - 1)
                        )

                    end_at = model.NewBoolVar(f"endat_{ei}_{t}")

                    if all_n is not None:
                        model.Add(end_at <= all_n)
                        model.Add(end_at >= all_n - _nsum_x(dates[t]))
                    else:
                        model.Add(end_at >= 1 - _nsum_x(dates[t]))

                    model.Add(end_at <= 1 - _nsum_x(dates[t]))

                    for r in dates[t:t + rest_n]:
                        if (e.id, r) in fixed_map:
                            continue

                        model.Add(end_at <= _allowed_x(r))

            # 【跨月规则二】夜白禁令：期末末班为夜班 → 首日只能夜班或休
            # （夜班次日白班休息 <11h，跨月同样适用）。
            if last_code in ("N", "Y") and not _dis("nightrest"):
                # 【F3】首日只能"夜班（N 或 Y）或休"，不能排白班（真实休息 < 11h）。
                # 原实现用 nunion（现在只含 N），会把合法的 Y 也排除掉。
                all_night_first = sum(
                    x[e.id, dates[0], s.id] for s in night_shifts
                )
                print(f"[NIGHT-REST] {e.name} 上月末班={last_code} 本周期首日禁白班")
                if (e.id, dates[0]) not in fixed_map:
                    model.Add(all_night_first + off[e.id, dates[0]] == 1)

            # 【块起点】首日虚拟前夜 = 上周期期末连续夜班数（head_prev）：
            #   cn=1/2 → 首日是上月块的延续（start=0，不再当新块起点延伸——
            #   否则与延续叠加成 4 连夜，违反连夜≤3，实测 INFEASIBLE）；
            #   cn=3 → 首日必然休（rest_remaining 已强制）；cn=0 → 可为新起点。
            # 【3 连保证】块起点 start → 次日、第 3 日强制夜班。
            #
            # 【关键修复】head_prev 的语义是"前一天是不是夜班"（0/1），
            # 不能直接赋成"上月末连续夜班数"：start 是 BoolVar，
            # head_prev=2 时 `start <= 1 - prev` 变成 `start <= -1`，
            # CP-SAT 中 0/1 都不满足 ⇒ 整个模型立刻 INFEASIBLE。
            # 实测：cn_prev=1 可行，cn_prev=2/3 必无解（与 rest_remaining 无关）。
            # 这正是"上一周期以 ≥2 连夜班收尾 → 下一周期排不出来"的根因。
            head_prev = 1 if cn_prev > 0 else 0

            for i, d in enumerate(dates):
                prev = nunion[dates[i - 1]] if i > 0 else head_prev

                # 【2026-09-21 新增】周期末尾 2 天不允许"开新夜班块"：
                # 最后 2 天出现 N 必须是前一日的延续，否则会留下 1~2 连残块
                # （实测：某夜班员工 11/25 孤立 N（前一日 O3、次日休）、另一员工同类）。
                if i >= len(dates) - 2 and i > 0:
                    model.Add(nunion[d] <= nunion[dates[i - 1]])

                start = model.NewBoolVar(f"nstart_{ei}_{d}")
                model.Add(start <= nunion[d])
                model.Add(start <= 1 - prev)
                model.Add(start >= nunion[d] - prev)

                if i + 1 < len(dates):
                    model.Add(nunion[dates[i + 1]] >= start)
                if i + 2 < len(dates):
                    model.Add(nunion[dates[i + 2]] >= start)

                # 【禁孤立夜班（软）】内部日的夜班至少一侧相邻也是夜班
                # （起点延伸规则已保证新块 ≥3 连，此项兜底延续边界的碎片）
                if 0 < i < len(dates) - 1 and not _dis("niso"):
                    iso = model.NewIntVar(0, 1, f"niso_{ei}_{d}")
                    model.Add(
                        iso >= nunion[d] - nunion[dates[i - 1]] - nunion[dates[i + 1]]
                    )
                    night_pen.append(iso)

    # 【新增】权限硬约束（文档 §21）：无权限员工整周期禁止排对应班次。
    # 之前 solver 不查权限，全靠 validator 事后拦截——必须前置。
    perm_rows = db.scalars(
        select(EmployeeShiftPermission).where(
            EmployeeShiftPermission.allowed.is_(True)
        )
    ).all()

    allowed_pairs = {(p.employee_id, p.shift_id) for p in perm_rows}

    for e in employees:
        for s in shifts:
            if (e.id, s.id) not in allowed_pairs:
                model.Add(
                    sum(x[e.id, d, s.id] for d in dates) == 0
                )

    # 【业务规则】班次与日类型匹配：
    #   Y/C 仅法定假日（3 倍日）；D 仅周末；N/O1/O2/O3 仅非假日。
    # （曾出现非假日被排 Y 的错误——3 倍工资口径随之错乱）
    for e in employees:
        for d in dates:
            hol = holidays.get(d)
            is_holiday = bool(hol and hol.day_type == "HOLIDAY")
            is_weekend = d.weekday() >= 5

            if not is_holiday:
                for code in ("Y", "C"):
                    s = shift_by_code.get(code)
                    if s:
                        model.Add(x[e.id, d, s.id] == 0)

            if is_holiday:
                for code in ("N", "O1", "O2", "O3", "D"):
                    s = shift_by_code.get(code)
                    if s:
                        model.Add(x[e.id, d, s.id] == 0)

            if not is_weekend and not is_holiday:
                s = shift_by_code.get("D")
                if s:
                    model.Add(x[e.id, d, s.id] == 0)

    # 公平性：工作小时（低权重；班次均等已隐含大部分公平）
    hours = {}

    for e in employees:
        # 【修复】文档上限 10000（=100 小时）过小，31 天周期极端分布
        # 可超 200 小时，会被域上限误判 INFEASIBLE；放大到 1e6。
        hours[e.id] = model.NewIntVar(
            0,
            1000000,
            f"hours_{employee_index[e.id]}"
        )

        terms = []

        for d in dates:
            for s in shifts:
                # 用 100 倍整数表示小时，避免浮点
                paid = int(float(s.paid_hours) * 100)

                terms.append(
                    paid * x[e.id, d, s.id]
                )

        model.Add(
            hours[e.id] == sum(terms)
        )

    # 总工作量范围
    max_hours = model.NewIntVar(0, 10000000, "max_hours")
    min_hours = model.NewIntVar(0, 10000000, "min_hours")

    for e in employees:
        model.Add(hours[e.id] <= max_hours)
        model.Add(hours[e.id] >= min_hours)

    # 【业务规则 2026-09-21】工时口径：
    #   普通工时 = 排除法定假日班次（C/Y）后的工时；节假日加班单独统计、
    #   不计入普通工时；人均普通工时以"全 O1 班次人员"为基准。
    #   原实现是"夜班人员向无夜班权限者的最高总工时看齐"，未排除节假日、
    #   且以最大值为基准，会把整体越推越高（实测夜班人员 192~244h vs 全 O1 176h）。
    holiday_set = {
        d
        for d in dates
        if holidays.get(d) is not None and holidays[d].day_type == "HOLIDAY"
    }

    normal_hours = {}
    holiday_hours = {}

    for e in employees:
        n_terms, h_terms = [], []

        for d in dates:
            for s in shifts:
                paid = int(float(s.paid_hours) * 100)
                if d in holiday_set:
                    h_terms.append(paid * x[e.id, d, s.id])
                else:
                    n_terms.append(paid * x[e.id, d, s.id])

        nv = model.NewIntVar(0, 1000000, f"nhours_{employee_index[e.id]}")
        model.Add(nv == (sum(n_terms) if n_terms else 0))
        normal_hours[e.id] = nv

        if h_terms:
            hv = model.NewIntVar(0, 1000000, f"hhours_{employee_index[e.id]}")
            model.Add(hv == sum(h_terms))
            holiday_hours[e.id] = hv

    o1_row = shift_by_code.get("O1")
    allowed_codes = {
        e.id: {s.code for s in shifts if (e.id, s.id) in allowed_pairs}
        for e in employees
    }

    # 基准组：普通班次只有 O1（可能另有 C/Y 的节假日加班权限）
    benchmark = [
        e
        for e in employees
        if o1_row is not None
        and (e.id, o1_row.id) in allowed_pairs
        and not (allowed_codes[e.id] & {"O2", "O3", "D", "N"})
    ]

    # 【为什么是软的】硬对齐与夜班块结构/跨月衔接组合会 INFEASIBLE（历史实测），
    # 因此仍用 -30/小时的软偏差（单位 0.01 小时）。
    night_capable = {
        e.id
        for e in employees
        if any((e.id, s.id) in allowed_pairs for s in night_shifts)
    }

    align_gaps = []

    if len(benchmark) >= 2:
        n_bench = len(benchmark)

        bench_total = model.NewIntVar(0, 10000000, "bench_total")
        model.Add(bench_total == sum(normal_hours[e.id] for e in benchmark))

        bench_avg = model.NewIntVar(0, 1000000, "bench_avg")
        model.Add(bench_total >= bench_avg * n_bench)
        model.Add(bench_total <= bench_avg * n_bench + (n_bench - 1))

        for e in employees:
            if e in benchmark:
                continue

            # 【只惩罚"超过基准"的一侧】以全 O1 人员的普通工时作参考，
            # 避免把夜班/多班次人员排得比基准更重。
            # 不惩罚低于基准：双向对齐会把所有人拉到完全相同工时，
            # 反而压垮夜班 3-4 连块与 O3 长连块（实测：O3 块退化成一堆 1~2 连）。
            gap_up = model.NewIntVar(0, 1000000, f"hgap_up_{employee_index[e.id]}")
            model.Add(gap_up >= normal_hours[e.id] - bench_avg)
            align_gaps.append(gap_up)

    # 【新增 2026-09-21】出勤天数均等：业务反馈"有人班次不足"
    #（实测班次极差 5：夜班人员 17~18 班 vs 全 O1 人员 22~23 班）。
    # 真实排班的做法是"班次数相当、夜班人员工时略高"
    #（真实 2026-10：员工甲 16 班 128h、员工乙 16 班 140h），
    # 所以这里把"出勤天数 max-min"作为主公平目标（与班次均等 -300/班同级）。
    day_counts = {}

    for e in employees:
        dv = model.NewIntVar(0, len(dates), f"days_{employee_index[e.id]}")
        model.Add(dv == sum(1 - off[e.id, d] for d in dates))
        day_counts[e.id] = dv

    max_days = model.NewIntVar(0, len(dates), "max_days")
    min_days = model.NewIntVar(0, len(dates), "min_days")

    for e in employees:
        model.Add(max_days >= day_counts[e.id])
        model.Add(min_days <= day_counts[e.id])

    day_spread = model.NewIntVar(0, len(dates), "day_spread")
    model.Add(day_spread == max_days - min_days)

    # 【新增】班次均等（O1 除外，用户确认规则）：
    # 每个班次在员工间的 max(count)-min(count) 最小化。
    # 【用户规则 2026-09-20】均衡的两个关键修正：
    # 1. **基于班次权限**：只有拥有该班次权限的人才参与该班次的均等
    #    （无权限者恒为 0，会把 min 拉到 0 使均衡失真）；
    # 2. **全年累计基线**：计数 = 本年度已排累计 + 本周期新增——
    #    上月排得多的人本月自动少排，跨月/全年自动轮换趋平
    #    （特别是节假日 C/Y 的全年均衡，用户明确要求）。
    # N/Y 重新纳入均等：全年累计视角下，单月 N 呈 3 或 6（块结构）
    # 而跨月累计趋平——块结构与均等不再冲突。
    prev_counts = {}

    prev_rows = db.execute(
        select(
            ScheduleAssignment.employee_id,
            Shift.code,
            func.count(),
        )
        .join(Shift, Shift.id == ScheduleAssignment.shift_id)
        .join(SchedulePeriod, SchedulePeriod.id == ScheduleAssignment.period_id)
        .where(
            SchedulePeriod.team_id == period.team_id,
            SchedulePeriod.id != period.id,
            SchedulePeriod.start_date >= date(period.start_date.year, 1, 1),
            SchedulePeriod.start_date < period.start_date,
            ScheduleAssignment.status == "WORK",
        )
        .group_by(ScheduleAssignment.employee_id, Shift.code)
    ).all()

    for emp_id, code, n in prev_rows:
        prev_counts[(emp_id, code)] = int(n)

    fair_diffs = []

    for code in ("O2", "O3", "D", "N", "C", "Y"):
        s = shift_by_code.get(code)

        if not s:
            continue

        # 只有有权限的人才参与该班次的均衡
        eligible = [
            e for e in employees
            if (e.id, s.id) in allowed_pairs
        ]

        if len(eligible) < 2:
            continue

        counts = []

        for e in eligible:
            # 【用户澄清】全年累计基线只用于节假日班次（C/Y）——
            # 跨月轮换节假日值班；O2/O3/D/N 月度内均衡即可（base=0）。
            base = prev_counts.get((e.id, code), 0) if code in ("C", "Y") else 0
            cv = model.NewIntVar(
                base, base + len(dates), f"cnt_{employee_index[e.id]}_{code}"
            )
            model.Add(
                cv == base + sum(x[e.id, d, s.id] for d in dates)
            )
            counts.append(cv)

        mx = model.NewIntVar(0, 500, f"cntmax_{code}")
        mn = model.NewIntVar(0, 500, f"cntmin_{code}")
        model.AddMaxEquality(mx, counts)
        model.AddMinEquality(mn, counts)

        # 【N 特殊处理】夜班以 3 连块为单位（每人月度 N 呈 3/6/9…），
        # 与连续最小化数学冲突（实测块结构退化）——改为**月度硬上限**：
        # 有权限人群内月度差距 ≤ 3，与块结构天然兼容。
        # C/Y/O2/O3/D 保持连续最小化（C/Y 基于全年累计）。
        if code == "N":
            model.Add(mx - mn <= 3)
        else:
            fair_diffs.append(mx - mn)

    # 【新增】班次连续块（人性化规律）：
    # 最大化「相邻两天同一员工同一班次」的配对数，
    # 促使夜班成 2-3 连块、白班成连排，避免今天 O1 明天 N 的碎片化。
    # 【用户规则】O2/D 禁止连续（硬约束见下）；O3 有专属高权重连块（连续性优先）。
    same_pairs = []

    for e in employees:
        ei = employee_index[e.id]

        for i in range(len(dates) - 1):
            d1, d2 = dates[i], dates[i + 1]

            if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                continue

            for s in shifts:
                if s.code in ("O2", "D", "O3"):
                    continue

                b = model.NewBoolVar(f"same_{ei}_{i}_{s.code}")
                # b = x[d1] AND x[d2]
                # 【修复】布尔否定必须用 ~（- 会生成算术负 IntAffine，
                # 传进 AddBoolOr 直接 TypeError）
                model.AddBoolOr([~b, x[e.id, d1, s.id]])
                model.AddBoolOr([~b, x[e.id, d2, s.id]])
                model.AddBoolOr([b, ~x[e.id, d1, s.id], ~x[e.id, d2, s.id]])
                same_pairs.append(b)

    # 【用户规则】O3 连续性优先：O3→O3 配对给高权重（与夜班块同级），
    # 促使 O3 成 3-5 天连块，不被 O2 打散。
    o3s = shift_by_code.get("O3")
    o3_pairs = []

    if o3s:
        for e in employees:
            ei = employee_index[e.id]

            for i in range(len(dates) - 1):
                d1, d2 = dates[i], dates[i + 1]

                if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                    continue

                b = model.NewBoolVar(f"o3pair_{ei}_{i}")
                model.AddBoolOr([~b, x[e.id, d1, o3s.id]])
                model.AddBoolOr([~b, x[e.id, d2, o3s.id]])
                model.AddBoolOr([b, ~x[e.id, d1, o3s.id], ~x[e.id, d2, o3s.id]])
                o3_pairs.append(b)

    # 【新增】白班转移规则（用户确认）：
    #   O2→O2、D→D 硬禁止（O2 穿插在 O1 中；周末两天不让同一人连上 12h）
    #   O3→O2 惩罚（晚班次日早班体验差，尽量减少；也避免 O2 贴上 O3 块尾）
    #   【修正】O2→O3 只是"允许"，不再奖励——曾给 +10 奖励导致 solver
    #   为了拿分把 O2 全贴着 O3 排，反而打散了 O3 连续块（用户截图反馈）。
    # 相邻两天均为固定班次时跳过（历史事实优先）。
    o2s = shift_by_code.get("O2")
    ds = shift_by_code.get("D")

    trans_pen = []

    if o2s:
        for e in employees:
            for i in range(len(dates) - 1):
                d1, d2 = dates[i], dates[i + 1]

                if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                    continue

                model.Add(
                    x[e.id, d1, o2s.id] + x[e.id, d2, o2s.id] <= 1
                )

    if ds:
        for e in employees:
            for i in range(len(dates) - 1):
                d1, d2 = dates[i], dates[i + 1]

                if (e.id, d1) in fixed_map and (e.id, d2) in fixed_map:
                    continue

                model.Add(
                    x[e.id, d1, ds.id] + x[e.id, d2, ds.id] <= 1
                )

    if o2s and o3s:
        for e in employees:
            ei = employee_index[e.id]

            for i in range(len(dates) - 1):
                d1, d2 = dates[i], dates[i + 1]

                # O3→O2 惩罚
                p = model.NewBoolVar(f"o32_{ei}_{i}")
                model.AddBoolOr([~p, x[e.id, d1, o3s.id]])
                model.AddBoolOr([~p, x[e.id, d2, o2s.id]])
                model.AddBoolOr([p, ~x[e.id, d1, o3s.id], ~x[e.id, d2, o2s.id]])
                trans_pen.append(p)

    # O1 尽量安排（填充班次，不要求均等）
    o1 = shift_by_code.get("O1")

    o1_total = []

    if o1:
        for e in employees:
            for d in dates:
                o1_total.append(
                    x[e.id, d, o1.id]
                )

    # 综合目标（用户确认优先级）：
    #   班次均等（每差 1 个扣 300，主导项，O1 除外）
    #   = 夜班 3 连块（每个碎片惩罚 -300：孤立/2 连断头/N_N 缺口，
    #     【用户反馈提权】跨月真实边界导致相位被打乱，-150 压不住覆盖对抗）
    #   > O3 连续块（每对 +30——O3 连续性优先，用户明确要求）
    #   > O3→O2 减少（每次 -50）
    #   > 班次连续块（每对 +2，O2/D/O3 除外——O3 走专属权重）
    #   > O1 尽量安排（每班 +1，允许不均）
    # 【新增 2026-09-21】O1 工作日软目标（**不是硬约束**）：
    # 每个工作日把 O1 填到 O1_TARGET_WORKDAY 人，填满即止。
    # 背景：O1 是弹性补充班次，但只靠 +1 的软奖励时工作日 O1 只有 5~6 人、最少 1 人，
    # 于是"只会 O1"的员工排不满工作日（用户实测）。做成"封顶的软奖励"后，
    # 人手不足时依然不会 INFEASIBLE（只是填不到目标）。
    O1_TARGET_WORKDAY = int(rules["o1_target_workday"])
    # WORKDAY_FILL_TARGET 已在函数开头定义（按团队规模自动缩放）
    o1_capped_terms = []
    day_capped_terms = []
    o1_row_cap = shift_by_code.get("O1")

    for d in dates:
        hol_cap = holidays.get(d)

        if hol_cap is not None and hol_cap.day_type == "HOLIDAY":
            continue

        if d.weekday() >= 5:
            continue

        # ① 每日在岗总人数填到目标（这是"工作日排满"的主杠杆）
        headcount = sum(1 - off[e.id, d] for e in employees)
        day_cap = model.NewIntVar(0, WORKDAY_FILL_TARGET, f"daycap_{d}")
        model.Add(day_cap <= headcount)
        model.Add(day_cap >= headcount - (len(employees) - WORKDAY_FILL_TARGET))
        day_capped_terms.append(day_cap)

        # ② O1 单独填到目标（否则 solver 可能用 O3 去凑总人数，
        #    而"只会 O1"的员工仍然排不满工作日）
        if o1_row_cap is not None:
            o1_total_day = sum(x[e.id, d, o1_row_cap.id] for e in employees)
            capped = model.NewIntVar(0, O1_TARGET_WORKDAY, f"o1cap_{d}")
            model.Add(capped <= o1_total_day)
            model.Add(capped >= o1_total_day - (len(employees) - O1_TARGET_WORKDAY))
            o1_capped_terms.append(capped)

    objective_terms = []

    # 【新增】工作日填充软奖励（人手不足时自然填不满，不会无解）：
    #   ① 每日在岗总人数填到 (人数-2)
    #   ② 每工作日 O1 填到 12 人
    # 【权重 2026-09-22 提高】每日填充奖励 10 → 60，O1 填充 10 → 30。
    # 原因：权重太小时 solver 认为"哪天填满都行"，会把休息堆在月初 ——
    # 实测 2026-12 的 11/26 只有 11 人在岗（O1 仅 4 人），而 12/07 之后是 14~15 人。
    # 提高后"每个工作日都填到目标"才有足够引力。
    if day_capped_terms:
        objective_terms.append(int(rules["weight_fill_day"]) * sum(day_capped_terms))

    if o1_capped_terms:
        objective_terms.append(int(rules["weight_fill_o1"]) * sum(o1_capped_terms))

    if same_pairs:
        objective_terms.append(int(rules["weight_same_pair"]) * sum(same_pairs))

    if o1_total:
        objective_terms.append(int(rules["weight_o1_bonus"]) * sum(o1_total))

    for fd in fair_diffs:
        objective_terms.append(-int(rules["weight_fair_shift"]) * fd)

    # 【权重 2026-09-21 三次调整】公平目标 = 压极差 + 抬高地板：
    #   -800/天 压极差（班次不能差太多）；
    #   +600/天 奖励"最低班次"min_days —— 只压极差会让 solver 停在"人人 19 班"
    #   的均衡上：给任何人多加 1 个班都会破坏均衡（-800 远大于每日填充奖励 +10），
    #   于是工作日填不满、所有人的班次也上不去（用户实测"O1 人员排班又缺了"）。
    #   加上"抬高地板"后，所有人一起多上一天成为纯收益，工作日自然被填满。
    objective_terms.append(-int(rules["weight_day_spread"]) * day_spread)
    # 【权重再提】+1500/天：让"抬高地板"成为主导项，把每人班次推到上限
    #（17 人的上限≈工作日数 22；此前 +600 不足以压过其它软项，地板停在 19~21）
    objective_terms.append(int(rules["weight_min_days"]) * min_days)

    # 【工时项 2026-09-21 二次调整】工时偏离基准**不再进入目标函数**，只作为报告指标。
    # 原因：夜班 12h、白班 8h，只要工时项存在，solver 就会在"班次均等"与"工时贴近基准"
    # 之间停留在边际平衡点（实测权重 -400 / -800 时班次极差都还是 5）。
    # 业务口径是"班次数相当、夜班人员工时略高"
    #（真实 2026-10：员工甲 16 班 128h、员工乙 16 班 140h），
    # 因此公平目标只保留"出勤天数均等"；工时在 verify_schedule.py 里单独对比。
    # align_gaps / normal_hours / bench_avg 仍保留计算，便于日后需要时恢复权重。
    _unused_align = sum(align_gaps) if align_gaps else 0

    if night_pen:
        night_pen_total = model.NewIntVar(0, len(night_pen), "night_pen_total")
        model.Add(night_pen_total == sum(night_pen))
        objective_terms.append(-int(rules["weight_night_fragment"]) * night_pen_total)

    if o3_pairs:
        objective_terms.append(int(rules["weight_o3_pair"]) * sum(o3_pairs))

    if trans_pen:
        objective_terms.append(-int(rules["weight_trans_pen"]) * sum(trans_pen))

    model.Maximize(sum(objective_terms))

    solver = cp_model.CpSolver()

    solver.parameters.max_time_in_seconds = int(rules["solver_time_limit"])
    solver.parameters.num_search_workers = int(rules["solver_workers"])
    # 固定随机种子：同参数同数据产出可复现的结果（多 worker 并行搜索
    # 的结果有随机波动，不固定会导致"同样约束时而好时而碎片化"）
    solver.parameters.random_seed = 42

    # 【新增 2026-09-21】热启动：用上一次的排班作为初始提示。
    # 目的：改一条权限/一个人再重排时，从旧解出发更快收敛到同等或更好的解，
    # 从而可以把求解时限从 120s 降下来（用户反馈"重新生成很慢"）。
    hint_rows = db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == period.id,
            ScheduleAssignment.source == "AUTO",
        )
    ).all()

    active_shift_ids = {s.id for s in shifts}
    hinted = 0

    for r in hint_rows:
        if r.employee_id not in employee_index or r.work_date not in date_index:
            continue

        if (r.employee_id, r.work_date) in fixed_map:
            continue

        if r.shift_id is None:
            model.AddHint(off[r.employee_id, r.work_date], 1)
            hinted += 1
        elif r.shift_id in active_shift_ids:
            model.AddHint(x[r.employee_id, r.work_date, r.shift_id], 1)
            hinted += 1

    if hinted:
        print(f"[WARMSTART] 以上一次排班的 {hinted} 格作为初始提示")

    status = solver.Solve(model)

    if status not in (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
    ):
        # 【2026-09-21 修复】无解时必须给出**可操作的原因**，并且不要把已发布周期
        # 直接改成 INFEASIBLE —— 否则"改了权限重新排班"看起来完全没变化
        #（求解失败不改写排班数据，运营会误以为功能坏了）。
        # 真实案例：5 名只会 O1 的员工被停用后在职仅 17 人，
        # 而工作日需求 O1 12 + O2 2 + O3 3 + N 2 = 19 人 ⇒ 必然无解。
        diagnosis = []
        active_n = len(employees)

        for code, need in (("O2", 2), ("O3", 3), ("N", 2)):
            row = shift_by_code.get(code)

            if row is None:
                diagnosis.append(f"班次 {code} 未定义（请检查 init_data）")
                continue

            allowed_n = sum(
                1 for e in employees if (e.id, row.id) in allowed_pairs
            )

            if allowed_n == 0:
                diagnosis.append(f"{code} 无任何人有权限（每工作日需要 {need} 人）")

        # O1 是弹性班次，不计入硬性在岗需求
        total_need = 2 + 3 + 2

        if active_n < total_need:
            diagnosis.append(
                f"工作日在岗需求合计 {total_need} 人 > 在职员工 {active_n} 人"
                f"（缺口 {total_need - active_n} 人）——请恢复在职人员，"
                f"或下调工作日覆盖（O1/O2/O3/N 的人数要求）"
            )

        rest_blocked = 0
        for e in employees:
            st = load_previous_boundary(db, period, e)

            if (st.get("night_rest_remaining") or 0) > 0:
                rest_blocked += 1

        if rest_blocked:
            diagnosis.append(
                f"上月夜班连休未结束的有 {rest_blocked} 人，周期首日可用人数相应减少"
            )

        for code, need in (("O2", 2), ("O3", 3), ("N", 2)):
            row = shift_by_code.get(code)

            if row is None:
                continue

            allowed_n = sum(
                1 for e in employees
                if (e.id, row.id) in allowed_pairs
                and not is_leave(e.id, period.start_date, leaves)
            )

            if 0 < allowed_n < need:
                diagnosis.append(
                    f"{code} 周期首日有权限且未请假的仅 {allowed_n} 人 < 需求 {need} 人"
                )

        # 【2026-10-26】指定休息 / 请假的影响
        if leaves:
            leave_person_days = 0
            night_leave_person_days = 0
            night_shift_id_set = {s.id for s in night_shifts}

            for e in employees:
                for lv in leaves.get(e.id, []):
                    d0 = max(lv.start_date, period.start_date)
                    d1 = min(lv.end_date, period.end_date)

                    if d1 < d0:
                        continue

                    n_days = (d1 - d0).days + 1
                    leave_person_days += n_days

                    if any(sid in permissions[e.id] for sid in night_shift_id_set):
                        night_leave_person_days += n_days

            if leave_person_days:
                diagnosis.append(
                    f"本周期指定休息/请假共 {leave_person_days} 人天"
                    f"（其中夜班人员 {night_leave_person_days} 人天）"
                )

        # 【2026-10-26】工作日最低在岗地板偏高的提示
        fill_target_hint = max(1, len(employees) - int(rules["workday_headcount_slack"]))
        floor_hint = min(
            fill_target_hint - 1,
            max(0, len(employees) - int(rules["workday_min_headcount_slack"])),
        )

        if floor_hint > 0:
            diagnosis.append(
                f"工作日要求至少 {floor_hint} 人/天在岗（在职 {len(employees)} 人、"
                f"填充目标 {fill_target_hint}）；夜班轮换期间部分人强制休息不能上白班，"
                "若因此无解，请调小「工作日最低在岗的容差」或增加在职人数"
            )

        # 单月夜班上限的容量体检
        if month_cap > 0:
            cap_people = sum(
                1
                for e in employees
                if any((e.id, s.id) in allowed_pairs for s in night_shifts)
            )
            need_night = 0

            for d in dates:
                hol = holidays.get(d)

                if hol and hol.day_type == "HOLIDAY":
                    need_night += int(rules["coverage_holiday"].get("N", 0))
                elif d.weekday() >= 5:
                    need_night += int(rules["coverage_weekend"].get("N", 0))
                else:
                    need_night += int(rules["coverage_workday"].get("N", 0))

            if cap_people * month_cap < need_night:
                diagnosis.append(
                    f"单月夜班上限 {month_cap} 次 × 有夜班权限 {cap_people} 人 = "
                    f"{cap_people * month_cap} 个夜班，少于本周期夜班需求 {need_night} 个，"
                    "请把上限调高或给更多人开通夜班权限"
                )

        if not diagnosis:
            diagnosis.append(
                "逐项检查未发现明显缺口，可能是夜班连班/强制休息与覆盖冲突、"
                "或锁定/历史班次与覆盖冲突；可用 dis=carry,nightrest,niso 逐个排除"
            )

        previous_status = period.status
        # 已发布/已求解的周期保留原状态（数据未被改写），只把 dependency_status 标红
        if previous_status in ("PUBLISHED", "SOLVED", "VALIDATED"):
            period.dependency_status = "INFEASIBLE"
            db.add(
                AuditLog(
                    action="SOLVE_INFEASIBLE",
                    entity_type="SchedulePeriod",
                    entity_id=period.id,
                    after_data={"period": period.name, "diagnosis": diagnosis},
                )
            )
        else:
            period.status = "INFEASIBLE"

        db.commit()

        return {
            "status": "INFEASIBLE",
            "message": "没有找到满足硬约束的排班（原排班数据未被修改）",
            "kept_status": period.status,
            "diagnosis": diagnosis,
            "active_employees": active_n,
        }

    # 清除旧的 AUTO 且未锁定的 assignments（HISTORY/MANUAL/locked 保留）
    old = db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == period.id,
            ScheduleAssignment.locked.is_(False),
            ScheduleAssignment.source == "AUTO",
        )
    ).all()

    for row in old:
        db.delete(row)

    db.flush()

    # 保存结果（固定格跳过：locked / HISTORY / MANUAL 保留原 assignment）
    for e in employees:
        for d in dates:

            if (e.id, d) in fixed_map:
                continue

            assigned = False

            for s in shifts:
                value = solver.Value(
                    x[e.id, d, s.id]
                )

                if value:
                    db.add(
                        ScheduleAssignment(
                            period_id=period.id,
                            employee_id=e.id,
                            work_date=d,
                            shift_id=s.id,
                            status="WORK",
                            source="AUTO",
                        )
                    )
                    assigned = True
                    break

            if not assigned:
                db.add(
                    ScheduleAssignment(
                        period_id=period.id,
                        employee_id=e.id,
                        work_date=d,
                        shift_id=None,
                        status="OFF",
                        source="AUTO",
                    )
                )

    # 生成期末 Boundary State，供下一个周期 Solver 作为 prefix 输入
    # 【关键修复】SessionLocal 配置 autoflush=False：generate 内部的查询
    # 读不到本事务 pending 的新 assignments，导致 Boundary 全 0（期末
    # 夜班块连续数=0），下周期跨月延续失效——月度衔接断裂的真正根因。
    db.flush()
    generate_boundary_states(db, period)

    period.status = "SOLVED"
    period.generated_at = datetime.utcnow()

    db.commit()

    return {
        "status": "SOLVED",
        "objective": solver.ObjectiveValue(),
        "wall_time": solver.WallTime(),
    }
