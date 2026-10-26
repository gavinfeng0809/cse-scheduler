import os
import re
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    Employee,
    Shift,
    Team,
    Holiday,
    SchedulePeriod,
    ScheduleAssignment,
    EmployeeShiftPermission,
    EmployeeLeave,
    AuditLog,
    ScheduleRevision,
)
from .revisions import snapshot_period, restore_snapshot, list_revisions
from .rules import load_rules, save_rules, reset_rule, rule_specs_meta
from .importer import import_excel
from .exporter import export_period
from .scheduler.solver import solve_period
from .scheduler.validator import validate_period
from .auth import get_current_user


router = APIRouter(dependencies=[Depends(get_current_user)])


def parse_period_id(period_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(period_id)
    except ValueError:
        raise HTTPException(400, "非法 period id")


def safe_filename(name: str) -> str:
    """【修复】文档直接拼接 file.filename，含 ../ 时可路径穿越写任意文件。"""
    base = os.path.basename(str(name).replace("\\", "/"))
    base = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff.\-() ]", "_", base).strip()
    return base or "upload.xlsx"


def _night_shift_ids(db: Session) -> set:
    """计入"连班"的夜班班次 id（N；Y 不计入连班）。夜班策略只对这些班次有意义。"""
    return {
        s.id
        for s in db.scalars(
            select(Shift).where(Shift.counts_as_night_streak.is_(True))
        )
    }


def _night_permitted_employee_ids(db: Session) -> set:
    """拥有夜班(N)权限的员工 id 集合。"""
    night_ids = _night_shift_ids(db)

    if not night_ids:
        return set()

    return {
        p.employee_id
        for p in db.scalars(
            select(EmployeeShiftPermission).where(
                EmployeeShiftPermission.allowed.is_(True)
            )
        )
        if p.shift_id in night_ids
    }


def _cse_team(db: Session) -> Team:
    team = db.scalar(select(Team).where(Team.code == "CSE"))
    if not team:
        raise HTTPException(500, "CSE Team 不存在，请先运行 python -m app.init_data")
    return team


def _period_dates(period: SchedulePeriod):
    from datetime import timedelta

    dates = []
    d = period.start_date
    while d <= period.end_date:
        dates.append(d)
        d += timedelta(days=1)
    return dates


@router.get("/employees")
def employees(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Employee).order_by(Employee.sort_order, Employee.name)
    ).all()

    night_permitted = _night_permitted_employee_ids(db)

    return [
        {
            "id": str(x.id),
            "name": x.name,
            "email": x.email,
            "employee_no": x.employee_no,
            "tier": x.legacy_tier,
            "active": x.active,
            "sort_order": x.sort_order,
            "night_max_consecutive": x.night_max_consecutive,
            "night_rest_days": x.night_rest_days,
            "night_policy_confirmed": x.night_policy_confirmed,
            # 【联动】没有夜班(N)权限的人不会被排夜班，其夜班策略无意义
            "has_night_permission": x.id in night_permitted,
        }
        for x in rows
    ]


class EmployeeIn(BaseModel):
    name: str
    email: str | None = None
    tier: str | None = None


@router.post("/employees")
def create_employee(body: EmployeeIn, db: Session = Depends(get_db)):
    """新增员工：默认开通全部班次权限（可在权限页调整）。"""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "姓名不能为空")

    email = body.email.strip() if body.email else None
    employee_no = email or name
    team = _cse_team(db)

    conflict = db.scalar(
        select(Employee).where(
            Employee.team_id == team.id,
            Employee.employee_no == employee_no,
        )
    )
    if conflict:
        raise HTTPException(400, f"已存在同名/同邮箱员工：{conflict.name}")

    emp = Employee(
        team_id=team.id,
        employee_no=employee_no,
        name=name,
        email=email,
        legacy_tier=body.tier,
        active=True,
        sort_order=(db.scalar(select(func.max(Employee.sort_order))) or 0) + 1,
    )
    db.add(emp)
    db.flush()

    # 默认全部班次可上，避免新员工无法被排班
    for s in db.scalars(select(Shift)).all():
        db.add(
            EmployeeShiftPermission(
                employee_id=emp.id, shift_id=s.id, allowed=True
            )
        )

    db.commit()
    db.refresh(emp)

    return {
        "id": str(emp.id), "name": emp.name, "email": emp.email,
        "tier": emp.legacy_tier, "active": emp.active,
    }


@router.put("/employees/{employee_id}")
def update_employee(employee_id: str, body: EmployeeIn, db: Session = Depends(get_db)):
    emp = db.get(Employee, uuid.UUID(employee_id))
    if not emp:
        raise HTTPException(404, "员工不存在")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "姓名不能为空")

    emp.name = name
    emp.email = body.email.strip() if body.email else None
    if body.tier is not None:
        emp.legacy_tier = body.tier

    db.commit()
    return {
        "id": str(emp.id), "name": emp.name, "email": emp.email,
        "tier": emp.legacy_tier, "active": emp.active,
    }


@router.delete("/employees/{employee_id}")
def deactivate_employee(employee_id: str, db: Session = Depends(get_db)):
    """停用（软删除）：历史排班保留，但不再参与后续自动排班。"""
    emp = db.get(Employee, uuid.UUID(employee_id))
    if not emp:
        raise HTTPException(404, "员工不存在")

    emp.active = False
    db.commit()
    return {"status": "deactivated", "employee": emp.name}


@router.post("/employees/{employee_id}/activate")
def activate_employee(employee_id: str, db: Session = Depends(get_db)):
    emp = db.get(Employee, uuid.UUID(employee_id))
    if not emp:
        raise HTTPException(404, "员工不存在")

    emp.active = True
    db.commit()
    return {"status": "activated", "employee": emp.name}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    """仪表盘：已发布周期的排班概览。"""
    team = _cse_team(db)

    published = db.scalars(
        select(SchedulePeriod)
        .where(SchedulePeriod.status == "PUBLISHED")
        .order_by(SchedulePeriod.start_date.desc())
    ).all()

    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    holiday_dates = {
        x.holiday_date
        for x in db.scalars(
            select(Holiday).where(
                Holiday.team_id == team.id, Holiday.day_type == "HOLIDAY"
            )
        )
    }

    result = []

    for p in published:
        rows = db.scalars(
            select(ScheduleAssignment).where(
                ScheduleAssignment.period_id == p.id
            )
        ).all()

        total = night = weekend = holiday_cnt = 0
        duty = set()

        for row in rows:
            if row.status != "WORK" or not row.shift_id:
                continue
            s = shifts.get(row.shift_id)
            if not s:
                continue
            total += 1
            duty.add(row.employee_id)
            if s.is_night:
                night += 1
            if row.work_date in holiday_dates:
                holiday_cnt += 1
            elif row.work_date.weekday() >= 5:
                weekend += 1

        result.append(
            {
                "id": str(p.id),
                "name": p.name,
                "start_date": str(p.start_date),
                "end_date": str(p.end_date),
                "published_at": str(p.published_at) if p.published_at else None,
                "total_shifts": total,
                "night_shifts": night,
                "weekend_shifts": weekend,
                "holiday_shifts": holiday_cnt,
                "employees_count": len(duty),
            }
        )

    return result


@router.get("/shifts")
def shifts(db: Session = Depends(get_db)):
    rows = db.scalars(select(Shift).order_by(Shift.code)).all()
    return [
        {
            "id": str(x.id),
            "code": x.code,
            "name": x.name,
            "start": x.start_time.isoformat(),
            "end": x.end_time.isoformat(),
            "paid_hours": float(x.paid_hours),
            "night": x.is_night,
        }
        for x in rows
    ]


@router.get("/permissions")
def get_permissions(db: Session = Depends(get_db)):
    """班次权限矩阵（文档 §22/44：历史推断只是初始权限，必须人工确认）。"""
    employees = db.scalars(
        select(Employee)
        .where(Employee.active.is_(True))
        .order_by(Employee.sort_order, Employee.name)
    ).all()

    shifts = db.scalars(select(Shift).order_by(Shift.code)).all()

    perms = {
        (p.employee_id, p.shift_id): p.allowed
        for p in db.scalars(select(EmployeeShiftPermission)).all()
    }

    return {
        "employees": [
            {"id": str(e.id), "name": e.name, "email": e.email or ""}
            for e in employees
        ],
        "shifts": [
            {"id": str(s.id), "code": s.code, "name": s.name, "night": s.is_night}
            for s in shifts
        ],
        "matrix": {
            str(e.id): {
                str(s.id): bool(perms.get((e.id, s.id), False))
                for s in shifts
            }
            for e in employees
        },
    }


