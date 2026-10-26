import re
from datetime import date, timedelta

import openpyxl
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    Employee,
    Team,
    Shift,
    EmployeeShiftPermission,
    SchedulePeriod,
    ScheduleAssignment,
)
from .scheduler.state import generate_boundary_states


VALID_SHIFTS = {"O1", "O2", "O3", "C", "D", "N", "Y"}

# 真实 Excel 每个月份块底部的统计/汇总行会把 N/O2/总数 等标签放在
# Engineer 列或 D 列，必须跳过，否则会被当成员工导入。
SUMMARY_LABELS = {
    "总数", "补休", "排班表加班", "法定三倍", "法定两倍", "其它加班",
    "合计", "合计加班", "小计", "Engineer",
}

TITLE_RE = re.compile(r"(\d{4})年(\d{1,2})月")


def normalize_shift(value):
    if value is None:
        return None

    value = str(value).strip()

    if value in VALID_SHIFTS:
        return value

    if value == "O4":
        return "O4_INVALID"

    return None


def find_layout(ws, rows):
    """在给定行里找 Engineer/Email 表头列。

    新表：A=Engineer, B=Email；旧表：C=Engineer, D=Email。
    找不到时返回旧布局默认值 (3, 4)。
    """
    for r in rows:
        for c in range(1, 9):
            v = ws.cell(r, c).value
            if isinstance(v, str) and v.strip().lower() == "engineer":
                ve = ws.cell(r, c + 1).value
                if isinstance(ve, str) and "email" in ve.strip().lower():
                    return c, c + 1
                return c, None
    return 3, 4


def read_date_nums(ws, row, col_start):
    """从 row 行 col_start 列起连续读取日期数字，返回 [(col, n)]。"""
    nums = []
    col = col_start

    while True:
        v = ws.cell(row, col).value
        if v is None:
            break
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            break
        if not (1 <= n <= 31):
            break
        nums.append((col, n))
        col += 1

    return nums


def split_main_extra(nums):
    """提取主块（26→25）日期序列，忽略尾部统计列数字。

    主块验证：首列≥26、末列≤25、序列逐日吻合（31→1 回绕）。
    【用户纠正】尾部 01/02/03 列是 O1/O2/O3 班次统计列，不是日期——
    主块之后的数字一律丢弃。
    """
    def try_main(seq):
        if len(seq) < 28:
            return None
        if seq[0] < 26 or seq[-1] > 25:
            return None
        return seq

    # 全量即主块（无统计列的表）
    full = [n for _c, n in nums]
    if try_main(full):
        return nums, []

    # 前缀截断：跳过尾部统计列数字
    for k in range(len(nums) - 1, 27, -1):
        main = [n for _c, n in nums[:k]]
        if try_main(main):
            return nums[:k], nums[k:]

    return None, None


def reconstruct_block(ws, rows_to_try, col_start, title):
    """从候选行读日期数字并重建 26→25 周期。

    返回 dict(start, end, dates, stat_cols) 或 None。
    约定：title 月 = 周期结束月（文档第 46 节）。
    stat_cols：主块之后的统计列 {col: shift_code}（列头 01/02/03 → O1/O2/O3），
    用于导入后核对每人班次统计数。
    """
    if not title:
        return None

    end_year, end_month = title

    for row in rows_to_try:
        nums = read_date_nums(ws, row, col_start)

        if len(nums) < 28:
            continue

        main, _ignored = split_main_extra(nums)

        if not main:
            continue

        main_nums = [n for _c, n in main]
        last_col = main[-1][0]

        if end_month == 1:
            start_year, start_month = end_year - 1, 12
        else:
            start_year, start_month = end_year, end_month - 1

        try:
            start = date(start_year, start_month, main_nums[0])
            end = date(end_year, end_month, main_nums[-1])
        except ValueError:
            continue

        span = (end - start).days + 1

        if span != len(main_nums):
            continue

        d = start
        ok = True
        for n in main_nums:
            if d.day != n:
                ok = False
                break
            d += timedelta(days=1)

        if not ok:
            continue

        dates = [start + timedelta(days=i) for i in range(span)]

        # 统计列：主块之后列头为班次代码（O1/O2/O3/O4/D/N/C/Y 文本）
        stat_cols = {}
        col = last_col + 1
        while True:
            v = ws.cell(row, col).value
            if v is None:
                break
            code = str(v).strip() if v is not None else ""
            if code in VALID_SHIFTS:
                stat_cols[col] = code
                col += 1
            else:
                break

        return {
            "start": start,
            "end": end,
            "dates": dates,
            "stat_cols": stat_cols,
        }

    return None


