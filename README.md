# CSE 7×24 自动排班系统

面向 7×24 值班团队（CSE 技术支持）的**自动排班系统**：用 Google OR-Tools CP-SAT 求解器在满足一整套业务硬约束的前提下自动生成月度排班表，并把**所有排班规则搬到 Web 界面**上配置——规则调整不需要改代码、不需要重新部署。

> 核心特点：**规则可配 + 硬约束可验证 + 跨月连续**。
> 每次排班前自动存快照，排完自动校验，不合规不允许发布，任何一步都能回滚。

---

## 目录

- [功能概览](#功能概览)
- [技术栈](#技术栈)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [核心概念](#核心概念)
- [排班规则体系](#排班规则体系)
- [统计口径](#统计口径)
- [API 一览](#api-一览)
- [数据模型](#数据模型)
- [日常运维](#日常运维)
- [已知限制](#已知限制)

---

## 功能概览

| 模块 | 能力 |
|---|---|
| **排班** | 一键自动排班（CP-SAT）、人工改单格（实时校验硬约束）、锁定单元格、跨月连续性自动衔接、规则校验、发布/撤回发布、历史修订版本回滚、导出 Excel |
| **规则配置** | 29 项排班规则全部界面化（覆盖人数、在职填充、连班、夜班、节假日与班次组合、统计口径、求解参数、目标权重）；员工级夜班策略（每人可设 4 连/3 连）；**指定休息/请假**（个别人员特定某几天休息） |
| **班次权限** | 员工 × 班次权限矩阵，历史班次可反推初始权限 |
| **统计分析** | 每人每月各班次数量（班次数 + 折算天数 + 工时）；每人全年**节假日加班**（三倍/双倍工资日分开，倍数总和 + 三倍占比）；年度矩阵 |
| **节假日管理** | **联网同步万年历**（timor.tech → holiday-cn 多源回退），自动区分三倍/双倍工资日；支持手工录入、按国家规定一键套用倍数 |
| **用户管理** | 管理员/排班员/只读三种角色，支持从员工创建账号、改角色、停用、重置密码（含"不能停用自己""至少保留一名管理员"等护栏） |
| **Dashboard** | 待办与异常提醒（待发布/校验异常/节假日未配置/周期缺口）、最新周期指标、年度加班概览、最近操作审计 |
| **周期管理** | 按 `26 日 ~ 次月 25 日` 模式批量生成整年周期（已存在/重叠自动跳过），可同时同步该年节假日 |

---

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI 0.116 · SQLAlchemy 2.0 · Alembic · Pydantic v2 |
| 求解器 | **Google OR-Tools CP-SAT 9.14**（硬约束建模 + 软目标加权优化） |
| 异步 | Celery 5.5 + Redis |
| 数据库 | PostgreSQL 16 |
| 前端 | React 18 + TypeScript + Ant Design 5 + Vite 7（单页应用，暗色/浅色企业风主题） |
| 网关 | Nginx 1.27（静态资源 + `/api` 反向代理） |
| 部署 | Docker Compose（6 个容器：nginx / api / worker / frontend / postgres / redis） |

---

## 系统架构

```
                    ┌─────────────────────────────┐
   浏览器  ────────▶│ nginx :80 (对外 APP_PORT)    │
                    │  ├─ /        → frontend(静态)│
                    │  └─ /api/*   → api:8000      │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ FastAPI (api)                │
                    │  ├─ 鉴权 JWT（滑动续期）      │
                    │  ├─ CRUD（员工/班次/周期/…）  │
                    │  ├─ 求解 CP-SAT → solver.py  │
                    │  └─ 校验 validator.py        │
                    └───────┬──────────────┬──────┘
                            │              │
                  ┌─────────▼───┐   ┌──────▼───────┐
                  │ PostgreSQL  │   │ Redis        │
                  │  业务数据    │   │ Celery broker│
                  └─────────────┘   └──────┬───────┘
                                           │
                                  ┌────────▼────────┐
                                  │ Celery worker   │
                                  └─────────────────┘
```

**求解与校验分离**：`solver.py` 生成排班（只处理能与不能），`validator.py` 独立复核（能发现任何违反规则的情况，包括人工改动），两者共用同一套规则来源 `app/rules.py::RULE_SPECS`。

---

## 快速开始

### 1. 准备环境

- Docker 20+ 与 Docker Compose v2
- 可用端口（默认 `3005`，可通过 `APP_PORT` 修改）

### 2. 配置

```bash
git clone <this-repo> cse-scheduler
cd cse-scheduler
cp .env.example .env
```

编辑 `.env`，**至少修改这几项**：

```ini
POSTGRES_PASSWORD=<足够长的随机串>
DATABASE_URL=postgresql+psycopg://cse:<同一个密码>@postgres:5432/cse_scheduler
JWT_SECRET=<足够长的随机串>
ADMIN_INITIAL_PASSWORD=<初始管理员密码>
APP_PORT=3005
```

> 生成随机串：`openssl rand -base64 32`

### 3. 启动

```bash
docker compose up -d --build
docker compose ps            # 6 个容器应为 running / healthy
```

### 4. 首次初始化（建表 + 种子数据）

```bash
docker compose exec api alembic upgrade head
docker compose exec api python -m app.init_data
```

`init_data` 会创建：团队、7 个班次（O1/O2/O3/D/N/C/Y）、默认管理员账号（`admin`，密码取 `ADMIN_INITIAL_PASSWORD`，未设置则为内置默认值——**首次登录后请立即修改**）。

### 5. 访问

```
http://<服务器IP>:3005/
```

### 6. 开始使用（推荐顺序）

1. **员工**：录入人员（或「Excel 导入」历史班表）
2. **班次权限**：勾选每人可上的班次（N/Y 为夜班权限）
3. **规则配置**：按团队实际情况调整规则（含员工夜班策略、指定休息）
4. **节假日管理**：联网同步当年法定节假日
5. **Dashboard → 排班周期 → 自动生成周期**：铺出整年周期
6. **排班**：选周期 → 自动排班 → 规则校验 → 发布

---

## 核心概念

### 排班周期（26 → 25）

周期命名 `YYYY-MM`，覆盖 **上月 26 日 ~ 本月 25 日**。例如：

| 周期 | 日期范围 |
|---|---|
| 2026-11 | 2026-10-26 ~ 2026-11-25 |
| 2026-12 | 2026-11-26 ~ 2026-12-25 |

**跨月连续性**：周期首日的状态依赖上一周期期末（连续上班天数、连续夜班数、剩余强制休息天数、末班班次），由 `schedule_boundary_states` 携带。
因此**改了前一个月，后一个月必须重排**，否则月份边界会对不上（校验会报 `NIGHT_REST_CARRIED` / `SAME_SHIFT_CHAIN_CROSS` 等）。

### 班次代码

| 代码 | 含义 | 时长 | 特性 |
|---|---|---|---|
| `O1` | 弹性班 08:30–18:30 | 8h（含 90min 休息） | **弹性补充班次**，不设硬性人数要求 |
| `O2` | 09:00–17:00 | 8h | 工作日主力 |
| `O3` | 13:30–21:30 | 8h | 工作日主力（晚班） |
| `D` | 周末白班 09:00–21:00 | 12h | 仅周末 |
| `N` | 夜班 21:00–09:00 | 12h | 计入夜班连班 |
| `C` | 节假日白班 09:00–21:00 | 12h | 仅法定假日 |
| `Y` | 节假日夜班 21:00–09:00 | 12h | **不计入夜班连班**（独立班次） |

### 员工状态

- **在职/停用**：停用只表示"以后不再参与排班"，**历史周期的班次仍然可见、可统计**（导出也保留）
- **班次权限**：每人可上哪些班次；夜班策略只对有 `N` 权限的人生效
- **夜班策略（员工级）**：`night_max_consecutive`（可几连）、`night_rest_days`（连班后强制休息几天）、`night_policy_confirmed`

### 单元格状态

| 状态 | 说明 |
|---|---|
| `AUTO` | 求解器生成 |
| `MANUAL` | 人工修改（重排时保留，界面加粗） |
| `HISTORY` | Excel 导入的历史班次（视为既成事实） |
| `locked` | 锁定，重排不覆盖 |

---

## 排班规则体系

全部 **29 项规则**见 [`docs/规则清单.md`](docs/规则清单.md)，均可在界面「规则配置」修改。核心几类：

**① 覆盖需求（硬指标）**
- 工作日每班最低人数（默认 `O2:2 / O3:3 / N:2`；O1 弹性无硬要求）
- 周末精确人数（默认 `D:2 / N:2`，其余休息）
- 法定假日精确人数（默认 `C:3 / Y:2`）

**② 工作日填充（软目标 + 硬地板）**

```
填充目标 = 在职人数 − 工作日机动人数          （软目标：填不满不报错）
硬地板   = min(填充目标 − 1, 当天可用人数 − 最低在岗容差)   （硬约束：达不到则无解）
```

> 夜班强制休息期间**不能上白班**，因此实际可上白班人数 ≈ 在职 − 上夜班人数 − 休息中人数。人数偏紧时需调大「最低在岗容差」。

**③ 出勤与休息**：最多连续上班天数（默认 7，跨月连续计算）、班次间最低休息小时（默认 11）

**④ 夜班规则**：连班上限（默认 3~4，可按人设置）、连班后强制休息（默认 3~4 天）、休息窗口内允许的班次、**单月夜班次数上限**（默认 9，0=不限）

**⑤ 节假日与班次组合**：节假日上 `C` 班者前一天工作日必须上 `O3`、`D` 班不允许连续两天（均跨月生效）

**⑥ 指定休息/请假**：给个别人员指定特定日期休息，排班时强制 `OFF`，校验项 `LEAVE_VIOLATION` 兜底

**校验项速查**（`validator.py`）：`COVERAGE`、`PERMISSION`、`MAX_CONSECUTIVE_WORK`（含 `_CROSS`）、`MAX_CONSECUTIVE_NIGHT`、`NIGHT_REST_AFTER_BLOCK`/`_CROSS_BLOCK`/`_CARRIED`、`REST_HOURS`、`NIGHT_MONTH_MAX`、`LEAVE_VIOLATION`、`D_NO_CONSECUTIVE`、`HOLIDAY_C_NEEDS_PREV_O3`、`SAME_SHIFT_CHAIN_CROSS`、`HOLIDAY_CALENDAR_MISSING`（警告）

---

## 统计口径

### 折算天数

```
折算天数 = 计薪工时 ÷ 标准班次工时（默认 8h）
```

即：`O1/O2/O3`（8h）= **1 天**，`D/N/C/Y`（12h）= **1.5 天**。基准值在「规则配置 → 统计口径」可调。

### 节假日加班（倍数总和）

```
倍数总和 = 三倍工资日班次数 × 3 + 双倍工资日班次数 × 2
三倍占比 = 三倍倍数总和 ÷ 倍数总和
```

三倍/双倍由「节假日管理」里每天的 `pay_rate` 决定（3 = 法定节假日当天，2 = 假期内其余休息日，1 = 调休上班日不参与统计）。

### 普通工时 vs 节假日工时

节假日加班单独统计，**不计入普通工作时间**；人均工时参考全 `O1` 班次人员。

---

## API 一览

所有业务接口挂载在 `/api` 下，统一要求 `Authorization: Bearer <JWT>`（登录接口除外）。
交互式文档：`http://<host>:<port>/api/docs`（Swagger UI）。

| 分组 | 主要端点 |
|---|---|
| 鉴权 | `POST /auth/login`、`GET /auth/me`、`POST /auth/change-password` |
| 员工 | `GET/POST /employees`、`PUT/DELETE /employees/{id}`、`POST /employees/{id}/activate` |
| 班次权限 | `GET /permissions`、`PUT /permissions/{employee_id}` |
| 夜班策略 | `PUT /employees/{id}/night-policy`、`PUT /employees/night-policy/bulk` |
| 指定休息 | `GET/POST /leaves`、`PUT/DELETE /leaves/{id}` |
| 规则 | `GET/PUT /rules`、`DELETE /rules/{key}` |
| 周期 | `GET/POST /periods`、`POST /periods/auto-generate` |
| 排班 | `POST /periods/{id}/solve`、`GET /periods/{id}/grid`、`PUT /periods/{id}/cells`、`POST /periods/{id}/publish`\|`unpublish` |
| 校验/统计 | `GET /periods/{id}/validate`、`GET /periods/{id}/stats`、`GET /stats/shift-matrix`、`GET /stats/holiday-overtime` |
| 修订 | `GET /periods/{id}/revisions`、`POST /periods/{id}/revisions/{rid}/restore` |
| 导出 | `GET /periods/{id}/export/download` |
| 节假日 | `GET/POST /holidays`、`POST /holidays/sync`、`GET /holidays/sync/preview`、`POST /holidays/pay-rate/auto` |
| 用户/仪表盘 | `GET/POST /users`、`GET /dashboard`、`GET /dashboard/overview` |

求解接口支持消融排障参数：`?dis=carry,nightrest,niso`（逐个关闭跨月/夜班休息/夜班块约束，用于定位无解原因）。

---

## 数据模型

```
teams ─┬─ employees ─┬─ employee_shift_permissions
       │             ├─ employee_leaves            （指定休息/请假）
       │             └─ schedule_boundary_states   （跨月期末状态快照）
       ├─ shifts
       ├─ holidays                                （法定假日/调休 + 工资倍数）
       ├─ schedule_periods ─┬─ schedule_assignments（员工×日期 的班次）
       │                    ├─ schedule_locks
       │                    └─ schedule_revisions  （修订快照，可回滚）
       ├─ schedule_rules                           （规则覆盖项，只存改过的）
       └─ users / audit_logs
```

关键设计：
- **规则默认值写在代码**（`RULE_SPECS`），数据库只存"被改过的项"，读取时合并 → 升级不会冲掉用户配置
- **修订快照**：每次排班/发布前自动存档，出问题可一键回到任一版本
- **审计日志**：规则修改、发布/撤回、节假日同步、用户操作等全部留痕

---

## 日常运维

```bash
# 查看状态 / 日志
docker compose ps
docker compose logs -f api

# 备份 / 恢复数据库
./scripts/backup.sh
./scripts/restore.sh backups/cse_YYYYMMDD_HHMMSS.sql.gz

# 升级代码后
git pull
docker compose up -d --build
docker compose exec api alembic upgrade head   # 如有新迁移

# 重建单个服务
docker compose build api worker && docker compose up -d api worker
```

**健康检查**：`curl -s http://<host>:<port>/api/rules -H "Authorization: Bearer <token>"`

---

## 已知限制

1. **跨月必须成对重排**：改动某月排班后，下一个月的排班需重排，否则月份边界校验会失败。
2. **人手是硬天花板**：夜班容量 = 有 `N` 权限的在职人数 × 单月夜班上限；人数不足时无法排出（求解会返回具体诊断）。
3. **节假日需每年更新**：国务院每年 11–12 月公告次年安排，之后在「节假日管理」点一次联网同步即可。
4. **求解耗时**：单周期 30~60 秒（CP-SAT，可在规则里调时限）。
5. 本系统面向单团队（team）设计，多团队需扩展 `team_id` 相关逻辑。

---

## 说明

- 仓库内所有示例数据均为脱敏后的通用描述，不含任何真实人员信息与生产环境密钥。
- 部署前请务必修改 `.env` 中的密码与 `JWT_SECRET`，并在首次登录后修改管理员密码。