@router.put("/permissions/{employee_id}")
def set_permissions(
    employee_id: str,
    allowed_shift_ids: list[str],
    db: Session = Depends(get_db),
):
    """整批替换某员工的班次权限（幂等）。"""
    emp = db.get(Employee, uuid.UUID(employee_id))
    if not emp:
        raise HTTPException(404, "员工不存在")

    valid_shifts = {
        s.id
        for s in db.scalars(select(Shift)).all()
    }

    allowed_ids = []
    for sid in allowed_shift_ids:
        suid = uuid.UUID(sid)
        if suid not in valid_shifts:
            raise HTTPException(400, f"非法班次 id {sid}")
        allowed_ids.append(suid)

    # 幂等：删旧插新
    db.execute(
        delete(EmployeeShiftPermission).where(
            EmployeeShiftPermission.employee_id == emp.id
        )
    )

    for suid in allowed_ids:
        db.add(
            EmployeeShiftPermission(
                employee_id=emp.id,
                shift_id=suid,
                allowed=True,
            )
        )

    # 【联动】若该员工没有夜班(N)权限，则清空其夜班策略（恢复为规则默认值并置为未确认）
    night_reset = False

    if not (set(allowed_ids) & _night_shift_ids(db)):
        team = _cse_team(db)
        rules = load_rules(db, team.id)
        default_max = int(rules["night_default_max_consecutive"])
        default_rest = int(rules["night_default_rest_days"])

        if (
            emp.night_max_consecutive != default_max
            or emp.night_rest_days != default_rest
            or emp.night_policy_confirmed
        ):
            emp.night_max_consecutive = default_max
            emp.night_rest_days = default_rest
            emp.night_policy_confirmed = False
            night_reset = True

    db.commit()

    return {
        "status": "ok",
        "employee": emp.name,
        "allowed": len(allowed_ids),
        "has_night_permission": bool(set(allowed_ids) & _night_shift_ids(db)),
        "night_policy_reset": night_reset,
    }


@router.get("/holidays")
def list_holidays(db: Session = Depends(get_db)):
    team = _cse_team(db)
    rows = db.scalars(
        select(Holiday)
        .where(Holiday.team_id == team.id)
        .order_by(Holiday.holiday_date)
    ).all()
    return [
        {
            "id": str(x.id),
            "date": str(x.holiday_date),
            "name": x.name,
            "day_type": x.day_type,
            "pay_rate": int(x.pay_rate or 3),
        }
        for x in rows
    ]


@router.post("/holidays")
def create_holiday(
    holiday_date: date,
    name: str,
    day_type: str = "HOLIDAY",
    pay_rate: int = 3,
    db: Session = Depends(get_db),
):
    if day_type not in ("HOLIDAY", "WORKDAY_TRANSFER"):
        raise HTTPException(400, "day_type 只允许 HOLIDAY / WORKDAY_TRANSFER")

    team = _cse_team(db)

    exists = db.scalar(
        select(Holiday).where(
            Holiday.team_id == team.id,
            Holiday.holiday_date == holiday_date,
        )
    )
    if exists:
        raise HTTPException(400, "该日期已存在节假日记录")

    if int(pay_rate) not in (1, 2, 3):
        raise HTTPException(400, "工资倍数只允许 1 / 2 / 3")

    row = Holiday(
        team_id=team.id,
        holiday_date=holiday_date,
        name=name,
        day_type=day_type,
        pay_rate=int(pay_rate),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": str(row.id),
        "date": str(row.holiday_date),
        "name": row.name,
        "day_type": row.day_type,
        "pay_rate": row.pay_rate,
    }


@router.delete("/holidays/{holiday_id}")
def delete_holiday(holiday_id: str, db: Session = Depends(get_db)):
    row = db.get(Holiday, uuid.UUID(holiday_id))
    if not row:
        raise HTTPException(404, "节假日记录不存在")
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@router.post("/import/excel")
async def import_excel_api(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "只允许上传 xlsx")

    os.makedirs("/data/uploads", exist_ok=True)

    path = f"/data/uploads/{uuid.uuid4()}_{safe_filename(file.filename)}"

    with open(path, "wb") as f:
        import shutil
        shutil.copyfileobj(file.file, f)

    result = import_excel(db, path)

    return result


@router.get("/periods")
def list_periods(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(SchedulePeriod).order_by(SchedulePeriod.start_date)
    ).all()
    return [
        {
            "id": str(x.id),
            "name": x.name,
            "start_date": str(x.start_date),
            "end_date": str(x.end_date),
            "status": x.status,
        }
        for x in rows
    ]


@router.post("/periods")
def create_period(
    start_date: date,
    end_date: date,
    name: str,
    db: Session = Depends(get_db),
):
    if end_date < start_date:
        raise HTTPException(400, "end_date 不能早于 start_date")

    team = _cse_team(db)

    # 【2026-09-24】不只拦"完全相同的日期范围"，凡是**区间重叠**一律拒绝：
    # 重叠周期会让跨月连续性（取上月月末状态）与按日期范围的统计/校验同时算错。
    conflict = db.scalar(
        select(SchedulePeriod).where(
            SchedulePeriod.team_id == team.id,
            SchedulePeriod.start_date <= end_date,
            SchedulePeriod.end_date >= start_date,
        )
    )
    if conflict:
        same = conflict.start_date == start_date and conflict.end_date == end_date
        raise HTTPException(
            400,
            (
                f"该日期范围已存在周期 {conflict.name}"
                if same
                else f"该区间与现有周期 {conflict.name}"
                f"（{conflict.start_date} ~ {conflict.end_date}）重叠，请调整起止日期"
            ),
        )

    period = SchedulePeriod(
        team_id=team.id,
        name=name,
        start_date=start_date,
        end_date=end_date,
    )

    db.add(period)
    db.commit()
    db.refresh(period)

    return {
        "id": str(period.id),
        "name": period.name,
        "start_date": str(period.start_date),
        "end_date": str(period.end_date),
        "status": period.status,
    }


@router.post("/periods/{period_id}/solve")
def solve(
    period_id: str,
    dis: str = "",
    force: bool = False,
    db: Session = Depends(get_db),
):
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)

    if not period:
        raise HTTPException(404, "周期不存在")

    # 【F1 门禁】已发布周期禁止直接重排：求解会删除并重写全部 AUTO 行，
    # 发布后再重排会让"已发布版本"不可复现。必须显式 force=true（留审计）或先撤回发布。
    if period.status == "PUBLISHED" and not force:
        raise HTTPException(
            400,
            "周期已发布（PUBLISHED），禁止直接重排。请先 POST /unpublish 撤回发布，"
            "或显式传 force=true（会写入审计日志并自动保存修订快照）。",
        )

    # 【F1 快照】先整体存一份，保证任何一次重排都能回滚
    snapshot_period(db, period, reason="pre-solve" + ("-forced" if force else ""))

    if period.status == "PUBLISHED":
        db.add(
            AuditLog(
                action="SOLVE_PUBLISHED_PERIOD",
                entity_type="SchedulePeriod",
                entity_id=period.id,
                after_data={"period": period.name, "force": True, "dis": dis or None},
            )
        )

    # 【F2 修复】消融开关改为显式参数传递，不再写 os.environ
    disabled = {c.strip() for c in dis.split(",") if c.strip()}

    try:
        result = solve_period(db, pid, disabled=disabled)
    except Exception as exc:
        # 求解异常时不能把周期永远留在 SOLVING 状态
        db.rollback()
        period = db.get(SchedulePeriod, pid)
        if period:
            period.status = "ERROR"
            db.commit()
        raise HTTPException(500, f"求解失败: {exc}")

    # 【2026-09-24】节假日日历缺失提醒（不阻断求解，只提示）
    if isinstance(result, dict):
        holiday_years = {
            x.year
            for x in db.scalars(
                select(Holiday.holiday_date).where(Holiday.team_id == period.team_id)
            ).all()
        }
        missing = sorted({d.year for d in _period_dates(period)} - holiday_years)

        if missing:
            result.setdefault("warnings", []).append(
                "、".join(str(y) for y in missing)
                + " 年尚未配置法定节假日，本次排班会把法定假日按普通工作日处理；"
                "请到「节假日管理」联网同步或手工录入"
            )

    return result


@router.get("/periods/{period_id}/validate")
def validate(
    period_id: str,
    db: Session = Depends(get_db),
):
    return validate_period(db, parse_period_id(period_id))


@router.post("/periods/{period_id}/publish")
def publish(
    period_id: str,
    db: Session = Depends(get_db),
):
    """发布流程（文档 §56）：DRAFT → SOLVED → Validator PASS → PUBLISHED。

    FAIL 时拒绝发布并返回全部错误，不做任何静默放行。
    """
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    if period.status != "SOLVED":
        raise HTTPException(400, f"只有 SOLVED 状态可以发布，当前 {period.status}")

    result = validate_period(db, pid)

    if result["status"] != "PASS":
        raise HTTPException(400, {"message": "校验未通过，禁止发布", **result})

    # 【F1 快照】发布前存一份，作为"发布版本"基线（可回滚）
    snapshot_period(db, period, reason="publish")

    period.status = "PUBLISHED"
    period.published_at = datetime.utcnow()
    db.commit()

    return {"status": "PUBLISHED", "published_at": str(period.published_at)}


@router.post("/periods/{period_id}/unpublish")
def unpublish(
    period_id: str,
    db: Session = Depends(get_db),
):
    """撤回发布：PUBLISHED → SOLVED，排班数据保留，可修改/重排后再次发布。

    留审计记录（AuditLog）。
    """
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    if period.status != "PUBLISHED":
        raise HTTPException(400, f"只有 PUBLISHED 状态可以撤回，当前 {period.status}")

    period.status = "SOLVED"
    period.published_at = None

    db.add(
        AuditLog(
            action="UNPUBLISH",
            entity_type="SchedulePeriod",
            entity_id=period.id,
            after_data={"period": period.name, "status": "SOLVED"},
        )
    )

    db.commit()

    return {"status": "SOLVED", "message": "已撤回发布，可修改或重新排班后再次发布"}