def parse_blocks(ws):
    """解析垂直多月份块结构（兼容新旧列布局）。

    每块 = 标题行（列1-3 含 XXXX年X月）→ 表头/日期行 → 员工行 → 汇总行。
    返回块列表：[{title, start, end, dates, extra_dates,
                  rows=[(tier, name, email, row_codes, row)], col_shift}]
    """
    blocks = []
    current = None
    col_name, col_email = 3, 4
    col_shift = 5

    for row in range(1, ws.max_row + 1):
        # 块起点：列 1-3 匹配标题
        title = None
        for c in (1, 2, 3):
            a = ws.cell(row, c).value
            m = TITLE_RE.search(str(a)) if a is not None else None
            if m:
                title = (int(m.group(1)), int(m.group(2)))
                break

        if title:
            # 布局检测：标题行与下一行找 Engineer/Email
            col_name, col_email = find_layout(ws, (row, row + 1))
            col_shift = (col_email or col_name) + 1

            info = reconstruct_block(ws, (row, row + 1), col_shift, title)

            current = {
                "title": title,
                "start": info["start"] if info else None,
                "end": info["end"] if info else None,
                "dates": info["dates"] if info else None,
                "stat_cols": info["stat_cols"] if info else {},
                "rows": [],
                "col_shift": col_shift,
            }
            blocks.append(current)
            continue

        if current is None:
            continue

        name = ws.cell(row, col_name).value

        if not isinstance(name, str) or not name.strip():
            continue

        name = name.strip()

        if name in SUMMARY_LABELS or name.upper() in VALID_SHIFTS:
            continue

        tier = ws.cell(row, col_name - 1).value if col_name > 1 else None
        if isinstance(tier, str) and ("email" in tier.lower() or "engineer" in tier.lower()):
            tier = None

        email = ws.cell(row, col_email).value if col_email else None
        email = str(email).strip() if email is not None else None

        row_codes = {}

        for col in range(col_shift, col_shift + 31):
            v = normalize_shift(ws.cell(row, col).value)
            if v and v != "O4_INVALID":
                row_codes[col] = v

        # 记录行号作为人员显示顺序（用户表格顺序）
        current["rows"].append((tier, name, email, row_codes, row))

    return blocks


