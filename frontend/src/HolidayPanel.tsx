/**
 * 节假日管理（用户 2026-09-24 需求）：
 *   - 联网同步万年历：一键拉取某年法定节假日与调休安排（timor.tech → holiday-cn 多源回退）
 *   - 手工维护：按区间批量新增一个假期、逐日调整工资倍数、删除
 *   - 按国家规定套用倍数：元旦 1 / 春节 4 / 清明 1 / 劳动 2 / 端午 1 / 中秋 1 / 国庆 3 天记三倍，其余记双倍
 *
 * 说明：中国法定节假日与调休安排由国务院每年 11-12 月公告次年安排，
 *       未公布时同步会明确提示"尚未公布"，不会写入任何数据。
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
  Statistic,
  Table,
  Tag,
  message,
} from "antd";
import {
  CloudDownloadOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { apiCall } from "./api";

const API = "/api";

type HolidayRow = {
  id: string;
  date: string;
  name: string;
  day_type: string;
  pay_rate: number;
};

const WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"];
const RATE_LABEL: Record<number, string> = { 3: "3 倍（法定节假日）", 2: "2 倍（假日休息日）", 1: "1 倍（调休上班）" };
const RATE_COLOR: Record<number, string> = { 3: "red", 2: "orange", 1: "default" };

export default function HolidayPanel() {
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState<number>(thisYear);
  const [all, setAll] = useState<HolidayRow[]>([]);
  const [busy, setBusy] = useState(false);

  const [preview, setPreview] = useState<any>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [overwrite, setOverwrite] = useState(false);

  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState<{ range: any; name: string; day_type: string; pay_rate: number }>({
    range: null,
    name: "",
    day_type: "HOLIDAY",
    pay_rate: 3,
  });

  async function load() {
    try {
      setBusy(true);
      setAll(await apiCall(`${API}/holidays`));
    } catch (e: any) {
      message.error(e.message || "加载失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const configuredYears = Array.from(new Set(all.map((x) => Number(x.date.slice(0, 4)))));
  const yearOptions = Array.from(new Set([...configuredYears, thisYear, thisYear + 1, thisYear + 2]))
    .sort((a, b) => a - b)
    .map((y) => ({ value: y, label: `${y} 年${configuredYears.includes(y) ? "" : "（未配置）"}` }));

  const rows = all
    .filter((x) => Number(x.date.slice(0, 4)) === year)
    .sort((a, b) => a.date.localeCompare(b.date));

  const holidayDays = rows.filter((r) => r.day_type === "HOLIDAY");
  const triple = holidayDays.filter((r) => r.pay_rate === 3).length;
  const double = holidayDays.filter((r) => r.pay_rate === 2).length;
  const transfer = rows.filter((r) => r.day_type === "WORKDAY_TRANSFER").length;

  async function doPreview() {
    try {
      setBusy(true);
      const r = await apiCall(`${API}/holidays/sync/preview?year=${year}`);
      setPreview(r);
      setPreviewOpen(true);
      if (!r.ok) message.warning(r.message || "该年份数据尚未公布");
    } catch (e: any) {
      message.error(e.message || "联网同步失败（服务器可能无法访问外网）");
    } finally {
      setBusy(false);
    }
  }

  async function doSync() {
    try {
      setBusy(true);
      const r = await apiCall(`${API}/holidays/sync?year=${year}&overwrite=${overwrite}`, {
        method: "POST",
      });
      message.success(
        `同步完成（${r.source}）：新增 ${r.added} 条，更新 ${r.updated} 条，已一致 ${r.unchanged} 条`
      );
      setPreviewOpen(false);
      await load();
    } catch (e: any) {
      message.error(e.message || "同步失败");
    } finally {
      setBusy(false);
    }
  }

  async function doAutoRate() {
    try {
      setBusy(true);
      const r = await apiCall(`${API}/holidays/pay-rate/auto?year=${year}`, { method: "POST" });
      message.success(`已按国家规定套用倍数，修改 ${r.changed} 条`);
      await load();
    } catch (e: any) {
      message.error(e.message || "套用失败");
    } finally {
      setBusy(false);
    }
  }

  async function setRate(row: HolidayRow, pay_rate: number) {
    try {
      await apiCall(`${API}/holidays/${row.id}?pay_rate=${pay_rate}`, { method: "PUT" });
      setAll((prev) => prev.map((x) => (x.id === row.id ? { ...x, pay_rate } : x)));
      message.success(`${row.date} 已设为 ${RATE_LABEL[pay_rate]}`);
    } catch (e: any) {
      message.error(e.message || "设置失败");
    }
  }

  async function doDelete(row: HolidayRow) {
    try {
      await apiCall(`${API}/holidays/${row.id}`, { method: "DELETE" });
      setAll((prev) => prev.filter((x) => x.id !== row.id));
      message.success(`已删除 ${row.date}`);
    } catch (e: any) {
      message.error(e.message || "删除失败");
    }
  }

  async function doAdd() {
    if (!form.range || !form.range[0] || !form.range[1]) {
      message.warning("请选择日期区间");
      return;
    }
    if (!form.name.trim()) {
      message.warning("请填写假期名称，如 春节");
      return;
    }
    try {
      setBusy(true);
      const r = await apiCall(`${API}/holidays/bulk-add`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_date: form.range[0].format("YYYY-MM-DD"),
          end_date: form.range[1].format("YYYY-MM-DD"),
          name: form.name.trim(),
          day_type: form.day_type,
          pay_rate: form.pay_rate,
        }),
      });
      message.success(`已新增 ${r.added} 天`);
      setAddOpen(false);
      setForm({ range: null, name: "", day_type: "HOLIDAY", pay_rate: 3 });
      await load();
    } catch (e: any) {
      message.error(e.message || "新增失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Alert
        type="info"
        showIcon={false}
        message={
          <span>
            法定节假日与调休安排由国务院每年 11-12 月公告次年安排，可点<b>「联网同步」</b>一键获取；
            未配置年份排班会把法定假日当普通工作日、加班统计为 0（校验会给出预警）。
            同步源：timor.tech → holiday-cn（多源自动回退）。
          </span>
        }
      />

      <Card
        title="节假日与调休安排"
        extra={
          <Space>
            <Select
              style={{ width: 150 }}
              value={year}
              onChange={setYear}
              options={yearOptions}
            />
            <Button icon={<ReloadOutlined />} loading={busy} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Button type="primary" icon={<CloudDownloadOutlined />} loading={busy} onClick={() => void doPreview()}>
            联网同步 {year} 年
          </Button>
          <Button icon={<ThunderboltOutlined />} loading={busy} onClick={() => void doAutoRate()}>
            按国家规定套用倍数
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
            手工新增假期
          </Button>
        </Space>

        <Space size="large" style={{ marginBottom: 12 }} wrap>
          <Statistic title="法定假日" value={holidayDays.length} suffix="天" />
          <Statistic title="其中三倍工资日" value={triple} suffix="天" valueStyle={{ color: "#cf1322" }} />
          <Statistic title="其中双倍工资日" value={double} suffix="天" valueStyle={{ color: "#d46b08" }} />
          <Statistic title="调休上班" value={transfer} suffix="天" />
        </Space>

        <Table
          rowKey="id"
          size="small"
          bordered
          loading={busy}
          dataSource={rows}
          pagination={false}
          scroll={{ x: "max-content", y: "calc(100vh - 520px)" }}
          columns={[
            {
              title: "日期",
              dataIndex: "date",
              width: 160,
              render: (v: string) => `${v}（周${WEEKDAY[dayjs(v).day() === 0 ? 6 : dayjs(v).day() - 1]}）`,
            },
            { title: "名称", dataIndex: "name", width: 200 },
            {
              title: "类型",
              dataIndex: "day_type",
              width: 130,
              render: (v: string) =>
                v === "HOLIDAY" ? <Tag color="magenta">法定假日</Tag> : <Tag>调休上班</Tag>,
            },
            {
              title: "当前倍数",
              dataIndex: "pay_rate",
              width: 150,
              render: (v: number) => <Tag color={RATE_COLOR[v]}>{RATE_LABEL[v]}</Tag>,
            },
            {
              title: "设为",
              key: "set",
              width: 190,
              render: (_: any, r: HolidayRow) =>
                r.day_type === "HOLIDAY" ? (
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
                ) : (
                  <span style={{ color: "#8b93a1", fontSize: 12 }}>调休上班不参与加班统计</span>
                ),
            },
            {
              title: "操作",
              key: "action",
              width: 90,
              render: (_: any, r: HolidayRow) => (
                <Popconfirm title={`删除 ${r.date}？`} onConfirm={() => void doDelete(r)}>
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={`联网同步预览：${year} 年`}
        open={previewOpen}
        onCancel={() => setPreviewOpen(false)}
        onOk={() => void doSync()}
        okText="确认写入"
        okButtonProps={{ disabled: !preview?.ok, loading: busy }}
        width={720}
      >
        {preview && (
          <div>
            {preview.ok ? (
              <>
                <p>
                  数据源：<b>{preview.source}</b>　法定假日 <b>{preview.holiday_days}</b> 天
                  （三倍 <b>{preview.triple_days}</b> / 双倍 <b>{preview.double_days}</b>）　调休上班{" "}
                  <b>{preview.transfer_days}</b> 天
                </p>
                <p>
                  新增 <b>{preview.added}</b> 条，会更新 <b>{preview.updated}</b> 条，已一致{" "}
                  <b>{preview.unchanged}</b> 条
                </p>
                <Alert
                  type="warning"
                  showIcon={false}
                  style={{ marginBottom: 12 }}
                  message={
                    <span>
                      默认<b>只新增缺失日期</b>，不覆盖已有配置。
                      <label style={{ marginLeft: 8 }}>
                        <input
                          type="checkbox"
                          checked={overwrite}
                          onChange={(e) => setOverwrite(e.target.checked)}
                        />{" "}
                        同时用官方数据覆盖已存在日期的名称/类型/倍数
                      </label>
                    </span>
                  }
                />
                {preview.added_items?.length > 0 && (
                  <div style={{ maxHeight: 180, overflow: "auto", fontSize: 12 }}>
                    <b>将新增：</b>
                    {preview.added_items.map((x: any) => (
                      <div key={x.date}>
                        {x.date}　{x.name}　
                        {x.day_type === "HOLIDAY" ? `${x.pay_rate} 倍假日` : "调休上班"}
                      </div>
                    ))}
                  </div>
                )}
                {preview.updated_items?.length > 0 && (
                  <div style={{ maxHeight: 180, overflow: "auto", fontSize: 12, marginTop: 8 }}>
                    <b>将有差异（需要勾选覆盖才会改动）：</b>
                    {preview.updated_items.map((x: any) => (
                      <div key={x.date}>
                        {x.date}　{x.before?.name} → {x.name}　
                        {x.before?.day_type === "HOLIDAY" ? `${x.before?.pay_rate} 倍` : "调休"} →{" "}
                        {x.day_type === "HOLIDAY" ? `${x.pay_rate} 倍` : "调休"}
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <>
                <Alert type="warning" showIcon message={preview.message} />
                <ul style={{ fontSize: 12, color: "#6b7280" }}>
                  {(preview.sources || []).map((s: string, i: number) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </Modal>

      <Modal
        title="手工新增假期"
        open={addOpen}
        onOk={() => void doAdd()}
        onCancel={() => setAddOpen(false)}
        confirmLoading={busy}
        okText="新增"
      >
        <Form layout="vertical">
          <Form.Item label="日期区间（含首尾，一次录一个假期）">
            <DatePicker.RangePicker
              style={{ width: "100%" }}
              value={form.range}
              onChange={(v) => setForm({ ...form, range: v })}
            />
          </Form.Item>
          <Form.Item label="名称（如 春节、国庆节、春节调休上班）">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Form.Item>
          <Form.Item label="类型">
            <Select
              value={form.day_type}
              onChange={(v) => setForm({ ...form, day_type: v })}
              options={[
                { value: "HOLIDAY", label: "法定假日（排 C / Y 班，参与加班统计）" },
                { value: "WORKDAY_TRANSFER", label: "调休上班（按普通工作日排班）" },
              ]}
            />
          </Form.Item>
          {form.day_type === "HOLIDAY" && (
            <Form.Item label="工资倍数">
              <Select
                value={form.pay_rate}
                onChange={(v) => setForm({ ...form, pay_rate: v })}
                options={[
                  { value: 3, label: "3 倍（法定节假日当天）" },
                  { value: 2, label: "2 倍（假期内其余休息日）" },
                ]}
              />
            </Form.Item>
          )}
        </Form>
        <p style={{ color: "#6b7280", fontSize: 12, marginBottom: 0 }}>
          提示：已存在的日期会自动跳过；新增后点「按国家规定套用倍数」可按假期名称自动区分三倍/双倍。
        </p>
      </Modal>
    </div>
  );
}