@router.get("/periods/{period_id}/export")
def export(
    period_id: str,
    db: Session = Depends(get_db),
):
    path = export_period(db, parse_period_id(period_id))
    return {
        "file": path
    }


@router.get("/periods/{period_id}/export/download")
def export_download(
    period_id: str,
    db: Session = Depends(get_db),
):
    path = export_period(db, parse_period_id(period_id))
    # 【2026-09-24】补上 xlsx 的 MIME 类型（原来落到默认 text/plain，个别客户端会提示格式异常）
    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _employees_with_data(db: Session, period_id) -> list:
    """排班表 / 统计用的员工集合：全部在职员工 + **在该周期内有排班记录的已停用员工**。

    【2026-10-10】停用只表示"以后不再参与排班"，历史周期里他/她的班次必须仍能看到、能统计。
    原实现所有查询都写死 `Employee.active.is_(True)`，导致停用员工的历史排班整片消失。
    （求解器仍然只用在职员工，停用的人不会被排进新周期。）
    """
    data_ids = set(
        db.scalars(
            select(ScheduleAssignment.employee_id)
            .where(ScheduleAssignment.period_id == period_id)
            .distinct()
        ).all()
    )
    emps = db.scalars(
        select(Employee).order_by(Employee.sort_order, Employee.name)
    ).all()
    return [e for e in emps if e.active or e.id in data_ids]


@router.get("/periods/{period_id}/grid")
def grid(
    period_id: str,
    db: Session = Depends(get_db),
):
    """排班 Grid：员工 × 日期 矩阵 + 班次/节假日字典，供前端直接渲染。"""
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    dates = _period_dates(period)

    # 在职 + 该周期内有排班记录的已停用员工（历史可追溯）
    employees = _employees_with_data(db, pid)

    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    rows = db.scalars(
        select(ScheduleAssignment).where(ScheduleAssignment.period_id == pid)
    ).all()

    team = _cse_team(db)
    holiday_rows = db.scalars(
        select(Holiday).where(Holiday.team_id == team.id)
    ).all()
    holidays = {
        str(x.holiday_date): {"name": x.name, "day_type": x.day_type}
        for x in holiday_rows
    }

    # 【2026-10-26】本周期内的「指定休息/请假」（供排班表标记）
    leave_rows = db.scalars(
        select(EmployeeLeave).where(
            EmployeeLeave.status == "APPROVED",
            EmployeeLeave.start_date <= period.end_date,
            EmployeeLeave.end_date >= period.start_date,
        )
    ).all()

    leaves_map: dict = {}
    for lv in leave_rows:
        bucket = leaves_map.setdefault(str(lv.employee_id), {})
        cur = max(lv.start_date, period.start_date)
        last = min(lv.end_date, period.end_date)
        while cur <= last:
            bucket[str(cur)] = {
                "type": lv.leave_type,
                "label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
                "reason": lv.reason or "",
            }
            cur = cur + timedelta(days=1)

    cells = {}
    for row in rows:
        emp_cells = cells.setdefault(str(row.employee_id), {})
        shift = shifts.get(row.shift_id) if row.shift_id else None
        emp_cells[str(row.work_date)] = {
            "code": shift.code if shift else None,
            "status": row.status,
            "locked": row.locked,
            "source": row.source,
            "assignment_id": str(row.id),
        }

    return {
        "period": {
            "id": str(period.id),
            "name": period.name,
            "start_date": str(period.start_date),
            "end_date": str(period.end_date),
            "status": period.status,
        },
        "dates": [str(d) for d in dates],
        "employees": [
            {
                "id": str(e.id),
                "name": e.name,
                "email": e.email or "",
                "active": bool(e.active),
            }
            for e in employees
        ],
        "cells": cells,
        "holidays": holidays,
        "leaves": leaves_map,
    }


@router.get("/periods/{period_id}/stats")
def stats(
    period_id: str,
    db: Session = Depends(get_db),
):
    """工作量统计（用户确认口径 2026-09-20，折算口径 2026-09-24 修订）：

    - **工作**：工作日 + 调休上班日 + 周末的班次（除法定假日外的一切值班）
    - **节假日**：法定假日的班次（3 倍工资日）
    - **折算**：按 实付工时 ÷ 标准班次工时 计"天"——O1/O2/O3（8h）= 1 天，
      D/N/Y/C（12h）= 1.5 天（原实现按"1 班次 = 1 天"计，已修正）
    - 总工时 = 工作小时 + 节假日小时（真实工时，不折算）
    """
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    dates = _period_dates(period)
    date_set = set(dates)

    shifts = {
        x.id: x
        for x in db.scalars(select(Shift)).all()
    }

    team = _cse_team(db)
    holiday_dates = {
        x.holiday_date
        for x in db.scalars(
            select(Holiday).where(
                Holiday.team_id == team.id,
                Holiday.day_type == "HOLIDAY",
            )
        )
    }

    # 【2026-09-24 用户确认口径】D/N/Y/C 是 12h 班、O1/O2/O3 是 8h 班，
    # 「工作 / 节假」按 实付工时 ÷ 标准班次工时 折算成"天"（12 ÷ 8 = 1.5 天），
    # 不再按"1 个班次 = 1 天"计。基准值在「规则配置 → 统计口径」可改；
    # 「总h」始终是真实工时，不折算。
    standard_hours = (
        float(load_rules(db, team.id).get("standard_shift_hours") or 8) or 8.0
    )

    rows = db.scalars(
        select(ScheduleAssignment).where(ScheduleAssignment.period_id == pid)
    ).all()

    # 在职 + 该周期内有排班记录的已停用员工（否则停用后历史统计会缺人）
    employees = {e.id: e for e in _employees_with_data(db, pid)}

    acc = {}

    for row in rows:
        e = employees.get(row.employee_id)
        if not e or row.work_date not in date_set:
            continue
        a = acc.setdefault(
            e.id,
            {
                "name": e.name,
                "work_shifts": 0.0, "work_hours": 0.0,
                "holiday_shifts": 0.0, "holiday_hours": 0.0,
                "total_hours": 0.0,
                # 【2026-10-09】每人各班次：shift_days = 折算天数；shift_counts = 班次个数（1 班记 1 次）
                "shift_days": {},
                "shift_counts": {},
            },
        )
        if row.status != "WORK" or not row.shift_id:
            continue
        shift = shifts.get(row.shift_id)
        if not shift:
            continue

        paid = float(shift.paid_hours)
        days = paid / standard_hours        # 【2026-09-24】折算天数：12h 班 = 1.5 天
        a["total_hours"] += paid
        a["shift_days"][shift.code] = a["shift_days"].get(shift.code, 0.0) + days
        a["shift_counts"][shift.code] = a["shift_counts"].get(shift.code, 0) + 1

        if row.work_date in holiday_dates:
            a["holiday_shifts"] += days
            a["holiday_hours"] += paid      # 法定假日（3 倍）
        else:
            a["work_shifts"] += days
            a["work_hours"] += paid         # 工作日+调休上班日+周末

    result = [
        {
            "employee_id": str(k),
            "name": v["name"],
            "work_shifts": round(v["work_shifts"], 1),
            "work_hours": round(v["work_hours"], 1),
            "holiday_shifts": round(v["holiday_shifts"], 1),
            "holiday_hours": round(v["holiday_hours"], 1),
            "total_hours": round(v["total_hours"], 1),
            "shift_days": {c: round(x, 1) for c, x in v["shift_days"].items()},
            "shift_counts": dict(v["shift_counts"]),
        }
        for k, v in acc.items()
    ]
    result.sort(key=lambda x: x["name"])
    return result


