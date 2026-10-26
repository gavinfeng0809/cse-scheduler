import { useEffect, useState } from "react";
import {
  Layout,
  Menu,
  Table,
  Upload,
  Button,
  message,
  Card,
  Tag,
  Select,
  Modal,
  Input,
  DatePicker,
  Alert,
  Tooltip,
  Checkbox,
  Popconfirm,
} from "antd";
import {
  PlayCircleOutlined,
  CheckCircleOutlined,
  DownloadOutlined,
  SendOutlined,
  LockOutlined,
  UnlockOutlined,
  PlusOutlined,
  ReloadOutlined,
  UserOutlined,
  LogoutOutlined,
  KeyOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import RulesPanel from "./RulesPanel";
import AdminPanel from "./AdminPanel";
import StatsPanel from "./StatsPanel";
import HolidayPanel from "./HolidayPanel";
import PeriodPanel from "./PeriodPanel";
import DashboardOverview from "./DashboardOverview";
import { apiCall, UNAUTHORIZED_EVENT } from "./api";

const { Header, Content, Footer } = Layout;

type Employee = {
  id: string;
  name: string;
  email?: string;
  tier?: string;
};

type Shift = {
  id: string;
  code: string;
  name: string;
  start: string;
  end: string;
  paid_hours: number;
  night: boolean;
};

type Period = {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  status: string;
};

type CellInfo = {
  code: string | null;
  status: string;
  locked: boolean;
  source: string;
  assignment_id: string;
};

type GridData = {
  period: Period;
  dates: string[];
  employees: { id: string; name: string; email: string }[];
  cells: Record<string, Record<string, CellInfo>>;
  holidays: Record<string, { name: string; day_type: string }>;
  /** 指定休息 / 请假：{ employee_id: { 'YYYY-MM-DD': { type, label, reason } } } */
  leaves?: Record<string, Record<string, { type: string; label: string; reason: string }>>;
};

type Stat = {
  employee_id: string;
  name: string;
  work_shifts: number;
  holiday_shifts: number;
  total_hours: number;
  /** 每人各班次的折算天数：{ O1: 10, N: 12, ... }（12h 班 = 1.5 天） */
  shift_days?: Record<string, number>;
  /** 每人各班次的班次个数：{ O1: 10, N: 8, ... }（1 个班次记 1 次） */
  shift_counts?: Record<string, number>;
};

type VError = {
  employee_id?: string;
  employee?: string;
  date: string;
  rule: string;
  required?: number;
  actual?: number;
  rest?: number;
};

type PermMatrix = Record<string, Record<string, boolean>>;

const API = "/api";
const TOKEN_KEY = "cse_token";
const USER_KEY = "cse_user";

const SHIFT_COLORS: Record<string, string> = {
  O1: "green",
  O2: "blue",
  O3: "orange",
  D: "cyan",
  N: "purple",
  C: "gold",
  Y: "red",
  OFF: "default",
};

const WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"];

function App() {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [shifts, setShifts] = useState<Shift[]>([]);
  const [periods, setPeriods] = useState<Period[]>([]);
  const [currentPeriodId, setCurrentPeriodId] = useState<string | null>(null);
  const [grid, setGrid] = useState<GridData | null>(null);
  const [stats, setStats] = useState<Stat[]>([]);
  const [validateResult, setValidateResult] = useState<{ status: string; errors: VError[] } | null>(null);
  const [page, setPage] = useState("schedule");
  const [editCell, setEditCell] = useState<{ empId: string; empName: string; date: string; cell?: CellInfo } | null>(null);
  const [editShift, setEditShift] = useState<string>("OFF");
  const [busy, setBusy] = useState(false);

  // ---- 认证状态 ----
  const [token, setToken] = useState<string | null>(localStorage.getItem(TOKEN_KEY));
  const [me, setMe] = useState<{ username: string; role: string } | null>(
    JSON.parse(localStorage.getItem(USER_KEY) || "null")
  );
  const [loginForm, setLoginForm] = useState<{ username: string; password: string }>({ username: "", password: "" });
  const [pwdOpen, setPwdOpen] = useState(false);
  const [pwdForm, setPwdForm] = useState<{ old: string; new1: string; new2: string }>({ old: "", new1: "", new2: "" });
  const [permPage, setPermPage] = useState<{ employees: any[]; shifts: any[]; matrix: PermMatrix } | null>(null);
  const [permDraft, setPermDraft] = useState<PermMatrix>({});

  // ---- 员工编辑 ----
  const [empModal, setEmpModal] = useState<{ open: boolean; id?: string; name: string; email: string; tier: string }>({ open: false, name: "", email: "", tier: "" });

  async function saveEmployee() {
    if (!empModal.name.trim()) {
      message.warning("姓名不能为空");
      return;
    }
    setBusy(true);
    try {
      const payload = { name: empModal.name, email: empModal.email || null, tier: empModal.tier || null };
      if (empModal.id) {
        await apiCall(`${API}/employees/${empModal.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        message.success("员工已更新");
      } else {
        await apiCall(`${API}/employees`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        message.success("员工已新增（默认开通全部班次权限）");
      }
      setEmpModal({ open: false, name: "", email: "", tier: "" });
      loadBase();
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleEmployee(emp: any) {
    setBusy(true);
    try {
      if (emp.active) {
        await apiCall(`${API}/employees/${emp.id}`, { method: "DELETE" });
        message.success(`${emp.name} 已停用（历史排班保留，不再参与排班）`);
      } else {
        await apiCall(`${API}/employees/${emp.id}/activate`, { method: "POST" });
        message.success(`${emp.name} 已恢复`);
      }
      loadBase();
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  // ---- 仪表盘 ----
  const [dashboardData, setDashboardData] = useState<any[] | null>(null);

  async function loadDashboard() {
    const d = await apiCall(`${API}/dashboard`);
    setDashboardData(d);
  }

  useEffect(() => {
    if (page === "dashboard" && token) {
      loadDashboard().catch((e) => message.error(e.message));
    }
  }, [page, token]);

  function saveAuth(tk: string, user: any) {
    localStorage.setItem(TOKEN_KEY, tk);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
    setToken(tk);
    setMe(user);
  }

  function logout() {
    // 【2026-10-10】登出时一并清空已加载的数据：
    // 否则重新登录后页面可能残留旧数据，或停留在"加载中"状态（表格 loading={!grid} 一直转圈）。
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    setToken(null);
    setMe(null);
    setGrid(null);
    setStats([]);
    setPeriods([]);
    setEmployees([]);
    setShifts([]);
    setPermissions(null);
    setDashboardData(null);
    setValidateResult(null);
    setCurrentPeriodId(null);
  }

  async function doLogin() {
    if (!loginForm.username || !loginForm.password) {
      message.warning("请输入用户名和密码");
      return;
    }
    setBusy(true);
    try {
      const resp = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(loginForm),
      });
      const body = await resp.json();
      if (!resp.ok) {
        message.error(body.detail || "登录失败");
        return;
      }
      saveAuth(body.token, { username: body.username, role: body.role });
      message.success(`欢迎，${body.display_name || body.username}`);
      // 数据加载交给 useEffect([token]) 触发（此处手动调用会因闭包旧 token 竞态失败）
    } finally {
      setBusy(false);
    }
  }

  async function doChangePassword() {
    if (pwdForm.new1 !== pwdForm.new2) {
      message.error("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    try {
      await apiCall(`${API}/auth/change-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ old_password: pwdForm.old, new_password: pwdForm.new1 }),
      });
      message.success("密码已修改");
      setPwdOpen(false);
      setPwdForm({ old: "", new1: "", new2: "" });
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  // ---- 权限管理 ----
  async function loadPermissions() {
    const p = await apiCall(`${API}/permissions`, { headers: { Authorization: `Bearer ${token}` } });
    setPermPage(p);
    setPermDraft(p.matrix);
  }

  async function savePermRow(empId: string, empName: string) {
    const allowed = Object.entries(permDraft[empId] || {})
      .filter(([, v]) => v)
      .map(([sid]) => sid);
    setBusy(true);
    try {
      await apiCall(
        `${API}/permissions/${empId}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify(allowed),
        }
      );
      message.success(`${empName} 权限已保存`);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function loadBase() {
    const e = await apiCall(`${API}/employees`);
    const s = await apiCall(`${API}/shifts`);
    setEmployees(e);
    setShifts(s);
  }

  async function loadPeriods(selectLatest = false) {
    const p: Period[] = await apiCall(`${API}/periods`);
    setPeriods(p);
    if (selectLatest && p.length && !currentPeriodId) {
      setCurrentPeriodId(p[p.length - 1].id);
    }
  }

  async function loadGrid(pid: string) {
    const g: GridData = await apiCall(`${API}/periods/${pid}/grid`);
    setGrid(g);
    const st: Stat[] = await apiCall(`${API}/periods/${pid}/stats`);
    setStats(st);
  }

  useEffect(() => {
    if (token) {
      loadBase().catch(() => {});
      loadPeriods(true).catch((e) => message.error(e.message));
    }
  }, [token]);

  // 【2026-10-10】登录过期统一提示：共享 api 模块在收到 401 时会广播该事件
  useEffect(() => {
    const onUnauthorized = () => {
      logout();
      message.warning("登录已过期，请重新登录");
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (currentPeriodId) {
      loadGrid(currentPeriodId);
      setValidateResult(null);
    }
  }, [currentPeriodId, token]);

  useEffect(() => {
    if (page === "permissions" && token) {
      loadPermissions().catch((e) => message.error(e.message));
    }
  }, [page, token]);

  async function uploadExcel(file: File) {
    const form = new FormData();
    form.append("file", file);

    try {
      const result = await apiCall(`${API}/import/excel`, { method: "POST", body: form });

      const hb = result.history_blocks || [];
      const historyNote = hb.length
        ? `，历史入库 ${hb.length} 个周期 / ${hb.reduce((s: number, b: any) => s + b.assignments, 0)} 条班次`
        : "";

      message.success(`导入 ${result.employees_imported} 名员工${historyNote}`);
      loadBase();
    } catch (e: any) {
      message.error(e.message || "导入失败");
    }
    return false;
  }

  async function doSolve() {
    if (!currentPeriodId) return;
    setBusy(true);
    try {
      const r = await apiCall(`${API}/periods/${currentPeriodId}/solve`, { method: "POST" });
      message.success(`求解完成：${r.status}（${Number(r.wall_time).toFixed(1)}s）`);
      await loadPeriods();
      loadGrid(currentPeriodId);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function doValidate() {
    if (!currentPeriodId) return;
    setBusy(true);
    try {
      const r = await apiCall(`${API}/periods/${currentPeriodId}/validate`, {});
      setValidateResult(r);
      if (r.status === "PASS") message.success("校验通过（PASS）");
      else message.warning(`校验未通过：${r.errors.length} 个问题`);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function doPublish() {
    if (!currentPeriodId) return;
    setBusy(true);
    try {
      const r = await apiCall(`${API}/periods/${currentPeriodId}/publish`, { method: "POST" });
      message.success(`已发布（${r.status}）`);
      await loadPeriods();
      loadGrid(currentPeriodId);
    } catch (e: any) {
      if (e.detail?.errors) {
        setValidateResult({ status: "FAIL", errors: e.detail.errors });
        message.error(`发布被拒绝：校验未通过（${e.detail.errors.length} 个问题）`);
      } else {
        message.error(e.message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function doUnpublish() {
    if (!currentPeriodId) return;
    setBusy(true);
    try {
      await apiCall(`${API}/periods/${currentPeriodId}/unpublish`, { method: "POST" });
      message.success("已撤回发布，可修改或重新排班后再次发布");
      await loadPeriods();
      loadGrid(currentPeriodId);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  // 【2026-09-24 修复】导出改为"带令牌取回 Blob 再下载"：
  // 原实现用 window.open() 打开下载链接，浏览器跳转不会带上 JWT 请求头，
  // 后端返回 401 {"detail":"未登录"}，浏览器把这段 JSON 当文件存下来了。
  async function doExport() {
    if (!currentPeriodId) return;
    setBusy(true);
    try {
      const tk = localStorage.getItem(TOKEN_KEY);
      const resp = await fetch(`${API}/periods/${currentPeriodId}/export/download`, {
        headers: tk ? { Authorization: `Bearer ${tk}` } : {},
      });

      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const j = await resp.json();
          detail = typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail ?? j);
        } catch {
          /* 非 JSON 响应，保留状态码 */
        }
        throw new Error(detail);
      }

      const cd = resp.headers.get("content-disposition") || "";
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
      const name = currentPeriod?.name || "schedule";
      const filename = m ? decodeURIComponent(m[1]) : `${name}.xlsx`;

      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);

      message.success(`已导出 ${filename}`);
    } catch (e: any) {
      message.error(`导出失败：${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveCell() {
    if (!editCell || !currentPeriodId) return;
    setBusy(true);
    try {
      await apiCall(
        `${API}/periods/${currentPeriodId}/cells?employee_id=${editCell.empId}&work_date=${editCell.date}&shift_code=${editShift}`,
        { method: "PUT" }
      );
      message.success("修改成功");
      setEditCell(null);
      loadGrid(currentPeriodId);
    } catch (e: any) {
      const errs = e.detail?.errors;
      message.error(
        errs
          ? `${e.message}：${errs
              .map((x: any) => `${x.date} ${x.rule}${x.rest !== undefined ? `(${x.rest}h)` : ""}`)
              .join("；")}`
          : e.message
      );
    } finally {
      setBusy(false);
    }
  }

  async function toggleLock() {
    if (!editCell || !currentPeriodId) return;
    const cell = editCell.cell;
    if (!cell) {
      message.warning("空格子暂不支持锁定，请先设置班次");
      return;
    }
    setBusy(true);
    try {
      await apiCall(
        `${API}/periods/${currentPeriodId}/assignments/${cell.assignment_id}/${cell.locked ? "unlock" : "lock"}`,
        { method: "POST" }
      );
      message.success(cell.locked ? "已解锁" : "已锁定");
      setEditCell(null);
      loadGrid(currentPeriodId);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setBusy(false);
    }
  }

  const statByEmp = new Map(stats.map((s) => [s.employee_id, s]));
  const currentPeriod = periods.find((p) => p.id === currentPeriodId);

  function scheduleColumns() {
    if (!grid) return [];
    const cols: any[] = [
      {
        title: "姓名",
        dataIndex: "name",
        key: "name",
        fixed: "left",
        width: 96,
        render: (v: string, record: any) =>
          record.active === false ? (
            <Tooltip title="已停用：不再参与新排班，历史班次仍保留可查">
              <span style={{ color: "#8b93a1" }}>
                {v}
                <Tag style={{ marginLeft: 4, fontSize: 10, lineHeight: "14px" }}>停用</Tag>
              </span>
            </Tooltip>
          ) : (
            v
          ),
      },
    ];

    for (const d of grid.dates) {
      const dt = dayjs(d);
      const hol = grid.holidays[d];
      const isRest = hol?.day_type === "HOLIDAY";
      const isTransfer = hol?.day_type === "WORKDAY_TRANSFER";
      const wd = WEEKDAY[dt.day() === 0 ? 6 : dt.day() - 1];
      cols.push({
        title: (
          <Tooltip title={hol ? `${hol.name}（${isTransfer ? "调休上班" : "法定假日"}）` : ""}>
            <div
              style={{
                color: isRest ? "#f5222d" : isTransfer ? "#fa8c16" : undefined,
                textAlign: "center",
                lineHeight: 1.2,
              }}
            >
              <div style={{ fontSize: 11 }}>{dt.format("MM/DD")}</div>
              <div style={{ fontSize: 10 }}>
                {wd}
                {isRest ? "休" : isTransfer ? "班" : ""}
              </div>
            </div>
          </Tooltip>
        ),
        key: d,
        width: 46,
        onCell: (record: any) => ({
          onClick: () => {
            const cell = grid.cells[record.id]?.[d];
            setEditCell({ empId: record.id, empName: record.name, date: d, cell });
            setEditShift(cell?.code || "OFF");
          },
          style: { cursor: "pointer", padding: 0 },
        }),
        render: (_: any, record: any) => {
          const cell = grid.cells[record.id]?.[d];
          if (!cell || cell.status !== "WORK" || !cell.code) {
            // 【2026-10-26】指定休息 / 请假日在排班表上标出来（浅蓝底 + 休）
            const leave: any = (grid as any).leaves?.[record.id]?.[d];
            const bg = cell?.locked
              ? "#fff1f0"
              : leave
              ? "rgba(22,104,220,0.12)"
              : undefined;

            return (
              <div
                style={{ textAlign: "center", background: bg, minHeight: 26 }}
                title={leave ? `${leave.label}${leave.reason ? "：" + leave.reason : ""}` : undefined}
              >
                {cell?.locked ? (
                  <LockOutlined style={{ color: "#cf1322", fontSize: 10 }} />
                ) : leave ? (
                  <span style={{ color: "#1668dc", fontSize: 10 }}>休</span>
                ) : (
                  ""
                )}
              </div>
            );
          }
          return (
            <div
              style={{
                textAlign: "center",
                minHeight: 26,
                paddingTop: 2,
                background: cell.locked ? "#fff7e6" : undefined,
                fontWeight: cell.source === "MANUAL" ? 700 : undefined,
              }}
            >
              <Tag color={SHIFT_COLORS[cell.code] || "default"} style={{ margin: 0, fontSize: 11, padding: "0 4px" }}>
                {cell.code}
              </Tag>
              {cell.locked ? <LockOutlined style={{ color: "#d46b08", fontSize: 9 }} /> : null}
            </div>
          );
        },
      });
    }

    // 【2026-10-09 用户要求】统计列：工作(天) 在前，各班次「班次个数」在后；不再显示「节假」与「总h」
    // 注意：工作(天) 是折算天数（12h 班 = 1.5 天）；各班次列是班次个数（1 班 = 1 次）
    const shiftDayOrder = ["O1", "O2", "O3", "D", "N", "C", "Y"];
    const statCols: { title: string; key: string; width: number }[] = [
      { title: "工作(天)", key: "work_shifts", width: 64 },
      ...shiftDayOrder.map((c) => ({ title: c, key: "sd:" + c, width: 44 })),
    ];

    for (const c of statCols) {
      const shiftCode = c.key.startsWith("sd:") ? c.key.slice(3) : null;

      cols.push({
        title: shiftCode ? (
          <Tooltip title={`${shiftCode} 班次数（1 个班次记 1 次）`}>
            <span>{shiftCode}</span>
          </Tooltip>
        ) : (
          c.title
        ),
        key: c.key,
        fixed: "right",
        width: c.width,
        render: (_: any, record: any) => {
          const s = statByEmp.get(record.id) as any;
          if (!s) return <div style={{ textAlign: "center", fontSize: 11 }}>-</div>;
          // 【2026-10-09 用户要求】各班次列显示「班次个数」，1 个班次记 1 次（D/N/C/Y 不再折算 1.5）
          const v = shiftCode ? s.shift_counts?.[shiftCode] ?? 0 : s[c.key];
          return <div style={{ textAlign: "center", fontSize: 11 }}>{v}</div>;
        },
      });
    }

    return cols;
  }

  // 未登录：渲染登录页
  if (!token) {
    return (
      <div className="cse-login-wrap">
        <Card className="cse-login-card">
          <div style={{ textAlign: "center", marginBottom: 20 }}>
            <SafetyCertificateOutlined style={{ fontSize: 42, color: "#1677ff" }} />
            <h2 style={{ margin: "12px 0 4px" }}>CSE 7×24 自动排班系统</h2>
            <span style={{ color: "#888" }}>请登录以继续</span>
          </div>
          <Input
            placeholder="用户名"
            prefix={<UserOutlined style={{ color: "#bbb" }} />}
            value={loginForm.username}
            onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })}
            style={{ marginBottom: 12 }}
            onPressEnter={doLogin}
          />
          <Input.Password
            placeholder="密码"
            value={loginForm.password}
            onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
            style={{ marginBottom: 16 }}
            onPressEnter={doLogin}
          />
          <Button type="primary" block loading={busy} onClick={doLogin}>
            登 录
          </Button>
        </Card>
      </div>
    );
  }

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout>
        <Header className="cse-header">
          <div className="cse-brand">
            <span className="cse-dot" />
            <span className="cse-brand-text">CSE 自动排班系统</span>
          </div>
          <Menu
            className="cse-topmenu"
            theme="dark"
            mode="horizontal"
            selectedKeys={[page]}
            onClick={(x) => setPage(x.key)}
            items={[
              { key: "schedule", label: "排班" },
              { key: "permissions", label: "班次权限" },
              { key: "rules", label: "规则配置" },
              { key: "stats", label: "统计分析" },
              { key: "holidays", label: "节假日管理" },
              ...(me?.role === "admin" ? [{ key: "users", label: "用户管理" }] : []),
              { key: "employees", label: "员工" },
              { key: "shifts", label: "班次" },
              { key: "import", label: "Excel 导入" },
              { key: "dashboard", label: "Dashboard" },
            ]}
          />
          <div className="cse-header-right">
            {currentPeriod && (
              <Tag
                color={
                  currentPeriod.status === "PUBLISHED"
                    ? "green"
                    : currentPeriod.status === "SOLVED"
                    ? "blue"
                    : currentPeriod.status === "HISTORY"
                    ? "purple"
                    : "default"
                }
              >
                {currentPeriod.name} · {currentPeriod.status}
              </Tag>
            )}
            <span className="cse-user">
              <UserOutlined /> {me?.username}（{me?.role}）
            </span>
            <Button
              size="small"
              icon={<KeyOutlined />}
              onClick={() => setPwdOpen(true)}
              style={{ fontSize: 12 }}
            >
              改密码
            </Button>
            <Button size="small" icon={<LogoutOutlined />} onClick={logout} style={{ fontSize: 12 }}>
              退出
            </Button>
          </div>
        </Header>

        <Content className="cse-content">
          {page === "schedule" && (
            <Card
              title={
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <Select
                    style={{ width: 260 }}
                    placeholder="选择周期"
                    value={currentPeriodId}
                    onChange={(v) => setCurrentPeriodId(v)}
                    options={periods.map((p) => ({
                      value: p.id,
                      label: `${p.name}（${p.start_date.slice(5)}~${p.end_date.slice(5)}）${p.status}`,
                    }))}
                  />
                  <Button icon={<PlayCircleOutlined />} loading={busy} onClick={doSolve}>
                    自动排班
                  </Button>
                  <Button icon={<CheckCircleOutlined />} loading={busy} onClick={doValidate}>
                    规则校验
                  </Button>
                  <Button
                    icon={<SendOutlined />}
                    loading={busy}
                    disabled={currentPeriod?.status !== "SOLVED"}
                    onClick={doPublish}
                  >
                    发布
                  </Button>
                  <Popconfirm
                    title="撤回后该周期回到可编辑状态，可重新排班后再次发布。确定撤回？"
                    onConfirm={doUnpublish}
                  >
                    <Button
                      danger
                      ghost
                      loading={busy}
                      disabled={currentPeriod?.status !== "PUBLISHED"}
                    >
                      撤回发布
                    </Button>
                  </Popconfirm>
                  <Button
                    icon={<DownloadOutlined />}
                    loading={busy}
                    onClick={() => void doExport()}
                  >
                    导出 Excel
                  </Button>
                  <Button
                    icon={<ReloadOutlined />}
                    onClick={() => {
                      loadPeriods();
                      if (currentPeriodId) loadGrid(currentPeriodId);
                    }}
                  />
                </div>
              }
            >
              {validateResult && (
                <Alert
                  style={{ marginBottom: 12 }}
                  type={validateResult.status === "PASS" ? "success" : "error"}
                  message={
                    validateResult.status === "PASS"
                      ? "规则校验 PASS：覆盖 / 权限 / 连续工作 / 连夜 / 三连夜休息 / 最低休息时间全部合规"
                      : `校验 FAIL：${validateResult.errors.length} 个问题`
                  }
                  description={
                    validateResult.status === "FAIL" ? (
                      <div style={{ maxHeight: 180, overflow: "auto" }}>
                        {validateResult.errors.slice(0, 60).map((e, i) => (
                          <div key={i} style={{ fontSize: 12 }}>
                            {e.employee ? `${e.employee} ` : ""}
                            {e.date} {e.rule}
                            {e.required !== undefined && e.actual !== undefined
                              ? `（需 ${e.required} 实际 ${e.actual}）`
                              : ""}
                            {e.rest !== undefined ? `（仅休 ${e.rest}h，需 ${e.required}h）` : ""}
                          </div>
                        ))}
                      </div>
                    ) : null
                  }
                />
              )}

              <Alert
                style={{ marginBottom: 12 }}
                type="info"
                showIcon={false}
                message="点击任意格子可人工修改班次（自动校验全部硬约束）；格内小锁 = 已锁定，重排时保留；日期列红字 = 法定假日，橙字 = 调休上班。"
              />

              <Table
                rowKey="id"
                size="small"
                bordered
                loading={!grid}
                dataSource={grid?.employees || []}
                columns={scheduleColumns()}
                pagination={false}
                className="cse-grid"
                scroll={{ x: "max-content", y: "calc(100vh - 350px)" }}
              />
            </Card>
          )}

          {page === "permissions" && (
            <Card
              title="班次权限管理"
              extra={
                <span style={{ color: "#888", fontSize: 12 }}>
                  历史推断仅作初始权限，请重点确认 N/Y 夜班权限；勾选后点行尾"保存"
                </span>
              }
            >
              <Table
                rowKey="id"
                size="small"
                bordered
                loading={!permPage}
                dataSource={permPage?.employees || []}
                pagination={false}
                scroll={{ x: "max-content" }}
                columns={[
                  { title: "姓名", dataIndex: "name", width: 100 },
                  ...(permPage?.shifts || []).map((s: any) => ({
                    title: (
                      <Tag color={SHIFT_COLORS[s.code]} style={{ margin: 0 }}>
                        {s.code}
                        {s.night ? "(夜)" : ""}
                      </Tag>
                    ),
                    key: s.id,
                    width: 70,
                    render: (_: any, record: any) => (
                      <div style={{ textAlign: "center" }}>
                        <Checkbox
                          checked={!!permDraft[record.id]?.[s.id]}
                          onChange={(e) =>
                            setPermDraft({
                              ...permDraft,
                              [record.id]: {
                                ...(permDraft[record.id] || {}),
                                [s.id]: e.target.checked,
                              },
                            })
                          }
                        />
                      </div>
                    ),
                  })),
                  {
                    title: "操作",
                    key: "action",
                    width: 90,
                    render: (_: any, record: any) => (
                      <Button
                        size="small"
                        type="primary"
                        ghost
                        loading={busy}
                        onClick={() => savePermRow(record.id, record.name)}
                      >
                        保存
                      </Button>
                    ),
                  },
                ]}
              />
            </Card>
          )}

          {page === "rules" && <RulesPanel />}
          {page === "stats" && <StatsPanel />}
          {page === "holidays" && <HolidayPanel />}
          {page === "users" && me?.role === "admin" && <AdminPanel />}

          {page === "employees" && (
            <Card
              title="员工"
              extra={
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setEmpModal({ open: true, name: "", email: "", tier: "" })}
                >
                  新增员工
                </Button>
              }
            >
              <Table
                rowKey="id"
                dataSource={employees}
                columns={[
                  {
                    title: "姓名",
                    dataIndex: "name",
                    render: (v, r: any) =>
                      r.active ? v : <span style={{ color: "#bbb", textDecoration: "line-through" }}>{v}</span>,
                  },
                  { title: "Email", dataIndex: "email" },
                  { title: "历史 Tier", dataIndex: "tier" },
                  {
                    title: "状态",
                    dataIndex: "active",
                    width: 80,
                    render: (v) => (v ? <Tag color="green">在职</Tag> : <Tag>已停用</Tag>),
                  },
                  {
                    title: "操作",
                    key: "action",
                    width: 150,
                    render: (_: any, r: any) => (
                      <div style={{ display: "flex", gap: 6 }}>
                        <Button
                          size="small"
                          onClick={() =>
                            setEmpModal({ open: true, id: r.id, name: r.name, email: r.email || "", tier: r.tier || "" })
                          }
                        >
                          编辑
                        </Button>
                        <Popconfirm
                          title={r.active ? "停用后不再参与自动排班，确定？" : "恢复该员工？"}
                          onConfirm={() => toggleEmployee(r)}
                        >
                          <Button size="small" danger={r.active}>
                            {r.active ? "停用" : "恢复"}
                          </Button>
                        </Popconfirm>
                      </div>
                    ),
                  },
                ]}
              />
            </Card>
          )}

          {page === "shifts" && (
            <Card title="班次">
              <Table
                rowKey="id"
                dataSource={shifts}
                columns={[
                  { title: "班次", dataIndex: "code", render: (v) => <Tag color={SHIFT_COLORS[v]}>{v}</Tag> },
                  { title: "名称", dataIndex: "name" },
                  { title: "开始", dataIndex: "start" },
                  { title: "结束", dataIndex: "end" },
                  { title: "小时", dataIndex: "paid_hours" },
                  { title: "夜班", dataIndex: "night", render: (v) => (v ? "是" : "否") },
                ]}
              />
            </Card>
          )}

          {page === "import" && (
            <Card title="导入历史 Excel">
              <Upload accept=".xlsx" showUploadList={false} beforeUpload={uploadExcel}>
                <Button type="primary">上传 CSE Excel</Button>
              </Upload>
              <p style={{ color: "#888", marginTop: 12 }}>
                支持垂直多月份块结构；自动重建 26→25 周期、历史班次入库并生成 Boundary State。
              </p>
            </Card>
          )}

          {page === "dashboard" && (
            <div>
              <h1>Dashboard</h1>

              <DashboardOverview
                onGoto={(action: string, periodId?: string) => {
                  if (action === "holidays") setPage("holidays");
                  else if (action === "rules") setPage("rules");
                  else if (action === "stats") setPage("stats");
                  else if (action !== "periods") {
                    if (periodId) setCurrentPeriodId(periodId);
                    setPage("schedule");
                  }
                }}
              />
              <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
                <Card style={{ minWidth: 150 }}>
                  在职员工：<b>{employees.filter((e: any) => e.active !== false).length}</b>
                </Card>
                <Card style={{ minWidth: 150 }}>
                  当前班次：<b>{shifts.length}</b>
                </Card>
                <Card style={{ minWidth: 150 }}>
                  已发布周期：<b>{(dashboardData || []).length}</b>
                </Card>
              </div>

              <h3>已发布的班次</h3>
              {dashboardData && dashboardData.length === 0 && (
                <Alert
                  type="info"
                  message="还没有已发布的排班周期。到「排班」页完成排班、通过校验后点「发布」。"
                />
              )}
              {(dashboardData || []).length > 0 && (
                <Table
                  rowKey="id"
                  size="small"
                  bordered
                  dataSource={dashboardData || []}
                  pagination={false}
                  columns={[
                    { title: "周期", dataIndex: "name", width: 90 },
                    {
                      title: "日期范围",
                      key: "range",
                      render: (_: any, r: any) => `${r.start_date} ~ ${r.end_date}`,
                    },
                    {
                      title: "发布时间",
                      dataIndex: "published_at",
                      render: (v) => (v ? String(v).slice(0, 16).replace("T", " ") : "-"),
                    },
                    { title: "总班次", dataIndex: "total_shifts", width: 80, align: "center" as any },
                    { title: "夜班", dataIndex: "night_shifts", width: 70, align: "center" as any },
                    { title: "周末班", dataIndex: "weekend_shifts", width: 80, align: "center" as any },
                    { title: "假日班", dataIndex: "holiday_shifts", width: 80, align: "center" as any },
                    { title: "排班人数", dataIndex: "employees_count", width: 90, align: "center" as any },
                    {
                      title: "操作",
                      key: "action",
                      width: 200,
                      render: (_: any, r: any) => (
                        <div style={{ display: "flex", gap: 6 }}>
                          <Button
                            size="small"
                            type="primary"
                            ghost
                            onClick={() => {
                              setCurrentPeriodId(r.id);
                              setPage("schedule");
                            }}
                          >
                            查看排班
                          </Button>
                          <Popconfirm
                            title="撤回后可重新排班，再次发布前他人看到的是旧版。确定？"
                            onConfirm={async () => {
                              setBusy(true);
                              try {
                                await apiCall(`${API}/periods/${r.id}/unpublish`, { method: "POST" });
                                message.success(`${r.name} 已撤回发布`);
                                loadDashboard();
                              } catch (e: any) {
                                message.error(e.message);
                              } finally {
                                setBusy(false);
                              }
                            }}
                          >
                            <Button size="small" danger ghost>
                              撤回
                            </Button>
                          </Popconfirm>
                        </div>
                      ),
                    },
                  ]}
                />
              )}

              <PeriodPanel
                onChanged={() => void loadPeriods()}
                onOpen={(id: string) => {
                  setCurrentPeriodId(id);
                  setPage("schedule");
                }}
              />
            </div>
          )}
        </Content>
      </Layout>

      <Modal
        title={editCell ? `${editCell.empName} · ${editCell.date}` : ""}
        open={!!editCell}
        onCancel={() => setEditCell(null)}
        confirmLoading={busy}
        footer={
          [
            <Button key="cancel" onClick={() => setEditCell(null)}>
              取消
            </Button>,
            <Button
              key="lock"
              icon={editCell?.cell?.locked ? <UnlockOutlined /> : <LockOutlined />}
              onClick={toggleLock}
              loading={busy}
            >
              {editCell?.cell?.locked ? "解锁" : "锁定"}
            </Button>,
            <Button key="ok" type="primary" onClick={saveCell} loading={busy}>
              保存修改
            </Button>,
          ] as any
        }
      >
        <p>选择该日的班次（保存时自动校验权限 / 连续工作 / 连夜 / 三连夜休息 / 最低休息时间）：</p>
        <Select
          style={{ width: "100%" }}
          value={editShift}
          onChange={(v) => setEditShift(v)}
          options={[
            { value: "OFF", label: "OFF（休息）" },
            ...shifts.map((s) => ({ value: s.code, label: `${s.code}（${s.name}）` })),
          ]}
        />
        {editCell?.cell?.locked && (
          <Alert style={{ marginTop: 12 }} type="warning" message="该格已锁定，重排时不会被改动。" />
        )}
      </Modal>

      <Modal
        title="修改密码"
        open={pwdOpen}
        onOk={doChangePassword}
        onCancel={() => setPwdOpen(false)}
        confirmLoading={busy}
      >
        <Input.Password
          placeholder="原密码"
          value={pwdForm.old}
          onChange={(e) => setPwdForm({ ...pwdForm, old: e.target.value })}
          style={{ marginBottom: 12 }}
        />
        <Input.Password
          placeholder="新密码（至少 8 位）"
          value={pwdForm.new1}
          onChange={(e) => setPwdForm({ ...pwdForm, new1: e.target.value })}
          style={{ marginBottom: 12 }}
        />
        <Input.Password
          placeholder="确认新密码"
          value={pwdForm.new2}
          onChange={(e) => setPwdForm({ ...pwdForm, new2: e.target.value })}
        />
      </Modal>
      <Modal
        title={empModal.id ? "编辑员工" : "新增员工"}
        open={empModal.open}
        onOk={saveEmployee}
        onCancel={() => setEmpModal({ open: false, name: "", email: "", tier: "" })}
        confirmLoading={busy}
      >
        <Input
          placeholder="姓名（必填）"
          value={empModal.name}
          onChange={(e) => setEmpModal({ ...empModal, name: e.target.value })}
          style={{ marginBottom: 12 }}
        />
        <Input
          placeholder="Email（可选）"
          value={empModal.email}
          onChange={(e) => setEmpModal({ ...empModal, email: e.target.value })}
          style={{ marginBottom: 12 }}
        />
        <Input
          placeholder="历史 Tier（可选，如 T1(XA)）"
          value={empModal.tier}
          onChange={(e) => setEmpModal({ ...empModal, tier: e.target.value })}
        />
        {!empModal.id && (
          <p style={{ color: "#888", marginTop: 8 }}>新增员工默认开通全部班次权限，可在「班次权限」页调整。</p>
        )}
      </Modal>

      <Footer className="cse-footer">
        <span>CSE 7×24 自动排班系统 · MVP</span>
        <span>排班引擎 CP-SAT · 服务端口 {window.location.port || "80"}</span>
      </Footer>
    </Layout>
  );
}

export default App;
