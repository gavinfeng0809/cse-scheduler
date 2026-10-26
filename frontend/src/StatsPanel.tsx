/**
 * 统计分析（用户需求 2026-09-24）：
 *   1) 每人每月、每个班次的数量（含折算天数与工时）
 *   2) 每人全年节假日加班班次——三倍工资日 / 双倍工资日分开
 *   3) 节假日工资倍数维护（3 倍 = 法定节假日当天，2 倍 = 假期内其余休息日，1 = 调休上班日）
 *
 * 折算口径与「排班」页统计列一致：天数 = 计薪工时 ÷ 标准班次工时（默认 8h）
 * → O1/O2/O3（8h）= 1 天，D/N/Y/C（12h）= 1.5 天。基准在「规则配置 → 统计口径」可改。
 */
import { useEffect, useState } from "react";
import { Alert, Button, Card, Select, Table, Tag, Tooltip, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { apiCall } from "./api";

const API = "/api";

type Period = { id: string; name: string; start_date: string; end_date: string; status: string };
type MatrixRow = {
  employee_id: string;
  name: string;
  counts: Record<string, number>;
  total_shifts: number;
  work_days: number;
  holiday_days: number;
  total_hours: number;
};
type OtBucket = {
  codes: Record<string, number>;
  shifts: number;
  days: number;
  hours: number;
  weighted: number;
};
type OtRow = {
  employee_id: string;
  name: string;
  triple: OtBucket;
  double: OtBucket;
  holiday_shifts: number;
  holiday_days: number;
  holiday_hours: number;
  weighted_triple: number;
  weighted_double: number;
  weighted_total: number;
  triple_ratio: number;
};
type HolidayRow = { id: string; date: string; name: string; day_type: string; pay_rate: number };

const RATE_LABEL: Record<number, string> = { 3: "3 倍（法定节假日）", 2: "2 倍（假日休息日）", 1: "1 倍（调休上班）" };
const RATE_COLOR: Record<number, string> = { 3: "red", 2: "orange", 1: "default" };
const SHIFT_COLOR: Record<string, string> = {
  O1: "blue",
  O2: "geekblue",
  O3: "purple",
  D: "cyan",
  N: "magenta",
  C: "volcano",
  Y: "gold",
};

export default function StatsPanel() {
  const [periods, setPeriods] = useState<Period[]>([]);
  const [periodId, setPeriodId] = useState<string | undefined>(undefined);
  const [matrix, setMatrix] = useState<{ codes: string[]; standard_hours: number; rows: MatrixRow[] } | null>(null);

  const [year, setYear] = useState<number>(new Date().getFullYear());
  const [ot, setOt] = useState<{ holiday_days: HolidayRow[]; rows: OtRow[] } | null>(null);
  const [holidays, setHolidays] = useState<HolidayRow[]>([]);
  const [busy, setBusy] = useState(false);

  async function loadPeriods() {
    const ps: Period[] = await apiCall(`${API}/periods`);
    setPeriods(ps);
    if (!periodId && ps.length) {
      const published = ps.filter((p) => p.status === "PUBLISHED");
      const fallback = published.length ? published[published.length - 1] : ps[ps.length - 1];
      setPeriodId(fallback.id);
    }
  }

  async function loadMatrix(pid: string) {
    try {
      setBusy(true);
      setMatrix(await apiCall(`${API}/stats/shift-matrix?period_id=${pid}`));
    } catch (e: any) {
      message.error(e.message || "统计加载失败");
    } finally {
      setBusy(false);
    }
  }

  async function loadOvertime(y: number) {
    try {
      setBusy(true);
      const data = await apiCall(`${API}/stats/holiday-overtime?year=${y}`);
      setOt(data);
      setHolidays(data.holiday_days || []);
    } catch (e: any) {
      message.error(e.message || "年度统计加载失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void loadPeriods();
    void loadOvertime(new Date().getFullYear());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (periodId) void loadMatrix(periodId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [periodId]);

  const years = Array.from(
    new Set<number>([new Date().getFullYear(), ...holidays.map((h) => Number(h.date.slice(0, 4)))])
  ).sort((a, b) => b - a);

  async function setRate(row: HolidayRow, pay_rate: number) {
    try {
      await apiCall(`${API}/holidays/${row.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pay_rate }),
      });
      setHolidays((prev) => prev.map((h) => (h.id === row.id ? { ...h, pay_rate } : h)));
      await loadOvertime(year);
      message.success(`${row.date} 已设为 ${RATE_LABEL[pay_rate]}`);
    } catch (e: any) {
      message.error(e.message || "设置失败");
    }
  }

  const codes = matrix?.codes || [];

  const matrixColumns: any[] = [
    {
      title: "姓名",
      dataIndex: "name",
      key: "name",
      fixed: "left",
      width: 112,
      render: (v: string, r: any) =>
        r.active === false ? (
          <span style={{ color: "#8b93a1" }}>
            {v}
            <Tag style={{ marginLeft: 4, fontSize: 10, lineHeight: "14px" }}>停用</Tag>
          </span>
        ) : (
          v
        ),
    },
    ...codes.map((c) => ({
      title: <Tag color={SHIFT_COLOR[c] || "default"} style={{ margin: 0 }}>{c}</Tag>,
      key: c,
      width: 58,
      align: "center" as any,
      render: (_: any, r: MatrixRow) => r.counts?.[c] || 0,
    })),
    { title: "班次合计", dataIndex: "total_shifts", key: "total_shifts", width: 84, align: "center" as any },
    { title: "工作(天)", dataIndex: "work_days", key: "work_days", width: 84, align: "center" as any },
    { title: "节假(天)", dataIndex: "holiday_days", key: "holiday_days", width: 84, align: "center" as any },
    { title: "总h", dataIndex: "total_hours", key: "total_hours", width: 70, align: "center" as any },
  ];

  const matrixTotal = (matrix?.rows || []).reduce(
    (a, r) => {
      a.total_shifts += r.total_shifts;
      a.work_days += r.work_days;
      a.holiday_days += r.holiday_days;
      a.total_hours += r.total_hours;
      for (const c of codes) a.counts[c] = (a.counts[c] || 0) + (r.counts?.[c] || 0);
      return a;
    },
    { counts: {} as Record<string, number>, total_shifts: 0, work_days: 0, holiday_days: 0, total_hours: 0 }
  );

  const otColumns: any[] = [
    {
      title: "姓名",
      dataIndex: "name",
      key: "name",
      fixed: "left",
      width: 112,
      render: (v: string, r: any) =>
        r.active === false ? (
          <span style={{ color: "#8b93a1" }}>
            {v}
            <Tag style={{ marginLeft: 4, fontSize: 10, lineHeight: "14px" }}>停用</Tag>
          </span>
        ) : (
          v
        ),
    },
    {
      title: "三倍工资日（法定节假日）",
      children: [
        { title: "C", key: "t_c", width: 48, align: "center", render: (_: any, r: OtRow) => r.triple.codes?.C || 0 },
        { title: "Y", key: "t_y", width: 48, align: "center", render: (_: any, r: OtRow) => r.triple.codes?.Y || 0 },
        { title: "班次", key: "t_s", width: 60, align: "center", render: (_: any, r: OtRow) => r.triple.shifts },
        {
          title: (
            <Tooltip title="该区间班次数 × 3（三倍工资日）">
              <span>倍数总和</span>
            </Tooltip>
          ),
          key: "t_w",
          width: 84,
          align: "center",
          render: (_: any, r: OtRow) => <b>{r.triple.weighted}</b>,
        },
      ],
    },
    {
      title: "双倍工资日（假日休息日）",
      children: [
        { title: "C", key: "d_c", width: 48, align: "center", render: (_: any, r: OtRow) => r.double.codes?.C || 0 },
        { title: "Y", key: "d_y", width: 48, align: "center", render: (_: any, r: OtRow) => r.double.codes?.Y || 0 },
        { title: "班次", key: "d_s", width: 60, align: "center", render: (_: any, r: OtRow) => r.double.shifts },
        {
          title: (
            <Tooltip title="该区间班次数 × 2（双倍工资日）">
              <span>倍数总和</span>
            </Tooltip>
          ),
          key: "d_w",
          width: 84,
          align: "center",
          render: (_: any, r: OtRow) => <b>{r.double.weighted}</b>,
        },
      ],
    },
    {
      title: "合计",
      children: [
        {
          title: (
            <Tooltip title="三倍天数 × 3 + 双倍天数 × 2">
              <span>倍数总和</span>
            </Tooltip>
          ),
          dataIndex: "weighted_total",
          key: "w_t",
          width: 92,
          align: "center",
          render: (v: number) => <b style={{ color: "#c41d7f" }}>{v}</b>,
        },
        {
          title: "三倍占比",
          key: "ratio",
          width: 84,
          align: "center",
          render: (_: any, r: OtRow) => (r.weighted_total ? `${r.triple_ratio}%` : "-"),
        },
        { title: "加班工时", dataIndex: "holiday_hours", key: "h_h", width: 84, align: "center" },
      ],
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Alert
        type="info"
        showIcon={false}
        message={
          <span>
            加班合计口径：<b>倍数总和 = 三倍工资日班次 × 3 + 双倍工资日班次 × 2</b>，三倍占比 = 三倍倍数总和 ÷ 倍数总和。
            另：排班页统计列的「天数 = 计薪工时 ÷ 标准班次工时」（O1/O2/O3 = 1 天，D/N/Y/C = 1.5 天），基准在「规则配置 → 统计口径」可改。
          </span>
        }
      />

      <Card
        title="每人每月各班次数量"
        extra={
          <div style={{ display: "flex", gap: 8 }}>
            <Select
              style={{ width: 200 }}
              placeholder="选择周期"
              value={periodId}
              onChange={setPeriodId}
              options={periods.map((p) => ({ value: p.id, label: `${p.name}（${p.status}）` }))}
            />
            <Button icon={<ReloadOutlined />} loading={busy} onClick={() => periodId && void loadMatrix(periodId)}>
              刷新
            </Button>
          </div>
        }
      >
        <Table
          rowKey="employee_id"
          size="small"
          bordered
          loading={busy && !matrix}
          dataSource={matrix?.rows || []}
          columns={matrixColumns}
          pagination={false}
          scroll={{ x: "max-content" }}
          summary={() => (
            <Table.Summary fixed>
              <Table.Summary.Row>
                <Table.Summary.Cell index={0}>
                  <b>合计</b>
                </Table.Summary.Cell>
                {codes.map((c, i) => (
                  <Table.Summary.Cell index={i + 1} key={c} align="center">
                    <b>{matrixTotal.counts[c] || 0}</b>
                  </Table.Summary.Cell>
                ))}
                <Table.Summary.Cell index={codes.length + 1} align="center">
                  <b>{matrixTotal.total_shifts}</b>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={codes.length + 2} align="center">
                  <b>{Math.round(matrixTotal.work_days * 10) / 10}</b>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={codes.length + 3} align="center">
                  <b>{Math.round(matrixTotal.holiday_days * 10) / 10}</b>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={codes.length + 4} align="center">
                  <b>{Math.round(matrixTotal.total_hours * 10) / 10}</b>
                </Table.Summary.Cell>
              </Table.Summary.Row>
            </Table.Summary>
          )}
        />
      </Card>

      <Card
        title="每人全年节假日加班（三倍 / 双倍工资日分开）"
        extra={
          <div style={{ display: "flex", gap: 8 }}>
            <Select
              style={{ width: 140 }}
              value={year}
              onChange={(y) => {
                setYear(y);
                void loadOvertime(y);
              }}
              options={years.map((y) => ({ value: y, label: `${y} 年` }))}
            />
            <Button icon={<ReloadOutlined />} loading={busy} onClick={() => void loadOvertime(year)}>
              刷新
            </Button>
          </div>
        }
      >
        <Table
          rowKey="employee_id"
          size="small"
          bordered
          loading={busy && !ot}
          dataSource={ot?.rows || []}
          columns={otColumns}
          pagination={false}
          scroll={{ x: "max-content" }}
          summary={() => {
            const rs = ot?.rows || [];
            const t = rs.reduce(
              (a, r) => {
                a.tc += r.triple.codes?.C || 0;
                a.ty += r.triple.codes?.Y || 0;
                a.ts += r.triple.shifts;
                a.tw += r.triple.weighted;
                a.dc += r.double.codes?.C || 0;
                a.dy += r.double.codes?.Y || 0;
                a.ds += r.double.shifts;
                a.dw += r.double.weighted;
                a.wt += r.weighted_total;
                a.hours += r.holiday_hours;
                return a;
              },
              { tc: 0, ty: 0, ts: 0, tw: 0, dc: 0, dy: 0, ds: 0, dw: 0, wt: 0, hours: 0 }
            );
            return (
              <Table.Summary fixed>
                <Table.Summary.Row>
                  <Table.Summary.Cell index={0}>
                    <b>全部合计</b>
                  </Table.Summary.Cell>
                  <Table.Summary.Cell index={1} align="center"><b>{t.tc}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={2} align="center"><b>{t.ty}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={3} align="center"><b>{t.ts}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={4} align="center"><b>{t.tw}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={5} align="center"><b>{t.dc}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={6} align="center"><b>{t.dy}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={7} align="center"><b>{t.ds}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={8} align="center"><b>{t.dw}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={9} align="center"><b>{t.wt}</b></Table.Summary.Cell>
                  <Table.Summary.Cell index={10} align="center">
                    <b>{t.wt ? `${Math.round((t.tw / t.wt) * 1000) / 10}%` : "-"}</b>
                  </Table.Summary.Cell>
                  <Table.Summary.Cell index={11} align="center"><b>{Math.round(t.hours * 10) / 10}</b></Table.Summary.Cell>
                </Table.Summary.Row>
              </Table.Summary>
            );
          }}
        />
      </Card>

      <Card
        title={`${year} 年节假日工资倍数维护`}
        extra={
          <span style={{ color: "#6b7280", fontSize: 12 }}>
            三倍 = 法定节假日当天；双倍 = 假期内其余休息日；1 倍 = 调休上班日（不参与加班统计）
          </span>
        }
      >
        <Table
          rowKey="id"
          size="small"
          bordered
          dataSource={holidays}
          pagination={false}
          scroll={{ x: "max-content", y: 360 }}
          columns={[
            { title: "日期", dataIndex: "date", width: 120 },
            { title: "名称", dataIndex: "name", width: 180 },
            { title: "当前", dataIndex: "pay_rate", width: 160, render: (v: number) => <Tag color={RATE_COLOR[v]}>{RATE_LABEL[v]}</Tag> },
            {
              title: "设为",
              key: "set",
              width: 200,
              render: (_: any, r: HolidayRow) => (
                <Select
                  size="small"
                  style={{ width: 170 }}
                  value={r.pay_rate}
                  onChange={(v) => void setRate(r, v)}
                  options={[
                    { value: 3, label: "3 倍（法定节假日）" },
                    { value: 2, label: "2 倍（假日休息日）" },
                  ]}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
