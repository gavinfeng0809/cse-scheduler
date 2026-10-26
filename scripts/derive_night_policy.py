"""【F3】从历史排班反推每人的夜班连班策略，写入 employees。

业务确认：8 名有夜班权限的人里，有人可以 4 连 + 休 3，有人 3 连 + 休 3/2。

反推口径（只统计 N，假日夜班 Y 不参与连班计数）：
- 连班上限 = 历史最长"连续自然日上 N"的天数（下限 3、上限 5）；
- 强制休息天数 = 每个"≥3 连"的夜班块结束后，紧接着连续休息（无任何班次）的天数众数
  （下限 1、上限 4；无样本用 3）。
- 只对历史上真正上过 N 的人写策略；其余人保持默认 3/3。
- night_policy_confirmed 一律置 False：这是机器反推的初值，必须人工确认。

用法（容器内）：
    docker compose exec -T api python - < scripts/derive_night_policy.py
"""
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Employee, ScheduleAssignment, Shift


def main() -> None:
    db = SessionLocal()
    try:
        employees = db.scalars(
            select(Employee).where(Employee.active.is_(True)).order_by(Employee.sort_order)
        ).all()

        rows = db.execute(
            select(
                ScheduleAssignment.employee_id,
                ScheduleAssignment.work_date,
                Shift.code,
                Shift.counts_as_night_streak,
            )
            .join(Shift, Shift.id == ScheduleAssignment.shift_id)
            .where(ScheduleAssignment.status == "WORK")
        ).all()

        per_emp: dict = defaultdict(dict)
        for emp_id, work_date, code, is_streak in rows:
            per_emp[emp_id][work_date] = (code, bool(is_streak))

        print(f"{'员工':<9}{'最长N连班':>9}{'块数':>5}{'块后休息样本':>26}{'→上限':>7}{'→休息':>7}")
        print("-" * 72)

        written = 0

        for employee in employees:
            days = per_emp.get(employee.id, {})

            if not days:
                continue

            ordered = sorted(days)
            first, last = ordered[0], ordered[-1]

            longest = 0
            run = 0
            blocks = 0
            rest_obs: list[int] = []

            cursor = first
            while cursor <= last:
                entry = days.get(cursor)
                is_streak = bool(entry and entry[1])

                if is_streak:
                    run += 1
                    longest = max(longest, run)
                else:
                    if run >= 3:
                        blocks += 1
                        # 块结束后，连续"完全没有班次"的天数 = 实际休息天数
                        rested = 0
                        probe = cursor
                        while probe <= last and days.get(probe) is None:
                            rested += 1
                            probe += timedelta(days=1)
                        rest_obs.append(rested)
                    run = 0

                cursor += timedelta(days=1)

            if run >= 3:  # 历史末尾就是夜班块
                blocks += 1

            if longest == 0:
                # 从来没上过 N：保持默认，不写策略
                print(f"{employee.name:<9}{'-':>9}{'-':>5}{'-':>26}{'(跳过)':>7}{'':>7}")
                continue

            limit = min(5, max(3, longest))
            rest_days = Counter(rest_obs).most_common(1)[0][0] if rest_obs else 3
            rest_days = min(4, max(1, rest_days))

            employee.night_max_consecutive = limit
            employee.night_rest_days = rest_days
            employee.night_policy_confirmed = False
            written += 1

            print(
                f"{employee.name:<9}{longest:>9}{blocks:>5}"
                f"{str(sorted(rest_obs)):>26}{limit:>7}{rest_days:>7}"
            )

        db.commit()
        print("-" * 72)
        print(f"已写入 {written} 名有夜班历史的员工策略初值（night_policy_confirmed=False，待人工确认）")
        print("说明：连班上限=历史上最长连续 N 天数；休息天数=夜班块后连续无班天数的众数")
    finally:
        db.close()


if __name__ == "__main__":
    main()