def import_excel(db: Session, path: str, with_history: bool = True):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]

    blocks = parse_blocks(ws)

    team = db.scalar(
        select(Team).where(Team.code == "CSE")
    )

    if not team:
        team = Team(name="CSE Team", code="CSE")
        db.add(team)
        db.flush()

    shift_map = {
        x.code: x
        for x in db.scalars(select(Shift)).all()
    }

    # 一次性加载员工与已有权限对，消除 N+1 查询，
    # 并解决 autoflush=False 时 SELECT 看不到未提交权限行导致的
    # employee_shift_permissions 主键冲突（同一员工多行/多块必触发）。
    employees_by_no = {
        x.employee_no: x
        for x in db.scalars(
            select(Employee).where(Employee.team_id == team.id)
        ).all()
    }

    perm_pairs = {
        (p.employee_id, p.shift_id)
        for p in db.scalars(select(EmployeeShiftPermission)).all()
    }

    warnings = []
    warn_seen = set()

    # ---- 员工 upsert + 权限推断（跨所有月份块）----
    observed_by_employee = {}
    employees_in_order = []

    for block in blocks:
        for tier, name, email, row_codes, row_no in block["rows"]:
            employee_no = email or name

            employee = employees_by_no.get(employee_no)

            if not employee:
                employee = Employee(
                    team_id=team.id,
                    employee_no=employee_no,
                    name=name,
                    email=email,
                    legacy_tier=str(tier) if tier else None,
                    sort_order=row_no,
                )
                db.add(employee)
                db.flush()
                employees_by_no[employee_no] = employee
            elif email and not employee.email:
                employee.email = email

            if employee.id not in observed_by_employee:
                employees_in_order.append(employee)
                observed_by_employee[employee.id] = set()

            for col, code in row_codes.items():
                observed_by_employee[employee.id].add(code)

    # O4 检测（parse_blocks 只保留了有效班次）
    for row in range(1, ws.max_row + 1):
        for col in range(1, 40):
            if normalize_shift(ws.cell(row, col).value) == "O4_INVALID":
                name = ws.cell(row, 3).value
                key = f"O4:{name}"
                if name and key not in warn_seen:
                    warn_seen.add(key)
                    warnings.append(
                        f"{name}: Excel 中发现历史 O4，系统不会将 O4 作为有效班次"
                    )

    for employee in employees_in_order:
        for code in observed_by_employee[employee.id]:
            shift = shift_map.get(code)
            if not shift:
                continue

            if (employee.id, shift.id) in perm_pairs:
                continue

            db.add(
                EmployeeShiftPermission(
                    employee_id=employee.id,
                    shift_id=shift.id,
                    allowed=True,
                )
            )
            perm_pairs.add((employee.id, shift.id))

    # ---- 历史排班入库：每个月份块一个周期 + 尾部延伸归下月周期 ----
    history_blocks = []

    def upsert_period(start, end):
        period = db.scalar(
            select(SchedulePeriod).where(
                SchedulePeriod.team_id == team.id,
                SchedulePeriod.start_date == start,
                SchedulePeriod.end_date == end,
            )
        )
        if period:
            return period

        pname = f"{end.year}-{end.month:02d}"
        conflict = db.scalar(
            select(SchedulePeriod).where(
                SchedulePeriod.team_id == team.id,
                SchedulePeriod.name == pname,
            )
        )
        if conflict:
            pname = f"{pname}-XLSX"

        period = SchedulePeriod(
            team_id=team.id,
            name=pname,
            start_date=start,
            end_date=end,
            status="HISTORY",
        )
        db.add(period)
        db.flush()
        return period

    def write_history(period, history_map):
        db.execute(
            delete(ScheduleAssignment).where(
                ScheduleAssignment.period_id == period.id,
                ScheduleAssignment.source == "HISTORY",
            )
        )

        for (emp_id, d), sid in history_map.items():
            db.add(
                ScheduleAssignment(
                    period_id=period.id,
                    employee_id=emp_id,
                    work_date=d,
                    shift_id=sid,
                    status="WORK",
                    source="HISTORY",
                )
            )

        db.flush()
        return len(history_map)

    if with_history:
        for block in blocks:
            if not block["rows"]:
                continue

            if not block["dates"]:
                warnings.append(
                    f"{block['title'][0]}年{block['title'][1]}月块："
                    "未能从标题行日期重建 26→25 周期，该块班次未入库"
                )
                continue

            period = upsert_period(block["start"], block["end"])

            # 主块：(col - col_shift) → dates 下标
            history_map = {}

            for tier, name, email, row_codes, row_no in block["rows"]:
                employee_no = email or name
                employee = employees_by_no.get(employee_no)
                if not employee:
                    continue

                for col, code in row_codes.items():
                    idx = col - block["col_shift"]
                    if idx >= len(block["dates"]):
                        continue
                    shift = shift_map.get(code)
                    if not shift:
                        continue
                    history_map[(employee.id, block["dates"][idx])] = shift.id

            count = write_history(period, history_map)
            boundary_count = generate_boundary_states(db, period)

            history_blocks.append(
                {
                    "period": period.name,
                    "start_date": str(block["start"]),
                    "end_date": str(block["end"]),
                    "assignments": count,
                    "boundary_states": boundary_count,
                }
            )

            # 【用户纠正】尾部 01/02/03 列 = O1/O2/O3 班次统计列（非日期）。
            # 用表格自带的统计数核对导入结果，不一致则报警。
            if block.get("stat_cols"):
                for tier, name, email, row_codes, row_no in block["rows"]:
                    employee_no = email or name
                    employee = employees_by_no.get(employee_no)
                    if not employee:
                        continue

                    for col, code in block["stat_cols"].items():
                        expected_raw = ws.cell(row_no, col).value
                        try:
                            expected = int(str(expected_raw).strip())
                        except (TypeError, ValueError):
                            continue

                        actual = sum(
                            1
                            for (eid, _d), sid in history_map.items()
                            if eid == employee.id and shift_map[sid].code == code
                        )

                        if actual != expected:
                            warnings.append(
                                f"统计校验：{name} {code} 表格计数 {expected} ≠ 导入 {actual}"
                            )

    db.commit()

    return {
        "status": "ok",
        "employees_imported": len(employees_in_order),
        "warnings": warnings,
        "title": blocks[0]["title"] if blocks else None,
        "history": history_blocks[0] if history_blocks else None,
        "history_blocks": history_blocks,
        "blocks_total": len(blocks),
    }