@router.post("/periods/{period_id}/assignments/{assignment_id}/lock")
def lock_assignment(
    period_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    pid = parse_period_id(period_id)
    row = db.get(ScheduleAssignment, uuid.UUID(assignment_id))
    if not row or row.period_id != pid:
        raise HTTPException(404, "排班记录不存在")
    row.locked = True
    db.commit()
    return {"status": "locked", "assignment_id": assignment_id}


@router.post("/periods/{period_id}/assignments/{assignment_id}/unlock")
def unlock_assignment(
    period_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    pid = parse_period_id(period_id)
    row = db.get(ScheduleAssignment, uuid.UUID(assignment_id))
    if not row or row.period_id != pid:
        raise HTTPException(404, "排班记录不存在")
    row.locked = False
    db.commit()
    return {"status": "unlocked", "assignment_id": assignment_id}


@router.put("/periods/{period_id}/cells")
def set_cell(
    period_id: str,
    employee_id: str,
    work_date: date,
    shift_code: str = "OFF",
    db: Session = Depends(get_db),
):
    """人工修改（文档 §55）：不是裸 UPDATE，改完立即全规则校验，
    违反任何硬约束（权限/连续工作/连夜/三连夜休息/最低休息时间）直接拒绝回滚。
    """
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    if period.status not in ("SOLVED", "PUBLISHED"):
        raise HTTPException(400, f"周期状态 {period.status} 不允许人工修改")

    emp = db.get(Employee, uuid.UUID(employee_id))
    if not emp or not emp.active:
        raise HTTPException(404, "员工不存在")

    if not (period.start_date <= work_date <= period.end_date):
        raise HTTPException(400, "日期不在周期范围内")

    row = db.scalar(
        select(ScheduleAssignment).where(
            ScheduleAssignment.period_id == pid,
            ScheduleAssignment.employee_id == emp.id,
            ScheduleAssignment.work_date == work_date,
        )
    )

    if row and row.locked:
        raise HTTPException(400, "该格已锁定，请先解锁")

    new_shift = None
    new_status = "OFF"

    if shift_code and shift_code != "OFF":
        new_shift = db.scalar(select(Shift).where(Shift.code == shift_code))
        if not new_shift:
            raise HTTPException(400, f"非法班次代码 {shift_code}")
        new_status = "WORK"

    if row is None:
        row = ScheduleAssignment(
            period_id=pid,
            employee_id=emp.id,
            work_date=work_date,
            shift_id=new_shift.id if new_shift else None,
            status=new_status,
            source="MANUAL",
            locked=False,
        )
        db.add(row)
    else:
        row.shift_id = new_shift.id if new_shift else None
        row.status = new_status
        row.source = "MANUAL"

    db.flush()

    # 复用 Validator 全部规则，只看该员工的错误
    result = validate_period(db, pid)
    emp_errors = [
        e for e in result["errors"]
        if e.get("employee_id") == str(emp.id)
    ]

    if emp_errors:
        db.rollback()
        raise HTTPException(400, {"message": "修改违反排班规则，已回滚", "errors": emp_errors})

    db.commit()

    return {
        "status": "ok",
        "employee": emp.name,
        "date": str(work_date),
        "shift": shift_code,
    }


# ---------------------------------------------------------------------------
# 【F1】修订快照：列表与回滚（补丁 1 遗漏，补上）
# ---------------------------------------------------------------------------

@router.get("/periods/{period_id}/revisions")
def revisions(period_id: str, db: Session = Depends(get_db)):
    return list_revisions(db, parse_period_id(period_id))


@router.post("/periods/{period_id}/revisions/{revision_id}/restore")
def restore_revision(period_id: str, revision_id: str, db: Session = Depends(get_db)):
    """把周期排班回滚到某个快照（含 locked / HISTORY / MANUAL 行）。"""
    pid = parse_period_id(period_id)

    try:
        rid = uuid.UUID(revision_id)
    except ValueError:
        raise HTTPException(400, "非法 revision id")

    row = db.get(ScheduleRevision, rid)

    if not row or row.period_id != pid:
        raise HTTPException(404, "修订不存在")

    cells = restore_snapshot(db, row)

    db.add(
        AuditLog(
            action="RESTORE_REVISION",
            entity_type="SchedulePeriod",
            entity_id=pid,
            after_data={"revision_id": str(rid), "version": row.version, "cells": cells},
        )
    )
    db.commit()

    return {"status": "RESTORED", "version": row.version, "cells": cells}


# ---------------------------------------------------------------------------
# 【F3】员工级夜班策略
# ---------------------------------------------------------------------------

class NightPolicyIn(BaseModel):
    night_max_consecutive: int
    night_rest_days: int


@router.put("/employees/{employee_id}/night-policy")
def set_night_policy(
    employee_id: str,
    body: NightPolicyIn,
    db: Session = Depends(get_db),
):
    """设置某人的夜班连班上限与强制休息天数，并标记为"已人工确认"。"""
    try:
        eid = uuid.UUID(employee_id)
    except ValueError:
        raise HTTPException(400, "非法 employee id")

    emp = db.get(Employee, eid)

    if not emp:
        raise HTTPException(404, "员工不存在")

    # 【联动】没有夜班(N)权限的人不会被排夜班，不允许设置夜班策略
    if emp.id not in _night_permitted_employee_ids(db):
        raise HTTPException(
            400,
            f"{emp.name} 没有夜班(N)权限，无法设置夜班策略；"
            "请先在「班次权限」里为其开通 N 班次",
        )

    if not (1 <= body.night_max_consecutive <= 7):
        raise HTTPException(400, "连班上限需在 1~7 之间")

    if not (0 <= body.night_rest_days <= 7):
        raise HTTPException(400, "强制休息天数需在 0~7 之间")

    before = {
        "night_max_consecutive": emp.night_max_consecutive,
        "night_rest_days": emp.night_rest_days,
    }

    emp.night_max_consecutive = body.night_max_consecutive
    emp.night_rest_days = body.night_rest_days
    emp.night_policy_confirmed = True

    db.add(
        AuditLog(
            action="SET_NIGHT_POLICY",
            entity_type="Employee",
            entity_id=emp.id,
            before_data=before,
            after_data={
                "night_max_consecutive": emp.night_max_consecutive,
                "night_rest_days": emp.night_rest_days,
            },
        )
    )
    db.commit()

    return {
        "id": str(emp.id),
        "name": emp.name,
        "night_max_consecutive": emp.night_max_consecutive,
        "night_rest_days": emp.night_rest_days,
        "night_policy_confirmed": emp.night_policy_confirmed,
    }


# ---------------------------------------------------------------------------
# 【2026-09-22】可配置规则：团队级硬性条件/参数，Web 界面直接改，无需改代码
# ---------------------------------------------------------------------------

class RulesIn(BaseModel):
    rules: dict


class NightPolicyBulkIn(BaseModel):
    employee_ids: list[str] = []
    night_max_consecutive: int
    night_rest_days: int
    all_employees: bool = False


@router.get("/rules")
def get_rules(db: Session = Depends(get_db)):
    """返回规则清单（含默认值、取值范围、说明）与当前生效值。"""
    team = _cse_team(db)
    return {
        "specs": rule_specs_meta(),
        "current": load_rules(db, team.id),
    }


@router.put("/rules")
def put_rules(body: RulesIn, db: Session = Depends(get_db)):
    """保存规则（可只传要改的项）。保存后重新排班即生效。"""
    team = _cse_team(db)

    try:
        current = save_rules(db, team.id, body.rules or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    db.add(
        AuditLog(
            action="SAVE_RULES",
            entity_type="ScheduleRule",
            after_data={"rules": body.rules},
        )
    )
    db.commit()

    return {"current": current}


@router.delete("/rules/{rule_key}")
def reset_rule_api(rule_key: str, db: Session = Depends(get_db)):
    """把某项规则恢复为默认值。"""
    team = _cse_team(db)

    try:
        current = reset_rule(db, team.id, rule_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"current": current}


@router.put("/employees/night-policy/bulk")
def set_night_policy_bulk(body: NightPolicyBulkIn, db: Session = Depends(get_db)):
    """批量设置夜班策略（例如"这 5 个人可以 4 连、那 3 个人只能 3 连"）。"""
    if not (1 <= body.night_max_consecutive <= 7):
        raise HTTPException(400, "连班上限需在 1~7 之间")

    if not (0 <= body.night_rest_days <= 7):
        raise HTTPException(400, "强制休息天数需在 0~7 之间")

    if body.all_employees:
        targets = db.scalars(select(Employee).where(Employee.active.is_(True))).all()
    else:
        ids = []

        for raw in body.employee_ids:
            try:
                ids.append(uuid.UUID(raw))
            except ValueError:
                raise HTTPException(400, f"非法 employee id：{raw}")

        if not ids:
            raise HTTPException(400, "请选择员工（或勾选 all_employees）")

        targets = db.scalars(select(Employee).where(Employee.id.in_(ids))).all()

    # 【联动】只对拥有夜班(N)权限的人生效，其余跳过并回报
    permitted = _night_permitted_employee_ids(db)
    updated = [e for e in targets if e.id in permitted]
    skipped = [e for e in targets if e.id not in permitted]

    for emp in updated:
        emp.night_max_consecutive = body.night_max_consecutive
        emp.night_rest_days = body.night_rest_days
        emp.night_policy_confirmed = True

    db.add(
        AuditLog(
            action="SET_NIGHT_POLICY_BULK",
            entity_type="Employee",
            after_data={
                "count": len(targets),
                "night_max_consecutive": body.night_max_consecutive,
                "night_rest_days": body.night_rest_days,
            },
        )
    )
    db.commit()

    return {
        "updated": len(updated),
        "names": [e.name for e in updated],
        "skipped": [e.name for e in skipped],
        "skipped_reason": "没有夜班(N)权限" if skipped else "",
    }


# ============================================================================
# 【2026-09-24 新增】节假日工资倍数维护 + 两个统计接口
#   1) 每人每月、每个班次的数量          GET /stats/shift-matrix?period_id=
#   2) 每人全年节假日加班（3 倍/2 倍分开）GET /stats/holiday-overtime?year=
# ============================================================================


@router.put("/holidays/{holiday_id}")
def update_holiday(
    holiday_id: str,
    pay_rate: int | None = None,
    name: str | None = None,
    day_type: str | None = None,
    db: Session = Depends(get_db),
):
    """修改节假日（主要用途：设置工资倍数 3=法定三倍 / 2=休息日双倍 / 1=普通工作日）。"""
    try:
        hid = uuid.UUID(holiday_id)
    except ValueError:
        raise HTTPException(400, "节假日 id 非法")

    row = db.get(Holiday, hid)
    if not row:
        raise HTTPException(404, "节假日记录不存在")

    if pay_rate is not None:
        if int(pay_rate) not in (1, 2, 3):
            raise HTTPException(400, "工资倍数只允许 1 / 2 / 3")
        row.pay_rate = int(pay_rate)

    if name:
        row.name = name

    if day_type:
        if day_type not in ("HOLIDAY", "WORKDAY_TRANSFER"):
            raise HTTPException(400, "day_type 只允许 HOLIDAY / WORKDAY_TRANSFER")
        row.day_type = day_type

    db.commit()
    db.refresh(row)
    return {
        "id": str(row.id),
        "date": str(row.holiday_date),
        "name": row.name,
        "day_type": row.day_type,
        "pay_rate": row.pay_rate,
    }


class HolidayRateBulkIn(BaseModel):
    dates: list[str]
    pay_rate: int


@router.put("/holidays/pay-rate/bulk")
def bulk_set_holiday_pay_rate(body: HolidayRateBulkIn, db: Session = Depends(get_db)):
    """按日期批量设置工资倍数（页面上可把整段假期一次设好）。"""
    if int(body.pay_rate) not in (1, 2, 3):
        raise HTTPException(400, "工资倍数只允许 1 / 2 / 3")

    team = _cse_team(db)
    updated = []

    for ds in body.dates:
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        row = db.scalar(
            select(Holiday).where(
                Holiday.team_id == team.id,
                Holiday.holiday_date == d,
            )
        )
        if row:
            row.pay_rate = int(body.pay_rate)
            updated.append(ds)

    db.commit()
    return {"updated": len(updated), "dates": updated, "pay_rate": int(body.pay_rate)}


# 统计页班次列的显示顺序（用户指定 2026-09-24）：O1 → O2 → O3 → D → N → C → Y
SHIFT_DISPLAY_ORDER = ["O1", "O2", "O3", "D", "N", "C", "Y"]


def _sort_shift_codes(codes) -> list[str]:
    """按业务顺序排列班次代码；未列出的（新增班次）排在最后并按字母序。"""
    known = [c for c in SHIFT_DISPLAY_ORDER if c in codes]
    rest = sorted(c for c in codes if c not in SHIFT_DISPLAY_ORDER)
    return known + rest


@router.get("/stats/shift-matrix")
def shift_matrix(period_id: str, db: Session = Depends(get_db)):
    """每人每月、每个班次的数量（附折算天数与工时）。"""
    pid = parse_period_id(period_id)
    period = db.get(SchedulePeriod, pid)
    if not period:
        raise HTTPException(404, "周期不存在")

    dates = set(_period_dates(period))
    shifts = {x.id: x for x in db.scalars(select(Shift)).all()}
    codes = _sort_shift_codes({s.code for s in shifts.values()})
    team = _cse_team(db)

    holiday_dates = {
        x.holiday_date
        for x in db.scalars(
            select(Holiday).where(
                Holiday.team_id == team.id,
                Holiday.day_type == "HOLIDAY",
            )
        )
    }

    standard_hours = (
        float(load_rules(db, team.id).get("standard_shift_hours") or 8) or 8.0
    )

    acc = {}

    for e in _employees_with_data(db, pid):
        acc[e.id] = {
            "employee_id": str(e.id),
            "name": e.name,
            "active": bool(e.active),
            "counts": {c: 0 for c in codes},
            "total_shifts": 0,
            "work_days": 0.0,
            "holiday_days": 0.0,
            "total_hours": 0.0,
        }

    for row in db.scalars(
        select(ScheduleAssignment).where(ScheduleAssignment.period_id == pid)
    ).all():
        a = acc.get(row.employee_id)

        if not a or row.work_date not in dates:
            continue
        if row.status != "WORK" or not row.shift_id:
            continue

        s = shifts.get(row.shift_id)

        if not s:
            continue

        paid = float(s.paid_hours)
        a["counts"][s.code] = a["counts"].get(s.code, 0) + 1
        a["total_shifts"] += 1
        a["total_hours"] += paid

        if row.work_date in holiday_dates:
            a["holiday_days"] += paid / standard_hours
        else:
            a["work_days"] += paid / standard_hours

    rows = []

    for a in acc.values():
        a["work_days"] = round(a["work_days"], 1)
        a["holiday_days"] = round(a["holiday_days"], 1)
        a["total_hours"] = round(a["total_hours"], 1)
        rows.append(a)

    # 【2026-09-24】不再按姓名重排：保持与排班表/员工页一致的 sort_order 顺序
    return {
        "period": {
            "id": str(period.id),
            "name": period.name,
            "start_date": str(period.start_date),
            "end_date": str(period.end_date),
        },
        "codes": codes,
        "standard_hours": standard_hours,
        "rows": rows,
    }


@router.get("/stats/holiday-overtime")
def holiday_overtime(year: int, db: Session = Depends(get_db)):
    """每人全年节假日加班班次：三倍工资日 / 双倍工资日分开统计。"""
    team = _cse_team(db)
    start = date(year, 1, 1)
    end = date(year, 12, 31)

    holiday_rows = db.scalars(
        select(Holiday)
        .where(
            Holiday.team_id == team.id,
            Holiday.day_type == "HOLIDAY",
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
        )
        .order_by(Holiday.holiday_date)
    ).all()

    rate_by_date = {h.holiday_date: int(h.pay_rate or 3) for h in holiday_rows}

    shifts = {x.id: x for x in db.scalars(select(Shift)).all()}
    codes = _sort_shift_codes({s.code for s in shifts.values()})
    standard_hours = (
        float(load_rules(db, team.id).get("standard_shift_hours") or 8) or 8.0
    )

    acc = {}

    # 【2026-10-10】年度报表要包含"当年有节假日加班但已停用"的人（涉及加班费结算）
    ot_data_ids = set(
        db.scalars(
            select(ScheduleAssignment.employee_id)
            .where(
                ScheduleAssignment.work_date >= start,
                ScheduleAssignment.work_date <= end,
            )
            .distinct()
        ).all()
    )

    for e in db.scalars(
        select(Employee).order_by(Employee.sort_order, Employee.name)
    ).all():
        if not e.active and e.id not in ot_data_ids:
            continue
        acc[e.id] = {
            "employee_id": str(e.id),
            "name": e.name,
            "active": bool(e.active),
            "triple": {"codes": {c: 0 for c in codes}, "shifts": 0, "days": 0.0, "hours": 0.0, "weighted": 0},
            "double": {"codes": {c: 0 for c in codes}, "shifts": 0, "days": 0.0, "hours": 0.0, "weighted": 0},
        }

    for row in db.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.work_date >= start,
            ScheduleAssignment.work_date <= end,
            ScheduleAssignment.status == "WORK",
        )
    ).all():
        a = acc.get(row.employee_id)

        if not a or not row.shift_id:
            continue

        rate = rate_by_date.get(row.work_date)

        if rate is None or rate < 2:
            continue

        s = shifts.get(row.shift_id)

        if not s:
            continue

        bucket = a["triple"] if rate >= 3 else a["double"]
        paid = float(s.paid_hours)
        bucket["codes"][s.code] = bucket["codes"].get(s.code, 0) + 1
        bucket["shifts"] += 1
        bucket["hours"] += paid
        bucket["days"] += paid / standard_hours
        # 【2026-09-24 用户口径】倍数总和 = Σ 班次 × 当日工资倍数（三倍日记 3、双倍日记 2）
        bucket["weighted"] += rate

    rows = []

    for a in acc.values():
        for key in ("triple", "double"):
            a[key]["days"] = round(a[key]["days"], 1)
            a[key]["hours"] = round(a[key]["hours"], 1)
        a["holiday_shifts"] = a["triple"]["shifts"] + a["double"]["shifts"]
        a["holiday_days"] = round(a["triple"]["days"] + a["double"]["days"], 1)
        a["holiday_hours"] = round(a["triple"]["hours"] + a["double"]["hours"], 1)
        # 【2026-09-24 用户口径】合计 = 三倍天数×3 + 双倍天数×2
        a["weighted_triple"] = a["triple"]["weighted"]
        a["weighted_double"] = a["double"]["weighted"]
        a["weighted_total"] = a["weighted_triple"] + a["weighted_double"]
        a["triple_ratio"] = (
            round(a["weighted_triple"] / a["weighted_total"] * 100, 1)
            if a["weighted_total"]
            else 0.0
        )
        rows.append(a)

    return {
        "year": year,
        "codes": codes,
        "standard_hours": standard_hours,
        "holiday_days": [
            {
                "date": str(h.holiday_date),
                "name": h.name,
                "pay_rate": rate_by_date[h.holiday_date],
            }
            for h in holiday_rows
        ],
        "rows": rows,
    }


# ============================================================================
# 【2026-09-24 新增】节假日维护：联网同步（万年历）+ 批量录入 + 按国家规定套倍数
#   数据源优先级：timor.tech（带 wage，权威倍数）→ holiday-cn(GitHub) → jsdelivr 镜像
#   注：中国法定节假日与调休安排由国务院每年 11-12 月公告次年安排，
#       未公布时同步会明确返回"尚未公布"，不会写入任何数据。
# ============================================================================

# 各法定假日的"三倍工资天数"（按假期名称；春节按第 2 天起=除夕 起算）
STATUTORY_TRIPLE_DAYS: dict[str, int] = {
    "元旦": 1,
    "春节": 4,
    "清明节": 1,
    "劳动节": 2,
    "端午节": 1,
    "中秋节": 1,
    "国庆节": 3,
}

STATUTORY_TRIPLE_OFFSET: dict[str, int] = {
    "春节": 1,  # 春节放假日从"除夕前一天"开始，三倍从除夕起
}

HOLIDAY_SOURCES: list[tuple[str, str]] = [
    ("timor.tech", "https://timor.tech/api/holiday/year/{year}"),
    ("holiday-cn(github)", "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"),
    ("holiday-cn(jsdelivr)", "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"),
]


def _http_get_json(url: str, timeout: int = 15):
    import json as _json
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "cse-scheduler/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _parse_timor(payload: dict) -> list[dict]:
    """timor.tech：holiday=true 放假（wage 2/3），false 为补班（1 倍）。"""
    out = []

    for v in (payload.get("holiday") or {}).values():
        d = v.get("date")
        if not d:
            continue
        off = bool(v.get("holiday"))
        wage = int(v.get("wage") or (2 if off else 1))
        out.append(
            {
                "date": str(d),
                "name": str(v.get("name") or ""),
                "day_type": "HOLIDAY" if off else "WORKDAY_TRANSFER",
                "pay_rate": wage if off else 1,
            }
        )

    return out


def _parse_holiday_cn(payload: dict) -> list[dict]:
    """holiday-cn：days[{date,name,isOffDay}]；倍数按国家规定天数推算。"""
    days = payload.get("days") or []
    groups: dict[str, list[str]] = {}

    for x in days:
        if x.get("isOffDay"):
            groups.setdefault(str(x.get("name") or "").strip(), []).append(str(x.get("date")))

    triple: set[str] = set()

    for name, dates in groups.items():
        hit = None
        for key, n in STATUTORY_TRIPLE_DAYS.items():
            if key == name or key in name or name in key:
                hit = (key, n)
                break
        if not hit:
            continue
        key, n = hit
        offset = STATUTORY_TRIPLE_OFFSET.get(key, 0)
        for d in sorted(dates)[offset:offset + n]:
            triple.add(d)

    out = []

    for x in days:
        d = str(x.get("date"))
        off = bool(x.get("isOffDay"))
        out.append(
            {
                "date": d,
                "name": str(x.get("name") or ""),
                "day_type": "HOLIDAY" if off else "WORKDAY_TRANSFER",
                "pay_rate": (3 if d in triple else 2) if off else 1,
            }
        )

    return out


def _sync_holidays(db: Session, year: int, overwrite: bool, dry_run: bool) -> dict:
    team = _cse_team(db)
    fetched: list[dict] | None = None
    source: str | None = None
    notes: list[str] = []

    for name, tmpl in HOLIDAY_SOURCES:
        url = tmpl.format(year=year)
        try:
            payload = _http_get_json(url)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name} 访问失败：{type(exc).__name__}")
            continue

        rows = _parse_timor(payload) if name.startswith("timor") else _parse_holiday_cn(payload)
        rows = [r for r in rows if r["date"][:4] == str(year)]

        if rows:
            fetched, source = rows, name
            break

        notes.append(f"{name} 返回 0 条（该年份可能尚未公布）")

    if not fetched:
        return {
            "ok": False,
            "year": year,
            "source": None,
            "dry_run": dry_run,
            "message": f"{year} 年节假日安排尚未公布（国务院通常每年 11-12 月发布次年安排），已尝试所有数据源",
            "sources": notes,
            "added": 0,
            "updated": 0,
            "unchanged": 0,
            "items": [],
        }

    existing = {
        x.holiday_date: x
        for x in db.scalars(select(Holiday).where(Holiday.team_id == team.id)).all()
    }

    added: list[dict] = []
    updated: list[dict] = []
    unchanged: list[dict] = []

    for r in sorted(fetched, key=lambda x: x["date"]):
        try:
            d = date.fromisoformat(r["date"])
        except ValueError:
            continue

        cur = existing.get(d)
        item = {
            "date": r["date"],
            "name": r["name"],
            "day_type": r["day_type"],
            "pay_rate": int(r["pay_rate"]),
        }

        if cur is None:
            added.append(item)
            if not dry_run:
                db.add(
                    Holiday(
                        team_id=team.id,
                        holiday_date=d,
                        name=r["name"],
                        day_type=r["day_type"],
                        pay_rate=int(r["pay_rate"]),
                    )
                )
        elif cur.day_type == r["day_type"] and int(cur.pay_rate or 3) == int(r["pay_rate"]):
            unchanged.append(item)
        else:
            updated.append(
                {
                    **item,
                    "before": {
                        "name": cur.name,
                        "day_type": cur.day_type,
                        "pay_rate": int(cur.pay_rate or 3),
                    },
                }
            )
            if not dry_run and overwrite:
                cur.name = r["name"]
                cur.day_type = r["day_type"]
                cur.pay_rate = int(r["pay_rate"])

    if not dry_run:
        db.add(
            AuditLog(
                action="SYNC_HOLIDAYS",
                entity_type="Holiday",
                after_data={
                    "year": year,
                    "source": source,
                    "added": len(added),
                    "updated": len(updated) if overwrite else 0,
                    "unchanged": len(unchanged),
                },
            )
        )
        db.commit()

    return {
        "ok": True,
        "year": year,
        "source": source,
        "dry_run": dry_run,
        "overwrite": overwrite,
        "added": len(added),
        "updated": len(updated),
        "unchanged": len(unchanged),
        "holiday_days": sum(1 for r in fetched if r["day_type"] == "HOLIDAY"),
        "triple_days": sum(
            1 for r in fetched if r["day_type"] == "HOLIDAY" and int(r["pay_rate"]) == 3
        ),
        "double_days": sum(
            1 for r in fetched if r["day_type"] == "HOLIDAY" and int(r["pay_rate"]) == 2
        ),
        "transfer_days": sum(1 for r in fetched if r["day_type"] == "WORKDAY_TRANSFER"),
        "added_items": added,
        "updated_items": updated,
        "sources": notes,
    }


