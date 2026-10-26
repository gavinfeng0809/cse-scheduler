"""诊断：2026-10 补全求解时 fixed_dates 覆盖哪些日期、coverage 决策。"""
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ScheduleAssignment, SchedulePeriod, Holiday


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


db = SessionLocal()

period = db.scalar(select(SchedulePeriod).where(SchedulePeriod.name == "2026-10"))
print("period:", period.name, period.start_date, "->", period.end_date, "status:", period.status)

rows = db.scalars(
    select(ScheduleAssignment).where(ScheduleAssignment.period_id == period.id)
).all()

print("total assignments in DB:", len(rows))

fixed_map = {}
auto_dates = set()
for r in rows:
    if r.locked or r.source != "AUTO":
        fixed_map[(r.employee_id, r.work_date)] = r.shift_id
    else:
        auto_dates.add(r.work_date)

fixed_dates = {d for (_e, d) in fixed_map}
print("fixed dates:", sorted(str(d) for d in fixed_dates))
print("auto-only dates:", sorted(str(d) for d in auto_dates - fixed_dates))

holidays = {
    x.holiday_date: x
    for x in db.scalars(select(Holiday).where(Holiday.team_id == period.team_id)).all()
}

print("\n每日 coverage 决策（solve 时视角 = 只有 HISTORY 为 fixed）：")
for d in daterange(period.start_date, period.end_date):
    h = holidays.get(d)
    tag = "SKIP(fixed)" if d in fixed_dates else "APPLY"
    htype = f"{h.day_type}" if h else ("weekend" if d.weekday() >= 5 else "workday")
    print(f"  {d} {htype:<16} {tag}")

db.close()
