/**
 * 指定休息 / 请假（用户 2026-10-26 需求）：
 *   「个别人员需要在特定某几天进行休息」——在这里登记，排班（自动排班）时强制这些天休息。
 *
 * 说明：
 *   - 登记后需要**重新排班**才生效；重复排班时这些天一定是休息（OFF）；
 *   - 规则校验会检查：若有人在指定休息日被排了班，会报 `LEAVE_VIOLATION` 并禁止发布；
 *   - 夜班人员被指定休息较多时可能挤占夜班容量导致"无解"，届时求解会给出诊断。
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { apiCall } from "./api";

const API = "/api";

type LeaveRow = {
  id: string;
  employee_id: string;
  employee: string;
  active: boolean;
  start_date: string;
  end_date: string;
  days: number;
  leave_type: string;
  leave_type_label: string;
  status: string;
  reason: string;
};

type Emp = { id: string; name: string; active?: boolean };

const TYPE_COLOR: Record<string, string> = {
  REST: "blue",
  ANNUAL: "green",
  SICK: "orange",
  PERSONAL: "purple",
  OTHER: "default",
};

const TYPE_OPTIONS = [
  { value: "REST", label: "指定休息（特定某几天休息）" },
  { value: "ANNUAL", label: "年假" },
  { value: "SICK", label: "病假" },
  { value: "PERSONAL", label: "事假" },
  { value: "OTHER", label: "其他" },
];

export default function LeavePanel() {
  const [rows, setRows] = useState<LeaveRow[]>([]);
  const [employees, setEmployees] = useState<Emp[]>([]);
  const [busy, setBusy] = useState(false);

  const [range, setRange] = useState<any>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<{ empIds: string[]; range: any; type: string; reason: string }>({
    empIds: [],
    range: null,
    type: "REST",
    reason: "",
  });

  async function load() {
    try {
      setBusy(true);
      const qs =
        range && range[0] && range[1]
          ? `?from_date=${dayjs(range[0]).format("YYYY-MM-DD")}&to_date=${dayjs(range[1]).format("YYYY-MM-DD")}`
          : "";
      setRows(await apiCall(`${API}/leaves${qs}`));
    } catch (e: any) {
      message.error(e.message || "加载失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  useEffect(() => {
    apiCall(`${API}/employees`)
      .then((d: any) => setEmployees(Array.isArray(d) ? d : d?.items || []))
      .catch(() => {});
  }, []);

  async function doAdd() {
    if (!form.empIds.length) {
      message.warning("请选择员工");
      return;
    }
    if (!form.range || form.range.length !== 2) {
      message.warning("请选择休息日期区间");
      return;
    }
    try {
      setBusy(true);
      const r = await apiCall(`${API}/leaves`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          employee_ids: form.empIds,
          start_date: dayjs(form.range[0]).format("YYYY-MM-DD"),
          end_date: dayjs(form.range[1]).format("YYYY-MM-DD"),
          leave_type: form.type,
          reason: form.reason || null,
        }),
      });
      message.success(
        `已登记 ${r.created} 人：${r.names.join("、")}（${r.start_date} ~ ${r.end_date}）。重排后生效`
      );
      setOpen(false);
      setForm({ empIds: [], range: null, type: "REST", reason: "" });
      await load();
    } catch (e: any) {
      message.error(e.message || "登记失败");
    } finally {
      setBusy(false);
    }
  }

  async function doDelete(row: LeaveRow) {
    try {
      await apiCall(`${API}/leaves/${row.id}`, { method: "DELETE" });
      message.success("已删除");
      await load();
    } catch (e: any) {
      message.error(e.message || "删除失败");
    }
  }

  const activeEmps = employees.filter((e) => e.active !== false);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Alert
        type="info"
        showIcon={false}
        message={
          <span>
            在这里登记「特定某几天休息」的人员与日期，**重新排班后生效**（这些天只会是休息）。
            规则校验会拦截"指定休息日被排了班"的情况（`LEAVE_VIOLATION`）。
            若夜班人员休息过多导致排不出班，求解会给出诊断——可增加夜班权限人数或调小「工作日最低在岗的容差」。
          </span>
        }
      />

      <Card
        title="指定休息 / 请假"
        extra={
          <Space>
            <DatePicker.RangePicker
              style={{ width: 240 }}
              value={range}
              onChange={(v) => setRange(v)}
              placeholder={["筛选起始", "筛选结束"]}
            />
            <Button icon={<ReloadOutlined />} loading={busy} onClick={() => void load()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setForm({ empIds: [], range: null, type: "REST", reason: "" });
                setOpen(true);
              }}
            >
              新增指定休息
            </Button>
          </Space>
        }
      >
        <Table
          rowKey="id"
          size="small"
          bordered
          loading={busy}
          dataSource={rows}
          pagination={{ pageSize: 15, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          locale={{ emptyText: "暂无登记（点右上角「新增指定休息」）" }}
          columns={[
            {
              title: "员工",
              dataIndex: "employee",
              width: 130,
              render: (v: string, r: LeaveRow) =>
                r.active === false ? (
                  <span style={{ color: "#8b93a1" }}>
                    {v}
                    <Tag style={{ marginLeft: 4, fontSize: 10, lineHeight: "14px" }}>停用</Tag>
                  </span>
                ) : (
                  v
                ),
            },
            { title: "起始日期", dataIndex: "start_date", width: 120 },
            { title: "结束日期", dataIndex: "end_date", width: 120 },
            { title: "天数", dataIndex: "days", width: 70, align: "center" as any },
            {
              title: "类型",
              dataIndex: "leave_type_label",
              width: 130,
              render: (v: string, r: LeaveRow) => <Tag color={TYPE_COLOR[r.leave_type]}>{v}</Tag>,
            },
            { title: "备注", dataIndex: "reason", render: (v: string) => v || <span style={{ color: "#bbb" }}>-</span> },
            {
              title: "操作",
              key: "action",
              width: 90,
              render: (_: any, r: LeaveRow) => (
                <Popconfirm
                  title={`删除 ${r.employee} 的 ${r.start_date} ~ ${r.end_date}？`}
                  onConfirm={() => void doDelete(r)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title="新增指定休息"
        open={open}
        onOk={() => void doAdd()}
        onCancel={() => setOpen(false)}
        confirmLoading={busy}
        okText="登记"
        width={560}
      >
        <Form layout="vertical">
          <Form.Item label="员工（可多选）">
            <Select
              mode="multiple"
              allowClear
              style={{ width: "100%" }}
              placeholder="选择需要在特定日期休息的人员"
              value={form.empIds}
              onChange={(v) => setForm({ ...form, empIds: v })}
              optionFilterProp="label"
              options={activeEmps.map((e) => ({ value: e.id, label: e.name }))}
            />
          </Form.Item>
          <Form.Item label="休息日期（含首尾；单天就选同一天）">
            <DatePicker.RangePicker
              style={{ width: "100%" }}
              value={form.range}
              onChange={(v) => setForm({ ...form, range: v })}
            />
          </Form.Item>
          <Form.Item label="类型">
            <Select value={form.type} onChange={(v) => setForm({ ...form, type: v })} options={TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item label="备注（可选）">
            <Input
              value={form.reason}
              onChange={(e) => setForm({ ...form, reason: e.target.value })}
              placeholder="例如：家里有事 / 培训"
            />
          </Form.Item>
        </Form>
        <p style={{ color: "#6b7280", fontSize: 12, marginBottom: 0 }}>
          提示：登记后需到「排班」页重新点「自动排班」才会生效（重排前会自动存快照，可回滚）。
        </p>
      </Modal>
    </div>
  );
}