@router.get("/holidays/sync/preview")
def holidays_sync_preview(year: int, db: Session = Depends(get_db)):
    """联网预览：不写库，只返回会新增/更新哪些日期。"""
    return _sync_holidays(db, year, overwrite=False, dry_run=True)


@router.post("/holidays/sync")
def holidays_sync(year: int, overwrite: bool = False, db: Session = Depends(get_db)):
    """联网同步某年法定节假日（overwrite=true 时覆盖已存在日期的类型与倍数）。"""
    return _sync_holidays(db, year, overwrite=overwrite, dry_run=False)


class HolidayBulkIn(BaseModel):
    start_date: str
    end_date: str
    name: str
    day_type: str = "HOLIDAY"
    pay_rate: int = 3


@router.post("/holidays/bulk-add")
def bulk_add_holidays(body: HolidayBulkIn, db: Session = Depends(get_db)):
    """按区间批量新增（录一个假期一次搞定）。已存在的日期跳过。"""
    if body.day_type not in ("HOLIDAY", "WORKDAY_TRANSFER"):
        raise HTTPException(400, "day_type 只允许 HOLIDAY / WORKDAY_TRANSFER")
    if int(body.pay_rate) not in (1, 2, 3):
        raise HTTPException(400, "工资倍数只允许 1 / 2 / 3")

    try:
        d0 = date.fromisoformat(body.start_date)
        d1 = date.fromisoformat(body.end_date)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")

    if d1 < d0:
        raise HTTPException(400, "结束日期不能早于开始日期")
    if (d1 - d0).days > 40:
        raise HTTPException(400, "一次最多录入 40 天")

    team = _cse_team(db)
    existing = {
        x.holiday_date
        for x in db.scalars(select(Holiday).where(Holiday.team_id == team.id)).all()
    }
    added = []
    d = d0

    while d <= d1:
        if d not in existing:
            db.add(
                Holiday(
                    team_id=team.id,
                    holiday_date=d,
                    name=body.name,
                    day_type=body.day_type,
                    pay_rate=int(body.pay_rate),
                )
            )
            added.append(str(d))
        d += timedelta(days=1)

    db.commit()
    return {"added": len(added), "dates": added, "name": body.name, "pay_rate": int(body.pay_rate)}


