"""可配置规则（团队级）—— 从 `schedule_rules` 表读取，Web 界面可改，无需改代码。

设计：
- `RULE_SPECS` 是唯一事实来源：默认值、类型、取值范围、界面标签与说明都写在这里；
- 数据库里只存"被改过的项"（rule_key -> JSONB），读取时与默认值合并；
- solver / validator 通过 `load_rules()` 取值，因此界面上改完保存、重新排班即生效。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import ScheduleRule

# 规则清单：key -> 元信息。type 支持 int / bool / dict_int（值是 {班次代码: 人数}）
RULE_SPECS: dict[str, dict[str, Any]] = {
    "coverage_workday": {
        "label": "工作日覆盖（每班最低人数）",
        "type": "dict_int",
        "default": {"O2": 2, "O3": 3, "N": 2},
        "help": "工作日/调休日每班最低人数。O1 是弹性补充班次，不设硬性人数要求。",
    },
    "coverage_weekend": {
        "label": "周末覆盖（每班人数，精确）",
        "type": "dict_int",
        "default": {"D": 2, "N": 2},
        "help": "周末只排这两个班次且人数精确等于该值，其余员工休息。",
    },
    "coverage_holiday": {
        "label": "法定假日覆盖（每班人数，精确）",
        "type": "dict_int",
        "default": {"C": 3, "Y": 2},
        "help": "法定假日只排这两个班次且人数精确等于该值。",
    },
    "o1_target_workday": {
        "label": "工作日 O1 目标人数（软目标）",
        "type": "int",
        "default": 12,
        "min": 0,
        "max": 30,
        "help": "每个工作日尽量排到的 O1 人数；填满即止。是软目标：人手不足时只是填不满，不会无解。",
    },
    "workday_headcount_slack": {
        "label": "工作日机动人数（填充目标 = 在职人数 − 该值）",
        "type": "int",
        "default": 2,
        "min": 0,
        "max": 10,
        "help": (
            "【软目标】希望每个工作日大约有多少人在岗：填充目标 = 在职人数 − 该值。"
            "只是「尽量排满」，填不满不会报错、也不会导致无解。"
            "例：22 人 − 2 = 目标 20；17 人 − 2 = 目标 15；16 人 − 2 = 目标 14。"
            "注意：当天上夜班的 2 人 + 夜班后强制休息的 3~4 人不能上白班，"
            "所以实际在岗通常比目标少 1~3 人，属正常现象。"
        ),
    },
    "workday_min_headcount_slack": {
        "label": "工作日最低在岗的容差（硬约束）",
        "type": "int",
        "default": 3,
        "min": 0,
        "max": 10,
        "help": (
            "【硬约束】每个工作日至少要有多少人在岗 = min(填充目标 − 1, 当天可用人数 − 该值)。"
            "该值越大 → 地板越低 → 越容易排出来；该值越小 → 要求越严，可能直接无解。"
            "例：在职 16 人、机动 2（目标 14）、该值 5 → 地板 = min(13, 11) = 11 人/天。"
            "若夜班轮换期间可上白班的人不够（夜班休息期间不能上白班），就会排不出来，此时应调大该值。"
        ),
    },
    "max_consecutive_work_days": {
        "label": "最多连续上班天数",
        "type": "int",
        "default": 7,
        "min": 1,
        "max": 14,
        "help": "跨月也连续计算。",
    },
    "minimum_rest_hours": {
        "label": "班次间最低休息（小时）",
        "type": "int",
        "default": 11,
        "min": 0,
        "max": 24,
        "help": "夜班 21:00–次日 09:00，因此夜班次日不得排白班。",
    },
    "standard_shift_hours": {
        "label": "标准班次工时（统计折算基准）",
        "type": "int",
        "default": 8,
        "min": 4,
        "max": 12,
        "help": (
            "统计里「工作 / 节假」按 实付工时 ÷ 该值 折算成天数："
            "8h 的 O1/O2/O3 = 1 天，12h 的 D/N/Y/C = 1.5 天。"
            "只影响统计显示，不影响排班结果；「总h」始终是真实工时。"
        ),
    },
    "night_default_max_consecutive": {
        "label": "夜班连班上限（默认值）",
        "type": "int",
        "default": 3,
        "min": 1,
        "max": 7,
        "help": "员工个人可在「员工」页单独设置（例如有人可 4 连、有人 3 连）。",
    },
    "night_default_rest_days": {
        "label": "连班后强制休息天数（默认值）",
        "type": "int",
        "default": 3,
        "min": 0,
        "max": 7,
        "help": "员工个人可单独设置。",
    },
    "night_rest_allows_holiday_night": {
        "label": "强制休息期间允许上假日夜班 Y",
        "type": "bool",
        "default": True,
        "help": "Y 不计入连班，是独立班次。",
    },
    "night_rest_allows_day": {
        "label": "强制休息期间允许上白班",
        "type": "bool",
        "default": False,
        "help": "默认关闭：连班达标后只能休息或上 Y。",
    },
    "night_month_max_shifts": {
        "label": "单月夜班次数上限（每人，硬约束）",
        "type": "int",
        "default": 9,
        "min": 0,
        "max": 31,
        "help": (
            "每名有夜班权限的员工在一个排班周期（26 日→次月 25 日）内的夜班次数上限；"
            "0 = 不限制。当前实际最多 9 次，故默认 9 不改变现有排班。"
            "注意：该值 × 有夜班权限人数 必须 ≥ 本周期夜班总需求，否则会排不出班。"
            "本系统目前 8 人有夜班权限、每周期约需 62~68 个夜班，故低于 8 会直接无解"
            "（实测 7 无解）；要卡的更低，先在「班次权限」给更多人开通 N。"
        ),
    },
    "night_month_count_holiday_night": {
        "label": "单月夜班上限是否计入假日夜班 Y",
        "type": "bool",
        "default": False,
        "help": "默认关闭：只统计夜班 N（与连班口径一致）。开启后假日夜班 Y 也计入该上限。",
    },
    "holiday_day_shift_needs_prev_o3": {
        "label": "节假日上 C 班者，前一天工作日必上 O3",
        "type": "bool",
        "default": True,
        "help": (
            "排到节假日白班 C 的人，紧邻的前一天若为工作日（非周末、非假日），"
            "该人当天必须上 O3。假期第 2 天起，前一天已是假日，不受此规则约束。"
            "关闭后不做此限制，校验也不再检查。"
        ),
    },
    "d_shift_no_consecutive": {
        "label": "D 班不允许连续 2 天",
        "type": "bool",
        "default": True,
        "help": "任何员工不得连续两天上 D 班（周末两天的 D 班必须换人）。跨月同样生效。",
    },
    "solver_time_limit": {
        "label": "求解时限（秒）",
        "type": "int",
        "default": 45,
        "min": 5,
        "max": 600,
        "help": "越长时间质量可能更好但更慢。",
    },
    "solver_workers": {
        "label": "求解并行线程数",
        "type": "int",
        "default": 8,
        "min": 1,
        "max": 32,
    },
    # ------- 高级：目标函数权重（一般不用动）-------
    "weight_fair_shift": {"label": "班次均等权重", "type": "int", "default": 300, "min": 0, "max": 100000},
    "weight_day_spread": {"label": "出勤天数极差权重", "type": "int", "default": 800, "min": 0, "max": 100000},
    "weight_min_days": {"label": "抬高最低班次权重", "type": "int", "default": 1500, "min": 0, "max": 100000},
    "weight_fill_day": {"label": "每日填充奖励权重", "type": "int", "default": 60, "min": 0, "max": 100000},
    "weight_fill_o1": {"label": "O1 填充奖励权重", "type": "int", "default": 30, "min": 0, "max": 100000},
    "weight_night_fragment": {"label": "夜班碎片惩罚权重", "type": "int", "default": 300, "min": 0, "max": 100000},
    "weight_o3_pair": {"label": "O3 连块奖励权重", "type": "int", "default": 30, "min": 0, "max": 100000},
    "weight_same_pair": {"label": "班次连块奖励权重", "type": "int", "default": 2, "min": 0, "max": 100000},
    "weight_o1_bonus": {"label": "O1 每班奖励权重", "type": "int", "default": 1, "min": 0, "max": 100000},
    "weight_trans_pen": {"label": "O3→O2 惩罚权重", "type": "int", "default": 50, "min": 0, "max": 100000},
}

DEFAULTS: dict[str, Any] = {k: v["default"] for k, v in RULE_SPECS.items()}


def _validate(key: str, value: Any) -> Any:
    spec = RULE_SPECS.get(key)

    if spec is None:
        raise ValueError(f"未知规则：{key}")

    kind = spec["type"]

    if kind == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要整数")
        if iv < spec.get("min", 0) or iv > spec.get("max", 10**9):
            raise ValueError(f"{key} 需在 {spec.get('min')} ~ {spec.get('max')} 之间")
        return iv

    if kind == "bool":
        return bool(value)

    if kind == "dict_int":
        if not isinstance(value, dict):
            raise ValueError(f"{key} 需要对象，例如 {{\"O2\": 2}}")
        out = {}
        for code, num in value.items():
            code = str(code).strip().upper()
            if not code:
                continue
            try:
                n = int(num)
            except (TypeError, ValueError):
                raise ValueError(f"{key}.{code} 需要整数")
            if n < 0 or n > 30:
                raise ValueError(f"{key}.{code} 需在 0 ~ 30 之间")
            if n > 0:
                out[code] = n
        return out

    return value


def load_rules(db: Session, team_id) -> dict[str, Any]:
    """默认值 + 数据库覆盖项（深拷贝默认值，避免被就地修改）。"""
    merged: dict[str, Any] = {
        k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()
    }

    rows = db.scalars(
        select(ScheduleRule).where(
            ScheduleRule.team_id == team_id, ScheduleRule.enabled.is_(True)
        )
    ).all()

    for row in rows:
        if row.rule_key not in RULE_SPECS:
            continue
        try:
            merged[row.rule_key] = _validate(row.rule_key, row.rule_value)
        except ValueError:
            continue  # 库里存了非法值就忽略，保持默认

    return merged


def rule_specs_meta() -> list[dict[str, Any]]:
    out = []
    for key, spec in RULE_SPECS.items():
        item = {"key": key, "label": spec["label"], "type": spec["type"]}
        for field in ("min", "max", "help", "default"):
            if field in spec:
                item[field] = spec[field]
        out.append(item)
    return out


def save_rules(db: Session, team_id, updates: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("没有需要保存的规则")

    for key, value in updates.items():
        clean = _validate(key, value)
        row = db.scalar(
            select(ScheduleRule).where(
                ScheduleRule.team_id == team_id, ScheduleRule.rule_key == key
            )
        )
        if row is None:
            row = ScheduleRule(team_id=team_id, rule_key=key, rule_value=clean, enabled=True)
            db.add(row)
        else:
            row.rule_value = clean
            row.enabled = True

    db.commit()
    return load_rules(db, team_id)


def reset_rule(db: Session, team_id, key: str) -> dict[str, Any]:
    if key not in RULE_SPECS:
        raise ValueError(f"未知规则：{key}")
    db.execute(
        delete(ScheduleRule).where(
            ScheduleRule.team_id == team_id, ScheduleRule.rule_key == key
        )
    )
    db.commit()
    return load_rules(db, team_id)
