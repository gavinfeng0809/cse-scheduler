"""初始化班次与节假日种子数据。

节假日来源：国务院办公厅《关于2026年部分节假日安排的通知》
（2025-11-04 发布，新华社受权全文发布）
- 元旦：1/1-1/3 假；1/4(日) 班
- 春节：2/15-2/23 假（9 天）；2/14(六)、2/28(六) 班
- 清明：4/4-4/6 假
- 劳动节：5/1-5/5 假；5/9(六) 班
- 端午：6/19-6/21 假
- 中秋：9/25-9/27 假
- 国庆：10/1-10/7 假；9/20(日)、10/10(六) 班
"""
from datetime import date, time
from decimal import Decimal

from sqlalchemy import select

import os

from .database import SessionLocal
from .models import Team, Shift, Holiday, User


def _seed_admin(db):
    """users 空表时创建初始管理员（密码可由 ADMIN_INITIAL_PASSWORD 覆盖）。"""
    from sqlalchemy import func

    count = db.scalar(select(func.count()).select_from(User))

    if count:
        return

    username = "admin"
    # 初始密码必须由环境变量提供（.env 的 ADMIN_INITIAL_PASSWORD）；
    # 不再提供可猜测的默认值，避免部署后忘记修改。
    password = os.environ.get("ADMIN_INITIAL_PASSWORD")

    if not password:
        raise RuntimeError(
            "请先在 .env 中设置 ADMIN_INITIAL_PASSWORD（初始管理员密码）再执行初始化"
        )

    from .auth import hash_password

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            display_name="系统管理员",
        )
    )
    print(f"init_data: seeded admin user '{username}' (change password ASAP)")


DEFAULT_SHIFTS = [
    dict(code="O1", name="O1 弹性班", start_time=time(8, 30), end_time=time(18, 30),
         crosses_midnight=False, paid_hours=Decimal("8"), break_minutes=90, is_night=False),
    dict(code="O2", name="O2", start_time=time(9, 0), end_time=time(17, 0),
         crosses_midnight=False, paid_hours=Decimal("8"), break_minutes=0, is_night=False),
    dict(code="O3", name="O3", start_time=time(13, 30), end_time=time(21, 30),
         crosses_midnight=False, paid_hours=Decimal("8"), break_minutes=0, is_night=False),
    dict(code="C", name="C 节假日白班", start_time=time(9, 0), end_time=time(21, 0),
         crosses_midnight=False, paid_hours=Decimal("12"), break_minutes=0, is_night=False),
    dict(code="D", name="D 周末班", start_time=time(9, 0), end_time=time(21, 0),
         crosses_midnight=False, paid_hours=Decimal("12"), break_minutes=0, is_night=False),
    dict(code="N", name="N 夜班", start_time=time(21, 0), end_time=time(9, 0),
         crosses_midnight=True, paid_hours=Decimal("12"), break_minutes=0, is_night=True,
         counts_as_night_streak=True),
    dict(code="Y", name="Y 节假日夜班", start_time=time(21, 0), end_time=time(9, 0),
         crosses_midnight=True, paid_hours=Decimal("12"), break_minutes=0, is_night=True,
         counts_as_night_streak=False),
]


def _holidays_2026():
    items = []

    def add(names, start, end, day_type):
        d = start
        while d <= end:
            items.append((d, names, day_type))
            d = date.fromordinal(d.toordinal() + 1)

    add("元旦", date(2026, 1, 1), date(2026, 1, 3), "HOLIDAY")
    add("元旦调休上班", date(2026, 1, 4), date(2026, 1, 4), "WORKDAY_TRANSFER")
    add("春节", date(2026, 2, 15), date(2026, 2, 23), "HOLIDAY")
    add("春节调休上班", date(2026, 2, 14), date(2026, 2, 14), "WORKDAY_TRANSFER")
    add("春节调休上班", date(2026, 2, 28), date(2026, 2, 28), "WORKDAY_TRANSFER")
    add("清明节", date(2026, 4, 4), date(2026, 4, 6), "HOLIDAY")
    add("劳动节", date(2026, 5, 1), date(2026, 5, 5), "HOLIDAY")
    add("劳动节调休上班", date(2026, 5, 9), date(2026, 5, 9), "WORKDAY_TRANSFER")
    add("端午节", date(2026, 6, 19), date(2026, 6, 21), "HOLIDAY")
    add("中秋节", date(2026, 9, 25), date(2026, 9, 27), "HOLIDAY")
    add("国庆节", date(2026, 10, 1), date(2026, 10, 7), "HOLIDAY")
    add("国庆节调休上班", date(2026, 9, 20), date(2026, 9, 20), "WORKDAY_TRANSFER")
    add("国庆节调休上班", date(2026, 10, 10), date(2026, 10, 10), "WORKDAY_TRANSFER")
    return items


def init_data():
    db = SessionLocal()
    try:
        team = db.scalar(select(Team).where(Team.code == "CSE"))
        if not team:
            team = Team(name="CSE Team", code="CSE")
            db.add(team)
            db.flush()

        for item in DEFAULT_SHIFTS:
            exists = db.scalar(select(Shift).where(Shift.code == item["code"]))
            if not exists:
                db.add(Shift(**item))

        # 【F3】连班只看 N：已存在的班次行也要同步标记（幂等）
        for code, is_streak in (("N", True), ("Y", False)):
            row = db.scalar(select(Shift).where(Shift.code == code))
            if row and row.counts_as_night_streak != is_streak:
                row.counts_as_night_streak = is_streak

        for holiday_date, name, day_type in _holidays_2026():
            exists = db.scalar(
                select(Holiday).where(
                    Holiday.team_id == team.id,
                    Holiday.holiday_date == holiday_date,
                )
            )
            if not exists:
                db.add(Holiday(team_id=team.id, holiday_date=holiday_date,
                               name=name, day_type=day_type))

        db.commit()
        _seed_admin(db)
        db.commit()
        print("init_data: done")
    finally:
        db.close()


if __name__ == "__main__":
    init_data()