@router.post("/holidays/pay-rate/auto")
def auto_holiday_pay_rate(year: int, db: Session = Depends(get_db)):
    """按国家规定套用倍数：按假期名称分组，前 N 天 3 倍（春节从第 2 天=除夕 起），其余 2 倍。"""
    team = _cse_team(db)
    rows = db.scalars(
        select(Holiday)
        .where(
            Holiday.team_id == team.id,
            Holiday.day_type == "HOLIDAY",
            Holiday.holiday_date >= date(year, 1, 1),
            Holiday.holiday_date <= date(year, 12, 31),
        )
        .order_by(Holiday.holiday_date)
    ).all()

    groups: dict[str, list] = {}

    for r in rows:
        key = r.name or ""
        for k in STATUTORY_TRIPLE_DAYS:
            if k in key or key in k:
                key = k
                break
        groups.setdefault(key, []).append(r)

    changed = []

    for key, items in groups.items():
        n = STATUTORY_TRIPLE_DAYS.get(key)
        offset = STATUTORY_TRIPLE_OFFSET.get(key, 0)
        triple_ids = set()

        if n:
            for r in sorted(items, key=lambda x: x.holiday_date)[offset:offset + n]:
                triple_ids.add(r.id)

        for r in items:
            want = 3 if r.id in triple_ids else 2
            if int(r.pay_rate or 3) != want:
                r.pay_rate = want
                changed.append({"date": str(r.holiday_date), "name": r.name, "pay_rate": want})

    db.commit()
    return {
        "year": year,
        "groups": {k: len(v) for k, v in groups.items()},
        "changed": len(changed),
        "items": changed,
        "note": "春节按第 2~5 天（除夕起 4 天）计三倍；如与国务院公告不符请手动调整",
    }


