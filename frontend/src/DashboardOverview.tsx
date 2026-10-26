/**
 * Dashboard 概览（数据来自 GET /dashboard/overview，一次请求四块）：
 *   ① 待办与异常提醒：待发布 / 状态异常 / 节假日未配置年份 / 下一周期缺口 / 夜班策略未确认
 *   ② 最新周期概览：工作日日均在岗、人均班次、夜班与假日班次、总工时、手改与锁定格
 *   ③ 年度节假日加班概览：三倍/双倍倍数总和、三倍占比、加班最多的前 3 人
 *   ④ 最近操作记录：审计日志最近 8 条
 */
import { useEffect, useState } from "react";
import { Alert, Button, Card, Col, List, Row, Space, Statistic, Table, Tag, message } from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import { apiCall } from "./api";

const API = "/api";

type Todo = {
  level: string;
  title: string;
  detail: string;
  action: string;
  period_id?: string;
};

type Overview = {
  todos: Todo[];
  latest_period: any;
  holiday_overtime: any;
  recent_actions: { time: string; action: string; label: string; detail: string }[];
};

const LEVEL_ICON: Record<string, any> = {
  error: <ExclamationCircleOutlined style={{ color: "#d9363e" }} />,
  warning: <ClockCircleOutlined style={{ color: "#e8901a" }} />,
  info: <InfoCircleOutlined style={{ color: "#1668dc" }} />,
};

const ACTION_TEXT: Record<string, string> = {
  schedule: "去排班",
  holidays: "去节假日管理",
  rules: "去规则配置",
  periods: "自动生成周期",
};

export default function DashboardOverview({
  onGoto,
}: {
  onGoto?: (action: string, periodId?: string) => void;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      setBusy(true);
      setData(await apiCall(`${API}/dashboard/overview`));
    } catch (e: any) {
      message.error(e.message || "概览加载失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const todos = data?.todos || [];
  const lp = data?.latest_period;
  const ho = data?.holiday_overtime;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* ① 待办与异常 */}
      <Card
        title={
          <Space>
            <span>待办与异常</span>
            {todos.length > 0 ? (
              <Tag color="orange">{todos.length} 项</Tag>
            ) : (
              <Tag color="green" icon={<CheckCircleOutlined />}>
                暂无待办
              </Tag>
            )}
          </Space>
        }
        extra={
          <Button size="small" loading={busy} onClick={() => void load()}>
            刷新
          </Button>
        }
      >
        {todos.length === 0 ? (
          <span style={{ color: "#6b7280" }}>
            当前没有需要注意的事项：周期状态正常、节假日配置齐全、夜班策略已确认。
          </span>
        ) : (
          <List
            size="small"
            dataSource={todos}
            renderItem={(t: Todo) => (
              <List.Item
                actions={
                  t.action === "periods"
                    ? []
                    : [
                        <Button
                          key="go"
                          size="small"
                          type="link"
                          onClick={() => onGoto?.(t.action, t.period_id)}
                        >
                          {ACTION_TEXT[t.action] || "去处理"}
                        </Button>,
                      ]
                }
              >
                <List.Item.Meta
                  avatar={LEVEL_ICON[t.level] || LEVEL_ICON.info}
                  title={t.title}
                  description={<span style={{ fontSize: 12 }}>{t.detail}</span>}
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      {/* ② 最新周期概览 */}
      {lp && (
        <Card
          title={`最新周期概览：${lp.name}（${lp.start_date} ~ ${lp.end_date}）`}
          extra={
            <Space>
              <Tag color={lp.status === "PUBLISHED" ? "green" : "blue"}>{lp.status}</Tag>
              <Button size="small" type="link" onClick={() => onGoto?.("schedule", lp.id)}>
                去排班
              </Button>
            </Space>
          }
        >
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="工作日日均在岗" value={lp.workday_avg} suffix={" / 最低 " + lp.workday_min} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="总班次" value={lp.total_shifts} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic
                title="每人班次"
                value={lp.shifts_min === lp.shifts_max ? lp.shifts_max : `${lp.shifts_min}~${lp.shifts_max}`}
              />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="夜班" value={lp.night_shifts} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="假日班" value={lp.holiday_shifts} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="总工时" value={lp.total_hours} suffix="h" />
            </Col>
          </Row>
          <div style={{ marginTop: 12, fontSize: 12, color: "#6b7280" }}>
            排班人数 {lp.employees_count}　周末班 {lp.weekend_shifts}　人工修改格 {lp.manual_cells}　锁定格{" "}
            {lp.locked_cells}
          </div>
        </Card>
      )}

      {/* ③ 年度节假日加班概览 */}
      {ho && (
        <Card
          title={`${ho.year} 年节假日加班概览`}
          extra={
            <Button size="small" type="link" onClick={() => onGoto?.("stats")}>
              去统计分析
            </Button>
          }
        >
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="三倍倍数总和" value={ho.triple_weighted} valueStyle={{ color: "#cf1322" }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="双倍倍数总和" value={ho.double_weighted} valueStyle={{ color: "#d46b08" }} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="倍数总和" value={ho.weighted_total} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="三倍占比" value={ho.triple_ratio} suffix="%" />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="三倍/双倍天数" value={ho.triple_days} suffix={" / " + ho.double_days + " 天"} />
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Statistic title="有加班记录人数" value={ho.people} suffix="人" />
            </Col>
          </Row>
          {ho.top?.length > 0 && (
            <div style={{ marginTop: 12, fontSize: 13 }}>
              <span style={{ color: "#6b7280" }}>加班最多：</span>
              {ho.top.map((t: any, i: number) => (
                <Tag key={t.name} color={i === 0 ? "red" : "orange"} style={{ marginLeft: 6 }}>
                  {t.name} {t.weighted_total}（{t.shifts} 班）
                </Tag>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* ④ 最近操作 */}
      <Card title="最近操作">
        <Table
          rowKey={(r: any, i?: number) => `${r.time}-${r.label}-${i}`}
          size="small"
          bordered
          loading={busy && !data}
          dataSource={data?.recent_actions || []}
          pagination={false}
          scroll={{ x: "max-content" }}
          columns={[
            { title: "时间", dataIndex: "time", width: 120 },
            { title: "操作", dataIndex: "label", width: 160, render: (v: string) => <Tag>{v}</Tag> },
            { title: "对象 / 详情", dataIndex: "detail" },
          ]}
        />
      </Card>
    </div>
  );
}
