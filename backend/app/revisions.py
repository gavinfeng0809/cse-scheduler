"""周期修订快照（F1）。

背景：solve 会删除并重写周期内全部 source=AUTO 的排班行。此前没有任何快照，
一旦对已发布周期重新求解，"发布时那一版"就永久丢失（审计表里只有 UNPUBLISH 记录）。

这里在每次求解前与发布前把整周期快照写入 schedule_revisions，并提供回滚。

快照格式（JSONB）：
{
  "period": {"id","name","start_date","end_date","status","version"},
  "captured_at": "2026-09-21T03:00:00",
  "cells": [{"employee_id","work_date","shift_code","status","source","locked"}]
}
"""
from __future__ import annotations

import uuid as _uuid
from datetime import date as _date
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import ScheduleAssignment, SchedulePeriod, ScheduleRevision, Shift


def _read_cells(db: Session, period_id) -> list[dict]:
    shifts = {s.id: s.code for s in db.scalars(select(Shift)).all()}

    rows = db.scalars(
        select(ScheduleAssignment).where(ScheduleAssignment.period_id == period_id)
    ).all()

    return [
        {
            "employee_id": str(r.employee_id),
            "work_date": str(r.work_date),
            "shift_code": shifts.get(r.shift_id),
            "status": r.status,
            "source": r.source,
            "locked": bool(r.locked),
        }
        for r in rows
    ]


def snapshot_period(db: Session, period: SchedulePeriod, reason: str = "") -> ScheduleRevision:
    """把周期当前排班整体存为一个新版本。"""
    last = db.scalar(
        select(func.max(ScheduleRevision.version)).where(
            ScheduleRevision.period_id == period.id
        )
    )

    row = ScheduleRevision(
        period_id=period.id,
        version=int(last or 0) + 1,
        reason=reason or None,
        snapshot={
            "period": {
                "id": str(period.id),
                "name": period.name,
                "start_date": str(period.start_date),
                "end_date": str(period.end_date),
                "status": period.status,
                "version": period.version,
            },
            "captured_at": datetime.utcnow().isoformat(),
            "cells": _read_cells(db, period.id),
        },
    )

    db.add(row)
    db.flush()
    return row


def list_revisions(db: Session, period_id) -> list[dict]:
    rows = db.scalars(
        select(ScheduleRevision)
        .where(ScheduleRevision.period_id == period_id)
        .order_by(ScheduleRevision.version.desc())
    ).all()

    return [
        {
            "id": str(r.id),
            "version": r.version,
            "reason": r.reason,
            "cells": len((r.snapshot or {}).get("cells", [])),
            "captured_at": (r.snapshot or {}).get("captured_at"),
            "period_status": (r.snapshot or {}).get("period", {}).get("status"),
        }
        for r in rows
    ]


def restore_snapshot(db: Session, revision: ScheduleRevision) -> int:
    """把周期排班整体回滚到该快照（含 locked/HISTORY/MANUAL 行）。"""
    shifts = {s.code: s.id for s in db.scalars(select(Shift)).all()}

    db.execute(
        delete(ScheduleAssignment).where(
            ScheduleAssignment.period_id == revision.period_id
        )
    )

    count = 0
    for cell in (revision.snapshot or {}).get("cells", []):
        code = cell.get("shift_code")
        db.add(
            ScheduleAssignment(
                period_id=revision.period_id,
                employee_id=_uuid.UUID(cell["employee_id"]),
                work_date=_date.fromisoformat(cell["work_date"]),
                shift_id=shifts.get(code) if code else None,
                status=cell.get("status") or "OFF",
                source=cell.get("source") or "AUTO",
                locked=bool(cell.get("locked")),
            )
        )
        count += 1

    db.flush()
    return count