# ============================================================================
# 【2026-09-24 新增】自动生成排班周期（26→25 模式）
# ============================================================================


def _period_range(year: int, month: int) -> tuple[date, date]:
    """周期 "YYYY-MM" 覆盖 上月26日 ~ 本月25日（与现有周期命名一致）。"""
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    return date(prev_year, prev_month, 26), date(year, month, 25)


def _auto_generate_periods(
    db: Session,
    year: int,
    sync_holidays: bool,
    dry_run: bool,
) -> dict:
    team = _cse_team(db)
    existing = db.scalars(
        select(SchedulePeriod).where(SchedulePeriod.team_id == team.id)
    ).all()

    planned: list[dict] = []
    skipped: list[dict] = []

    for month in range(1, 13):
        name = f"{year}-{month:02d}"
        start, end = _period_range(year, month)

        clash = [
            p
            for p in existing
            if p.name == name or not (p.end_date < start or p.start_date > end)
        ]

        if clash:
            p = clash[0]
            why = "同名周期已存在" if p.name == name else "与现有周期日期重叠"
            skipped.append(
                {
                    "name": name,
                    "start_date": str(start),
                    "end_date": str(end),
                    "reason": f"{why}：{p.name}（{p.start_date} ~ {p.end_date}）",
                }
            )
            continue

        planned.append(
            {
                "name": name,
                "start_date": str(start),
                "end_date": str(end),
                "days": (end - start).days + 1,
            }
        )

        if not dry_run:
            db.add(
                SchedulePeriod(
                    team_id=team.id,
                    name=name,
                    start_date=start,
                    end_date=end,
                )
            )

    holiday_results: list[dict] = []

    if sync_holidays and not dry_run:
        try:
            r = _sync_holidays(db, year, overwrite=False, dry_run=False)
            holiday_results.append(
                {
                    "year": year,
                    "ok": r["ok"],
                    "source": r.get("source"),
                    "added": r.get("added", 0),
                    "unchanged": r.get("unchanged", 0),
                    "message": r.get("message", ""),
                }
            )
        except Exception as exc:  # noqa: BLE001
            holiday_results.append(
                {"year": year, "ok": False, "message": f"同步失败：{type(exc).__name__}"}
            )
    elif sync_holidays and dry_run:
        holiday_results.append(
            {"year": year, "ok": None, "message": "预览模式：未执行节假日同步"}
        )

    if not dry_run:
        db.add(
            AuditLog(
                action="AUTO_GENERATE_PERIODS",
                entity_type="SchedulePeriod",
                after_data={
                    "year": year,
                    "created": [p["name"] for p in planned],
                    "skipped": len(skipped),
                },
            )
        )
        db.commit()

    return {
        "ok": True,
        "year": year,
        "dry_run": dry_run,
        "created": len(planned),
        "skipped": len(skipped),
        "periods": planned,
        "skipped_items": skipped,
        "holidays": holiday_results,
    }


@router.post("/periods/auto-generate")
def auto_generate_periods(
    year: int,
    sync_holidays: bool = True,
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """按 26→25 模式自动生成某年 12 个排班周期；已存在的自动跳过，可选同时同步该年节假日。"""
    if year < 2000 or year > 2100:
        raise HTTPException(400, "年份超出合理范围")
    return _auto_generate_periods(db, year, sync_holidays, dry_run)


# ============================================================================
# 【2026-09-24 新增】Dashboard 概览：待办与异常 / 最新周期 / 年度加班 / 最近操作
# ============================================================================

AUDIT_ACTION_LABELS: dict[str, str] = {
    "UNPUBLISH": "撤回发布",
    "SOLVE_PUBLISHED_PERIOD": "重排已发布周期",
    "SAVE_RULES": "修改规则",
    "SET_NIGHT_POLICY": "设置夜班策略",
    "SET_NIGHT_POLICY_BULK": "批量设置夜班策略",
    "SYNC_HOLIDAYS": "同步节假日",
    "AUTO_GENERATE_PERIODS": "自动生成周期",
    "CREATE_USER": "新增用户",
    "DEACTIVATE_USER": "停用用户",
    "RESET_PASSWORD": "重置密码",
    "SOLVE_INFEASIBLE": "排班无解",
}


def _audit_detail(data) -> str:
    """把审计日志的 after_data 压成一行可读文本。"""
    if not isinstance(data, dict):
        return ""

    if "period" in data:
        return str(data["period"])

    if "username" in data:
        role = data.get("role")
        return f"{data['username']}" + (f"（{role}）" if role else "")

    if "year" in data and "source" in data:
        return f"{data['year']} 年 · {data['source']}（新增 {data.get('added', 0)}）"

    if "year" in data and "created" in data:
        created = data.get("created") or []
        head = "、".join(created[:3])
        return f"{data['year']} 年 · 新建 {len(created)} 个：{head}" + ("…" if len(created) > 3 else "")

    if "rules" in data:
        return "、".join(f"{k}={v}" for k, v in list(data["rules"].items())[:4])

    if "night_max_consecutive" in data or "night_rest_days" in data:
        return f"连班上限 {data.get('night_max_consecutive')} / 强制休息 {data.get('night_rest_days')}"

    if "count" in data:
        return f"{data['count']} 人"

    if "name" in data:
        return str(data["name"])

    return ""


@router.get("/dashboard/overview")
def dashboard_overview(db: Session = Depends(get_db)):
    """仪表盘概览：待办与异常提醒、最新周期指标、年度节假日加班、最近操作记录。"""
    team = _cse_team(db)

    periods = db.scalars(
        select(SchedulePeriod)
        .where(SchedulePeriod.team_id == team.id)
        .order_by(SchedulePeriod.start_date)
    ).all()
    live = [p for p in periods if p.status != "HISTORY"]

    todos: list[dict] = []

    # ① 已排好待发布
    for p in live:
        if p.status == "SOLVED":
            todos.append(
                {
                    "level": "warning",
                    "title": f"{p.name} 已排好、待发布",
                    "detail": "校验通过后点「发布」，未发布时他人看到的仍是旧版本",
                    "action": "schedule",
                    "period_id": str(p.id),
                    "period": p.name,
                }
            )

    # ② 状态异常
    for p in live:
        bad = p.status in ("INFEASIBLE", "ERROR") or (
            p.dependency_status and p.dependency_status != "VALID"
        )
        if bad:
            todos.append(
                {
                    "level": "error",
                    "title": f"{p.name} 状态异常（{p.status}／{p.dependency_status or '-'}）",
                    "detail": "请到排班页重新排班，或检查规则/权限是否冲突",
                    "action": "schedule",
                    "period_id": str(p.id),
                    "period": p.name,
                }
            )

    # ③ 节假日未配置的年份
    holiday_years = {
        x.year
        for x in db.scalars(
            select(Holiday.holiday_date).where(Holiday.team_id == team.id)
        ).all()
    }
    need_years: set[int] = set()

    for p in live:
        y, m = p.start_date.year, p.start_date.month
        while (y, m) <= (p.end_date.year, p.end_date.month):
            need_years.add(y)
            m += 1
            if m == 13:
                y, m = y + 1, 1

    missing_years = sorted(need_years - holiday_years)

    if missing_years:
        todos.append(
            {
                "level": "error",
                "title": "、".join(str(y) for y in missing_years) + " 年尚未配置法定节假日",
                "detail": "排班会把法定假日按普通工作日处理，节假日加班统计也会为 0",
                "action": "holidays",
            }
        )

    # ④ 下一周期缺口
    if periods:
        last = max(periods, key=lambda x: x.end_date)
        nxt_start = last.end_date + timedelta(days=1)

        if not any(p.start_date == nxt_start for p in periods):
            y2, m2 = nxt_start.year, nxt_start.month + 1
            if m2 == 13:
                y2, m2 = y2 + 1, 1
            todos.append(
                {
                    "level": "info",
                    "title": f"下一周期尚未创建（{nxt_start} ~ {date(y2, m2, 25)}）",
                    "detail": "用下方「排班周期 → 自动生成周期」可一次生成整年",
                    "action": "periods",
                }
            )

    # ⑤ 夜班策略未确认
    # 【2026-09-24 修正】只统计"有夜班权限"的员工：
    # 「员工夜班策略」表只列有 N 权限的人，没权限的人在该表里根本不出现、永远确认不了，
    # 之前按"全部在职"统计会一直误报（用户已确认全部有权限者，仍显示 7 人未确认）。
    active_emps = db.scalars(select(Employee).where(Employee.active.is_(True))).all()
    night_shift_ids = {
        x.id for x in db.scalars(select(Shift).where(Shift.is_night.is_(True))).all()
    }
    night_capable_ids: set = set()

    if night_shift_ids:
        for perm in db.scalars(
            select(EmployeeShiftPermission).where(
                EmployeeShiftPermission.allowed.is_(True),
                EmployeeShiftPermission.shift_id.in_(night_shift_ids),
            )
        ):
            night_capable_ids.add(perm.employee_id)

    unconfirmed = [
        e.name
        for e in active_emps
        if e.id in night_capable_ids and not e.night_policy_confirmed
    ]

    if unconfirmed:
        head = "、".join(unconfirmed[:6]) + ("…" if len(unconfirmed) > 6 else "")
        todos.append(
            {
                "level": "warning",
                "title": f"{len(unconfirmed)} 人有夜班权限但策略未确认",
                "detail": f"未确认者按全局默认值执行：{head}",
                "action": "rules",
            }
        )

    # ---------------- 最新周期指标 ----------------
    latest = None

    for p in reversed(live):
        if p.status == "PUBLISHED":
            latest = p
            break

    if latest is None and live:
        latest = live[-1]

    latest_info = None

    if latest:
        shifts = {x.id: x for x in db.scalars(select(Shift)).all()}
        hol_dates = {
            x.holiday_date
            for x in db.scalars(
                select(Holiday).where(
                    Holiday.team_id == team.id, Holiday.day_type == "HOLIDAY"
                )
            )
        }
        dates = _period_dates(latest)
        date_set = set(dates)

        rows = db.scalars(
            select(ScheduleAssignment).where(ScheduleAssignment.period_id == latest.id)
        ).all()

        work = [
            r
            for r in rows
            if r.status == "WORK" and r.shift_id in shifts and r.work_date in date_set
        ]

        per_emp: dict = {}
        on_duty: dict = {}
        total_hours = 0.0

        for r in work:
            per_emp[r.employee_id] = per_emp.get(r.employee_id, 0) + 1
            on_duty[r.work_date] = on_duty.get(r.work_date, 0) + 1
            total_hours += float(shifts[r.shift_id].paid_hours)

        workdays = [d for d in dates if d.weekday() < 5 and d not in hol_dates]
        wd_counts = [on_duty.get(d, 0) for d in workdays]

        latest_info = {
            "id": str(latest.id),
            "name": latest.name,
            "status": latest.status,
            "start_date": str(latest.start_date),
            "end_date": str(latest.end_date),
            "total_shifts": len(work),
            "night_shifts": sum(1 for r in work if shifts[r.shift_id].is_night),
            "holiday_shifts": sum(1 for r in work if r.work_date in hol_dates),
            "weekend_shifts": sum(1 for r in work if r.work_date.weekday() >= 5),
            "total_hours": round(total_hours, 1),
            "workday_avg": round(sum(wd_counts) / len(wd_counts), 1) if wd_counts else 0,
            "workday_min": min(wd_counts) if wd_counts else 0,
            "shifts_min": min(per_emp.values()) if per_emp else 0,
            "shifts_max": max(per_emp.values()) if per_emp else 0,
            "employees_count": len(per_emp),
            "manual_cells": sum(1 for r in rows if r.source == "MANUAL"),
            "locked_cells": sum(1 for r in rows if r.locked),
        }

    # ---------------- 年度节假日加班 ----------------
    year = date.today().year
    ot = holiday_overtime(year, db)
    triple_w = sum(r["triple"]["weighted"] for r in ot["rows"])
    double_w = sum(r["double"]["weighted"] for r in ot["rows"])
    total_w = triple_w + double_w
    top = sorted(ot["rows"], key=lambda x: -x["weighted_total"])[:3]

    holiday_info = {
        "year": year,
        "triple_weighted": triple_w,
        "double_weighted": double_w,
        "weighted_total": total_w,
        "triple_ratio": round(triple_w / total_w * 100, 1) if total_w else 0.0,
        "people": sum(1 for r in ot["rows"] if r["holiday_shifts"]),
        "triple_days": sum(1 for x in ot["holiday_days"] if x["pay_rate"] == 3),
        "double_days": sum(1 for x in ot["holiday_days"] if x["pay_rate"] == 2),
        "top": [
            {
                "name": r["name"],
                "weighted_total": r["weighted_total"],
                "shifts": r["holiday_shifts"],
            }
            for r in top
            if r["holiday_shifts"] > 0
        ],
    }

    # ---------------- 最近操作 ----------------
    logs = db.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8)
    ).all()

    recent = [
        {
            "time": x.created_at.strftime("%m-%d %H:%M"),
            "action": x.action,
            "label": AUDIT_ACTION_LABELS.get(x.action, x.action),
            "detail": _audit_detail(x.after_data),
        }
        for x in logs
    ]

    return {
        "todos": todos,
        "latest_period": latest_info,
        "holiday_overtime": holiday_info,
        "recent_actions": recent,
    }


