/**
 * 排班周期（Dashboard 内，周期相关操作集中在这里）：
 *   1) 列出全部周期（含未发布）与状态，每行「去排班」直达
 *   2) 自动生成某年 12 个周期（26→25 模式：上月26日 ~ 本月25日），已存在/重叠自动跳过
 *   3) 手工新建单个周期（非常规区间用；后端会拒绝与现有周期重叠的区间）
 *   4) 自动生成时可同时联网同步该年法定节假日
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import {
  CalendarOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { apiCall } from "./api";

const API = "/api";

type PeriodRow = {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  status: string;
};

const STATUS_COLOR: Record<string, string> = {
  PUBLISHED: "green",
  SOLVED: "blue",
  VALIDATED: "cyan",
  DRAFT: "default",
  HISTORY: "purple",
  INFEASIBLE: "red",
  ERROR: "red",
};

export default function PeriodPanel({
  onChanged,
  onOpen,
}: {
  onChanged?: () => void;
  onOpen?: (periodId: string) => void;
}) {
  const thisYear = new Date().getFullYear();
  const [rows, setRows] = useState<PeriodRow[]>([]);
  const [busy, setBusy] = useState(false);

  const [open, setOpen] = useState(false);
  const [year, setYear] = useState<number>(thisYear + 1);
  const [syncHolidays, setSyncHolidays] = useState(true);
  const [preview, setPreview] = useState<any>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newRange, setNewRange] = useState<any>(null);

  async function load() {
    try {
      setBusy(true);
      setRows(await apiCall(`${API}/periods`));
    } catch (e: any) {
      message.error(e.message || "周期加载失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function doPreview() {
    try {
      setBusy(true);
      const r = await apiCall(
        `${API}/periods/auto-generate?year=${year}&sync_holidays=${syncHolidays}&dry_run=true`,
        { method: "POST" }
      );
      setPreview(r);
      if (r.created === 0) message.info(`${year} 年的周期已全部存在，无需新建`);
    } catch (e: any) {
      message.error(e.message || "预览失败");
    } finally {
      setBusy(false);
    }
  }

  async function doGenerate() {
    try {
      setBusy(true);
      const r = await apiCall(
        `${API}/periods/auto-generate?year=${year}&sync_holidays=${syncHolidays}&dry_run=false`,
        { method: "POST" }
      );
      message.success(`已生成 ${r.created} 个周期，跳过 ${r.skipped} 个`);
      const h = (r.holidays || [])[0];
      if (h?.ok === false) message.warning(`节假日同步：${h.message}`);
      setOpen(false);
      setPreview(null);
      await load();
      onChanged?.();
    } catch (e: any) {
      message.error(e.message || "生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function doCreate() {
    if (!newName.trim() || !newRange || newRange.length !== 2) {
      message.warning("请填写周期名称与起止日期");
      return;
    }
    try {
      setBusy(true);
      const start = dayjs(newRange[0]).format("YYYY-MM-DD");
      const end = dayjs(newRange[1]).format("YYYY-MM-DD");
      const p = await apiCall(
        `${API}/periods?start_date=${start}&end_date=${end}&name=${encodeURIComponent(newName.trim())}`,
        { method: "POST" }
      );
      message.success(`周期 ${p.name} 已创建`);
      setCreateOpen(false);
      setNewName("");
      setNewRange(null);
      await load();
      onChanged?.();
    } catch (e: any) {
      message.error(e.message || "创建失败");
    } finally {
      setBusy(false);
    }
  }

  const yearOptions = Array.from({ length: 7 }, (_, i) => thisYear - 1 + i).map((y) => ({
    value: y,
    label: `${y} 年`,
  }));

  return (
    <Card
      title="排班周期"
      style={{ marginTop: 16 }}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} loading={busy} onClick={() => void load()}>
            刷新
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建周期
          </Button>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            onClick={() => {
              setPreview(null);
              setOpen(true);
            }}
          >
            自动生成周期
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
        pagination={false}
        scroll={{ x: "max-content", y: 320 }}
        columns={[
          { title: "周期", dataIndex: "name", width: 100 },
          {
            title: "日期范围（26→25）",
            key: "range",
            width: 240,
            render: (_: any, r: PeriodRow) => (
              <span>
                {r.start_date} ~ {r.end_date}
              </span>
            ),
          },
          {
            title: "天数",
            key: "days",
            width: 70,
            align: "center" as any,
            render: (_: any, r: PeriodRow) =>
              Math.round(
                (new Date(r.end_date).getTime() - new Date(r.start_date).getTime()) / 86400000
              ) + 1,
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 110,
            render: (v: string) => <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag>,
          },
          {
            title: "操作",
            key: "action",
            width: 110,
            render: (_: any, r: PeriodRow) => (
              <Button size="small" type="primary" ghost onClick={() => onOpen?.(r.id)}>
                去排班
              </Button>
            ),
          },
        ]}
      />

      <Modal
        title="自动生成排班周期"
        open={open}
        onCancel={() => setOpen(false)}
        width={720}
        footer={[
          <Button key="preview" icon={<CalendarOutlined />} loading={busy} onClick={() => void doPreview()}>
            预览
          </Button>,
          <Button
            key="ok"
            type="primary"
            loading={busy}
            disabled={!preview || preview.created === 0}
            onClick={() => void doGenerate()}
          >
            确认生成 {preview?.created ? `（${preview.created} 个）` : ""}
          </Button>,
        ]}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space>
            <span>生成年份：</span>
            <Select style={{ width: 140 }} value={year} onChange={setYear} options={yearOptions} />
            <Checkbox checked={syncHolidays} onChange={(e) => setSyncHolidays(e.target.checked)}>
              同时联网同步该年法定节假日
            </Checkbox>
          </Space>

          <Alert
            type="info"
            showIcon={false}
            message={
              <span>
                按现有命名规则生成 12 个周期：<b>YYYY-MM = 上月 26 日 ~ 本月 25 日</b>（如 2027-01 = 2026-12-26 ~
                2027-01-25）。已存在的同名或日期重叠周期会自动跳过；生成后到「排班」页逐个自动排班。
              </span>
            }
          />

          {preview && (
            <div style={{ fontSize: 12 }}>
              <p style={{ marginBottom: 4 }}>
                将新建 <b>{preview.created}</b> 个，跳过 <b>{preview.skipped}</b> 个
              </p>
              {preview.periods?.length > 0 && (
                <div style={{ maxHeight: 200, overflow: "auto" }}>
                  {preview.periods.map((p: any) => (
                    <div key={p.name}>
                      ✅ {p.name}　{p.start_date} ~ {p.end_date}（{p.days} 天）
                    </div>
                  ))}
                </div>
              )}
              {preview.skipped_items?.length > 0 && (
                <div style={{ maxHeight: 140, overflow: "auto", marginTop: 6, color: "#8b93a1" }}>
                  {preview.skipped_items.map((p: any) => (
                    <div key={p.name}>
                      ⏭ {p.name}　{p.reason}
                    </div>
                  ))}
                </div>
              )}
              {preview.holidays?.length > 0 && (
                <div style={{ marginTop: 6, color: "#6b7280" }}>
                  节假日同步：{preview.holidays.map((h: any) => `${h.year} 年 ${h.message || "已同步"}`).join("；")}
                </div>
              )}
            </div>
          )}
        </Space>
      </Modal>

      <Modal
        title="新建排班周期（非常规区间）"
        open={createOpen}
        onOk={() => void doCreate()}
        onCancel={() => setCreateOpen(false)}
        confirmLoading={busy}
        okText="创建"
      >
        <Input
          placeholder="周期名称，如 2027-01"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          style={{ marginBottom: 12 }}
        />
        <DatePicker.RangePicker
          style={{ width: "100%" }}
          value={newRange}
          onChange={(v) => setNewRange(v)}
        />
        <p style={{ color: "#6b7280", marginTop: 8, marginBottom: 0, fontSize: 12 }}>
          常规周期请直接用「自动生成周期」：<b>YYYY-MM = 上月 26 日 ~ 本月 25 日</b>（示例 2026-10-26 ~
          2026-11-25）。这里只用于补非常规区间；<b>与现有周期日期重叠的区间会被拒绝</b>（重叠会导致跨月连续性与统计算错）。
        </p>
      </Modal>
    </Card>
  );
}
