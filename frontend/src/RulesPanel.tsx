/**
 * 规则配置面板：把原来写死在代码里的硬性条件搬到界面，改完保存 → 重新排班即生效。
 *
 * - 团队级规则：覆盖需求（工作日/周末/假日）、工作日填充目标、出勤与休息、
 *   夜班默认策略、求解参数、高级目标权重；
 * - 员工级夜班策略：支持逐人设置与批量设置（例如"这 5 人可 4 连、那 3 人 3 连"）。
 *
 * 该组件自带请求逻辑（token 与主应用共用 localStorage 的 cse_token），
 * 不依赖 App.tsx 内部状态，便于独立维护。
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Divider,
  InputNumber,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import { ReloadOutlined, SaveOutlined, UndoOutlined, MoonOutlined } from "@ant-design/icons";
import { apiCall } from "./api";
import LeavePanel from "./LeavePanel";

const API = "/api";

type Spec = {
  key: string;
  label: string;
  type: "int" | "bool" | "dict_int";
  min?: number;
  max?: number;
  help?: string;
  default?: any;
};

type EmployeeRow = {
  id: string;
  name: string;
  tier?: string | null;
  active?: boolean;
  night_max_consecutive?: number;
  night_rest_days?: number;
  night_policy_confirmed?: boolean;
  has_night_permission?: boolean;
};

const GROUPS: { title: string; hint: string; keys: string[] }[] = [
  {
    title: "覆盖需求（人数硬指标）",
    hint: "这些是「必须满足」的人数；不满足时求解会失败并给出诊断。O1 是弹性班次，不在此列。",
    keys: ["coverage_workday", "coverage_weekend", "coverage_holiday"],
  },
  {
    title: "工作日填充（在岗人数）",
    hint: "控制每个工作日大概有多少人在岗；下面第一项是硬约束（不会导致无解），其余是软目标。",
    keys: ["workday_headcount_slack", "workday_min_headcount_slack", "o1_target_workday"],
  },
  {
    title: "出勤与休息",
    hint: "跨月同样适用。",
    keys: ["max_consecutive_work_days", "minimum_rest_hours"],
  },
  {
    title: "夜班规则（默认值）",
    hint: "这里是默认值；某个人的具体上限请在下方「员工夜班策略」里单独设置。",
    keys: [
      "night_default_max_consecutive",
      "night_default_rest_days",
      "night_rest_allows_holiday_night",
      "night_rest_allows_day",
      "night_month_max_shifts",
      "night_month_count_holiday_night",
    ],
  },
  {
    title: "节假日与班次组合规则",
    hint: "开启后求解与校验都会按此检查；不合规的排班会报错并禁止发布。",
    keys: ["holiday_day_shift_needs_prev_o3", "d_shift_no_consecutive"],
  },
  {
    title: "统计口径",
    hint: "只影响排班页统计列的显示（工作/节假折算成天数），不影响排班结果。",
    keys: ["standard_shift_hours"],
  },
  {
    title: "求解参数",
    hint: "时间内限越长可能排得更好，但更慢。",
    keys: ["solver_time_limit", "solver_workers"],
  },
  {
    title: "高级：目标函数权重",
    hint: "一般不用改。数值越大越「优先」；改完保存后重新排班生效。",
    keys: [
      "weight_day_spread",
      "weight_min_days",
      "weight_fill_day",
      "weight_fill_o1",
      "weight_fair_shift",
      "weight_night_fragment",
      "weight_o3_pair",
      "weight_same_pair",
      "weight_o1_bonus",
      "weight_trans_pen",
    ],
  },
];

export default function RulesPanel() {
  const [specs, setSpecs] = useState<Spec[]>([]);
  const [current, setCurrent] = useState<Record<string, any>>({});
  const [draft, setDraft] = useState<Record<string, any>>({});
  const [shifts, setShifts] = useState<any[]>([]);
  const [employees, setEmployees] = useState<EmployeeRow[]>([]);
  const [npDraft, setNpDraft] = useState<Record<string, { max: number; rest: number }>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkMax, setBulkMax] = useState(4);
  const [bulkRest, setBulkRest] = useState(3);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [r, s, e] = await Promise.all([
        apiCall(`${API}/rules`),
        apiCall(`${API}/shifts`),
        apiCall(`${API}/employees`),
      ]);
      setSpecs(r.specs || []);
      setCurrent(r.current || {});
      setDraft(JSON.parse(JSON.stringify(r.current || {})));
      setShifts(s || []);
      const actives: EmployeeRow[] = (e || []).filter((x: EmployeeRow) => x.active !== false);
      // 【联动】夜班策略只对拥有夜班(N)权限的人有意义——没有 N 权限的人不列入
      setEmployees(actives);
      const nd: Record<string, { max: number; rest: number }> = {};
      actives.forEach((x) => {
        nd[x.id] = {
          max: x.night_max_consecutive ?? 3,
          rest: x.night_rest_days ?? 3,
        };
      });
      setNpDraft(nd);
    } catch (err: any) {
      message.error(err.message || "加载规则失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function setValue(key: string, value: any) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function setDictValue(key: string, code: string, num: number | null) {
    setDraft((prev) => {
      const next = { ...(prev[key] || {}) };
      if (num === null || num === undefined || num <= 0) delete next[code];
      else next[code] = num;
      return { ...prev, [key]: next };
    });
  }

  const dirtyKeys = specs
    .map((s) => s.key)
    .filter((k) => JSON.stringify(draft[k] ?? null) !== JSON.stringify(current[k] ?? null));

  async function saveRules() {
    if (!dirtyKeys.length) {
      message.info("没有修改");
      return;
    }
    setSaving(true);
    try {
      const payload: Record<string, any> = {};
      dirtyKeys.forEach((k) => {
        payload[k] = draft[k];
      });
      const r = await apiCall(`${API}/rules`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: payload }),
      });
      setCurrent(r.current);
      setDraft(JSON.parse(JSON.stringify(r.current)));
      message.success(`已保存 ${dirtyKeys.length} 项；到「排班」页重新排班即生效`);
    } catch (err: any) {
      message.error(err.message || "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function resetRule(key: string) {
    try {
      const r = await apiCall(`${API}/rules/${key}`, { method: "DELETE" });
      setCurrent(r.current);
      setDraft(JSON.parse(JSON.stringify(r.current)));
      message.success("已恢复默认值");
    } catch (err: any) {
      message.error(err.message || "恢复失败");
    }
  }

  async function saveOneEmployee(row: EmployeeRow) {
    const d = npDraft[row.id];
    try {
      await apiCall(`${API}/employees/${row.id}/night-policy`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ night_max_consecutive: d.max, night_rest_days: d.rest }),
      });
      message.success(`${row.name} 已保存`);
      void load();
    } catch (err: any) {
      message.error(err.message || "保存失败");
    }
  }

  async function saveBulk() {
    if (!selected.length) {
      message.info("请先勾选员工");
      return;
    }
    try {
      const r = await apiCall(`${API}/employees/night-policy/bulk`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          employee_ids: selected,
          night_max_consecutive: bulkMax,
          night_rest_days: bulkRest,
        }),
      });
      message.success(`已更新 ${r.updated} 人：连班上限 ${bulkMax}、强制休息 ${bulkRest} 天`);
      setSelected([]);
      void load();
    } catch (err: any) {
      message.error(err.message || "批量设置失败");
    }
  }

  function renderControl(spec: Spec) {
    const value = draft[spec.key];

    if (spec.type === "bool") {
      return (
        <Switch
          checked={!!value}
          onChange={(v) => setValue(spec.key, v)}
          checkedChildren="允许"
          unCheckedChildren="不允许"
        />
      );
    }

    if (spec.type === "dict_int") {
      return (
        <Space wrap>
          {shifts.map((s) => {
            const cur = (value || {})[s.code];
            return (
              <span key={s.code} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Tag color={s.night ? "purple" : "blue"}>{s.code}</Tag>
                <InputNumber
                  size="small"
                  min={0}
                  max={30}
                  style={{ width: 62 }}
                  value={cur ?? 0}
                  onChange={(v) => setDictValue(spec.key, s.code, v as number)}
                />
              </span>
            );
          })}
        </Space>
      );
    }

    return (
      <InputNumber
        min={spec.min ?? 0}
        max={spec.max ?? 999999}
        value={value}
        onChange={(v) => setValue(spec.key, v)}
        style={{ width: 120 }}
      />
    );
  }

  // 【2026-10-26】按当前在职人数，实时展示「填充目标」和「硬地板」算出来的结果，
  // 避免用户对着公式自己算（这两个值互相耦合，最容易调错）。
  const headcount = employees.length;
  const slackHead = Number(draft["workday_headcount_slack"] ?? 2);
  const slackFloor = Number(draft["workday_min_headcount_slack"] ?? 3);
  const fillTarget = Math.max(1, headcount - slackHead);
  const hardFloor = Math.min(fillTarget - 1, Math.max(0, headcount - slackFloor));

  function liveHeadcountHint(key: string): string {
    if (key === "workday_headcount_slack") {
      return `当前在职 ${headcount} 人 → 填充目标 = ${headcount} − ${slackHead} = ${fillTarget} 人/天（软目标，填不满不报错）`;
    }
    return `当前在职 ${headcount} 人 → 硬地板 = min(${fillTarget}−1, ${headcount}−${slackFloor}) = ${hardFloor} 人/天（硬约束，达不到会无解）`;
  }

  const nightEmployees = employees.filter((e) => e.has_night_permission);

  const employeeColumns = [
    { title: "员工", dataIndex: "name", width: 120 },
    {
      title: "Tier",
      dataIndex: "tier",
      width: 90,
      render: (v: string) => v || "-",
    },
    {
      title: "夜班连班上限",
      width: 160,
      render: (_: any, row: EmployeeRow) => (
        <InputNumber
          min={1}
          max={7}
          value={npDraft[row.id]?.max ?? 3}
          onChange={(v) =>
            setNpDraft((prev) => ({ ...prev, [row.id]: { ...prev[row.id], max: v as number } }))
          }
        />
      ),
    },
    {
      title: "连班后强制休息（天）",
      width: 180,
      render: (_: any, row: EmployeeRow) => (
        <InputNumber
          min={0}
          max={7}
          value={npDraft[row.id]?.rest ?? 3}
          onChange={(v) =>
            setNpDraft((prev) => ({ ...prev, [row.id]: { ...prev[row.id], rest: v as number } }))
          }
        />
      ),
    },
    {
      title: "状态",
      width: 110,
      render: (_: any, row: EmployeeRow) =>
        row.night_policy_confirmed ? <Tag color="green">已确认</Tag> : <Tag>默认/待确认</Tag>,
    },
    {
      title: "操作",
      width: 90,
      render: (_: any, row: EmployeeRow) => (
        <Button size="small" type="link" onClick={() => saveOneEmployee(row)}>
          保存
        </Button>
      ),
    },
  ];

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="这里的规则改完保存后，到「排班」页点自动排班即生效（不需要改代码）。"
        description="覆盖需求是硬指标：不满足会导致求解失败并在响应里给出诊断；其余为软目标，人手不足时只会「填不满」，不会无解。"
      />

      <Card
        title="团队规则"
        loading={loading}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={saving}
              disabled={!dirtyKeys.length}
              onClick={saveRules}
            >
              保存{dirtyKeys.length ? `（${dirtyKeys.length} 项待保存）` : ""}
            </Button>
          </Space>
        }
      >
        {GROUPS.map((group) => {
          const items = group.keys
            .map((k) => specs.find((s) => s.key === k))
            .filter((x): x is Spec => !!x);
          if (!items.length) return null;
          return (
            <div key={group.title} style={{ marginBottom: 18 }}>
              <Divider orientation="left" style={{ margin: "8px 0" }}>
                {group.title}
              </Divider>
              <div style={{ fontSize: 12, color: "#888", marginBottom: 8 }}>{group.hint}</div>
              {items.map((spec) => (
                <div
                  key={spec.key}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "6px 0",
                    borderBottom: "1px dashed #f0f0f0",
                  }}
                >
                  <div style={{ width: 300 }}>
                    <div>
                      {spec.label}
                      {dirtyKeys.includes(spec.key) && (
                        <Tag color="orange" style={{ marginLeft: 6 }}>
                          未保存
                        </Tag>
                      )}
                    </div>
                    {spec.help && (
                      <div style={{ fontSize: 12, color: "#999" }}>{spec.help}</div>
                    )}
                    {(spec.key === "workday_headcount_slack" ||
                      spec.key === "workday_min_headcount_slack") && (
                      <div style={{ fontSize: 12, color: "#1668dc", marginTop: 4, fontWeight: 600 }}>
                        {liveHeadcountHint(spec.key)}
                      </div>
                    )}
                  </div>
                  <div style={{ flex: 1 }}>{renderControl(spec)}</div>
                  <Tooltip title={`默认值：${JSON.stringify(spec.default)}`}>
                    <Popconfirm
                      title="恢复为默认值？"
                      onConfirm={() => resetRule(spec.key)}
                      okText="恢复"
                      cancelText="取消"
                    >
                      <Button size="small" icon={<UndoOutlined />} type="text">
                        恢复默认
                      </Button>
                    </Popconfirm>
                  </Tooltip>
                </div>
              ))}
            </div>
          );
        })}
      </Card>

      <Card
        title={
          <span>
            <MoonOutlined /> 员工夜班策略（连班上限 / 强制休息天数）
          </span>
        }
        style={{ marginTop: 16 }}
        loading={loading}
        extra={
          <Space>
            <span style={{ fontSize: 12 }}>勾选后批量设置：</span>
            <InputNumber
              min={1}
              max={7}
              addonBefore="连班上限"
              value={bulkMax}
              onChange={(v) => setBulkMax(v as number)}
              style={{ width: 150 }}
            />
            <InputNumber
              min={0}
              max={7}
              addonBefore="休息天数"
              value={bulkRest}
              onChange={(v) => setBulkRest(v as number)}
              style={{ width: 150 }}
            />
            <Button type="primary" disabled={!selected.length} onClick={saveBulk}>
              批量应用（已选 {selected.length} 人）
            </Button>
          </Space>
        }
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="列表只显示拥有夜班(N)权限的员工——夜班策略与班次权限联动。"
          description={
            nightEmployees.length < employees.length
              ? `当前有 ${employees.length - nightEmployees.length} 人在职员工没有 N 权限，已自动不在此表；` +
                `如需为某人设置夜班策略，请先到「班次权限」页为其开通 N 班次（撤销 N 权限时其夜班策略会自动清空）。`
              : "所有在职员工都有 N 权限。"
          }
        />
        <div style={{ fontSize: 12, color: "#888", marginBottom: 8 }}>
          例如：有人可以连续 4 个夜班再休 3 天，有人只能 3 连；改完保存后重新排班即生效。
          「已确认」表示该人的策略已被人工设置过。
        </div>
        <Table
          rowKey="id"
          size="small"
          dataSource={nightEmployees}
          columns={employeeColumns as any}
          pagination={false}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys as string[]),
          }}
        />
      </Card>

      {/* 【2026-10-26】指定休息 / 请假 也是规则，统一放在本页管理 */}
      <div style={{ marginTop: 8 }}>
        <LeavePanel />
      </div>
    </div>
  );
}