# ============================================================================
# 【2026-10-26 新增】指定休息 / 请假管理
#   个别人员需要在特定某几天休息 → 求解时强制这些天休息（OFF），并参与校验。
# ============================================================================

LEAVE_TYPE_LABELS: dict[str, str] = {
    "REST": "指定休息",
    "ANNUAL": "年假",
    "SICK": "病假",
    "PERSONAL": "事假",
    "OTHER": "其他",
}


class LeaveIn(BaseModel):
    employee_ids: list[str]
    start_date: str
    end_date: str
    leave_type: str = "REST"
    reason: str | None = None


@router.get("/leaves")
def list_leaves(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
):
    """指定休息 / 请假列表（日期区间有交集即返回）。"""
    team = _cse_team(db)

    q = (
        select(EmployeeLeave, Employee)
        .join(Employee, Employee.id == EmployeeLeave.employee_id)
        .where(Employee.team_id == team.id)
        .order_by(EmployeeLeave.start_date, Employee.sort_order, Employee.name)
    )

    if from_date:
        try:
            q = q.where(EmployeeLeave.end_date >= date.fromisoformat(from_date))
        except ValueError:
            raise HTTPException(400, "from_date 格式应为 YYYY-MM-DD")
    if to_date:
        try:
            q = q.where(EmployeeLeave.start_date <= date.fromisoformat(to_date))
        except ValueError:
            raise HTTPException(400, "to_date 格式应为 YYYY-MM-DD")

    return [
        {
            "id": str(lv.id),
            "employee_id": str(emp.id),
            "employee": emp.name,
            "active": bool(emp.active),
            "start_date": str(lv.start_date),
            "end_date": str(lv.end_date),
            "days": (lv.end_date - lv.start_date).days + 1,
            "leave_type": lv.leave_type,
            "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
            "status": lv.status,
            "reason": lv.reason or "",
        }
        for lv, emp in db.execute(q).all()
    ]


@router.post("/leaves")
def create_leave(body: LeaveIn, db: Session = Depends(get_db)):
    """给一名或多名员工指定休息日期（求解时这些天强制休息）。"""
    if body.leave_type not in LEAVE_TYPE_LABELS:
        raise HTTPException(400, "leave_type 只允许：" + " / ".join(LEAVE_TYPE_LABELS))

    try:
        d0 = date.fromisoformat(body.start_date)
        d1 = date.fromisoformat(body.end_date)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")

    if d1 < d0:
        raise HTTPException(400, "结束日期不能早于开始日期")
    if (d1 - d0).days > 90:
        raise HTTPException(400, "单次最多指定 90 天")

    team = _cse_team(db)
    created: list[str] = []

    for eid in body.employee_ids:
        try:
            emp = db.get(Employee, uuid.UUID(eid))
        except ValueError:
            continue
        if not emp or emp.team_id != team.id:
            continue
        db.add(
            EmployeeLeave(
                employee_id=emp.id,
                start_date=d0,
                end_date=d1,
                leave_type=body.leave_type,
                status="APPROVED",
                reason=(body.reason or None),
            )
        )
        created.append(emp.name)

    if not created:
        raise HTTPException(400, "没有有效的员工")

    db.add(
        AuditLog(
            action="CREATE_LEAVE",
            entity_type="EmployeeLeave",
            after_data={
                "names": created,
                "start": str(d0),
                "end": str(d1),
                "type": body.leave_type,
            },
        )
    )
    db.commit()
    return {
        "created": len(created),
        "names": created,
        "start_date": str(d0),
        "end_date": str(d1),
        "leave_type": body.leave_type,
    }


@router.put("/leaves/{leave_id}")
def update_leave(
    leave_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    leave_type: str | None = None,
    reason: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        lid = uuid.UUID(leave_id)
    except ValueError:
        raise HTTPException(400, "id 非法")

    row = db.get(EmployeeLeave, lid)
    if not row:
        raise HTTPException(404, "记录不存在")

    if start_date:
        row.start_date = date.fromisoformat(start_date)
    if end_date:
        row.end_date = date.fromisoformat(end_date)
    if leave_type:
        if leave_type not in LEAVE_TYPE_LABELS:
            raise HTTPException(400, "类型不合法")
        row.leave_type = leave_type
    if reason is not None:
        row.reason = reason or None
    if row.end_date < row.start_date:
        raise HTTPException(400, "结束日期不能早于开始日期")

    db.add(
        AuditLog(
            action="UPDATE_LEAVE",
            entity_type="EmployeeLeave",
            entity_id=row.id,
            after_data={
                "start": str(row.start_date),
                "end": str(row.end_date),
                "type": row.leave_type,
            },
        )
    )
    db.commit()
    return {
        "id": str(row.id),
        "start_date": str(row.start_date),
        "end_date": str(row.end_date),
        "leave_type": row.leave_type,
    }


@router.delete("/leaves/{leave_id}")
def delete_leave(leave_id: str, db: Session = Depends(get_db)):
    try:
        lid = uuid.UUID(leave_id)
    except ValueError:
        raise HTTPException(400, "id 非法")

    row = db.get(EmployeeLeave, lid)
    if not row:
        raise HTTPException(404, "记录不存在")

    info = {
        "employee_id": str(row.employee_id),
        "start": str(row.start_date),
        "end": str(row.end_date),
    }
    db.delete(row)
    db.add(AuditLog(action="DELETE_LEAVE", entity_type="EmployeeLeave", after_data=info))
    db.commit()
    return {"status": "ok"}
