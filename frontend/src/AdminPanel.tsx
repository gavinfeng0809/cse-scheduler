/**
 * 用户管理（管理员）：新增/编辑用户与管理员、重置密码、停用启用。
 *
 * - 支持"从员工添加"：选一名在职员工，自动用其邮箱作登录名、姓名作显示名；
 * - 角色：管理员（可管理用户）/ 排班员 / 只读；
 * - 安全护栏在后端：不能停用自己、不能失去最后一名在用管理员、密码至少 8 位。
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import {
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  StopOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { apiCall } from "./api";

const API = "/api";

type UserRow = {
  id: string;
  username: string;
  display_name: string;
  role: string;
  role_label: string;
  active: boolean;
  created_at?: string | null;
};

type EmployeeRow = {
  id: string;
  name: string;
  email?: string | null;
  employee_no?: string | null;
  active?: boolean;
};

const ROLE_COLOR: Record<string, string> = {
  admin: "magenta",
  planner: "cyan",
  viewer: "default",
};

export default function AdminPanel() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [roles, setRoles] = useState<{ value: string; label: string }[]>([]);
  const [employees, setEmployees] = useState<EmployeeRow[]>([]);
  const [me, setMe] = useState<{ username: string; role: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [pwdUser, setPwdUser] = useState<UserRow | null>(null);
  const [pwdValue, setPwdValue] = useState("");
  const [form] = Form.useForm();
  const [createMode, setCreateMode] = useState<"employee" | "manual">("employee");

  async function load() {
    setLoading(true);
    try {
      const [u, r, e, who] = await Promise.all([
        apiCall(`${API}/users`),
        apiCall(`${API}/users/roles`),
        apiCall(`${API}/employees`),
        apiCall(`${API}/auth/me`),
      ]);
      setUsers(u || []);
      setRoles(r || []);
      setEmployees((e || []).filter((x: EmployeeRow) => x.active !== false));
      setMe(who || null);
    } catch (err: any) {
      message.error(err.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const isAdmin = (me?.role || "").toLowerCase() === "admin";

  async function submitCreate() {
    try {
      const values = await form.validateFields();
      const payload: any = {
        password: values.password,
        role: values.role,
        display_name: values.display_name,
      };

      if (createMode === "employee") {
        payload.employee_id = values.employee_id;
      } else {
        payload.username = values.username;
      }

      const created = await apiCall(`${API}/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      message.success(`已创建 ${created.display_name}（${created.role_label}）`);
      setCreateOpen(false);
      form.resetFields();
      void load();
    } catch (err: any) {
      if (err?.errorFields) return; // 表单校验失败
      message.error(err.message || "创建失败");
    }
  }

  async function toggleActive(row: UserRow, active: boolean) {
    try {
      await apiCall(`${API}/users/${row.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active }),
      });
      message.success(active ? `已启用 ${row.username}` : `已停用 ${row.username}`);
      void load();
    } catch (err: any) {
      message.error(err.message || "操作失败");
    }
  }

  async function changeRole(row: UserRow, role: string) {
    try {
      await apiCall(`${API}/users/${row.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role }),
      });
      message.success(`${row.username} 角色已改为 ${role}`);
      void load();
    } catch (err: any) {
      message.error(err.message || "操作失败");
    }
  }

  async function submitPassword() {
    if (!pwdUser) return;
    if (pwdValue.length < 8) {
      message.warning("密码至少 8 位");
      return;
    }
    try {
      await apiCall(`${API}/users/${pwdUser.id}/password`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: pwdValue }),
      });
      message.success(`${pwdUser.username} 的密码已重置`);
      setPwdUser(null);
      setPwdValue("");
    } catch (err: any) {
      message.error(err.message || "重置失败");
    }
  }

  const columns = [
    {
      title: "登录名",
      dataIndex: "username",
      render: (v: string) => (
        <Space>
          <TeamOutlined style={{ color: "#22d3ee" }} />
          <span>{v}</span>
        </Space>
      ),
    },
    { title: "显示名", dataIndex: "display_name" },
    {
      title: "角色",
      dataIndex: "role",
      width: 190,
      render: (v: string, row: UserRow) => (
        <Select
          size="small"
          value={v}
          style={{ width: 150 }}
          disabled={!isAdmin}
          onChange={(nv) => changeRole(row, nv)}
          options={roles.map((r) => ({ value: r.value, label: r.label }))}
        />
      ),
    },
    {
      title: "状态",
      dataIndex: "active",
      width: 130,
      render: (v: boolean, row: UserRow) => (
        <Space>
          <Switch
            size="small"
            checked={v}
            disabled={!isAdmin || row.username === me?.username}
            onChange={(nv) => toggleActive(row, nv)}
          />
          {v ? <Tag color="green">在用</Tag> : <Tag>已停用</Tag>}
        </Space>
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 170,
      render: (v: string | null) =>
        v ? String(v).slice(0, 16).replace("T", " ") : "-",
    },
    {
      title: "操作",
      width: 210,
      render: (_: any, row: UserRow) => (
        <Space>
          <Button size="small" icon={<KeyOutlined />} onClick={() => setPwdUser(row)}>
            重置密码
          </Button>
          <Popconfirm
            title={`停用 ${row.username}？`}
            description="停用后该账号无法登录，审计记录保留。"
            okText="停用"
            cancelText="取消"
            disabled={!isAdmin || row.username === me?.username}
            onConfirm={() => toggleActive(row, false)}
          >
            <Button
              size="small"
              danger
              icon={<StopOutlined />}
              disabled={!isAdmin || !row.active || row.username === me?.username}
            >
              停用
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Alert
        type={isAdmin ? "info" : "warning"}
        showIcon
        icon={<SafetyCertificateOutlined />}
        style={{ marginBottom: 12 }}
        message={
          isAdmin
            ? "用户管理：新增管理员/用户、调整角色、重置密码、停用账号。"
            : "当前账号不是管理员，只能查看，无法修改。"
        }
        description="角色：管理员（可管理用户）/ 排班员 / 只读。系统会阻止停用最后一名在用管理员、以及停用自己。"
      />

      <Card
        title={
          <span>
            <span className="cse-dot" />
            账号列表（{users.filter((u) => u.active).length} 个在用 / 共 {users.length} 个）
          </span>
        }
        loading={loading}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!isAdmin}
              onClick={() => {
                form.resetFields();
                setCreateMode("employee");
                setCreateOpen(true);
              }}
            >
              新增管理员/用户
            </Button>
          </Space>
        }
      >
        <Table rowKey="id" size="small" dataSource={users} columns={columns as any} pagination={false} />
      </Card>

      <Modal
        title="新增管理员 / 用户"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={submitCreate}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Radio.Group
          value={createMode}
          onChange={(e) => setCreateMode(e.target.value)}
          style={{ marginBottom: 12 }}
        >
          <Radio.Button value="employee">从员工添加</Radio.Button>
          <Radio.Button value="manual">手动输入登录名</Radio.Button>
        </Radio.Group>

        <Form form={form} layout="vertical" initialValues={{ role: "admin" }}>
          {createMode === "employee" ? (
            <Form.Item
              name="employee_id"
              label="选择员工（登录名取其邮箱，显示名取其姓名）"
              rules={[{ required: true, message: "请选择员工" }]}
            >
              <Select
                showSearch
                optionFilterProp="label"
                placeholder="输入姓名/邮箱搜索"
                options={employees.map((e) => ({
                  value: e.id,
                  label: `${e.name}｜${e.email || e.employee_no || ""}`,
                }))}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="username"
              label="登录名（3~64 位字母/数字/._@-）"
              rules={[
                { required: true, message: "请输入登录名" },
                { pattern: /^[A-Za-z0-9._@-]{3,64}$/, message: "格式不正确" },
              ]}
            >
              <Input placeholder="例如 zhangsan 或 zhangsan@corp.com" />
            </Form.Item>
          )}

          <Form.Item name="display_name" label="显示名（可选）">
            <Input placeholder="例如 张三（排班管理员）" />
          </Form.Item>

          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select
              options={(roles.length
                ? roles
                : [
                    { value: "admin", label: "管理员" },
                    { value: "planner", label: "排班员" },
                    { value: "viewer", label: "只读" },
                  ]
              ).map((r) => ({ value: r.value, label: r.label }))}
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="初始密码（至少 8 位）"
            rules={[
              { required: true, message: "请输入初始密码" },
              { min: 8, message: "密码至少 8 位" },
            ]}
          >
            <Input.Password placeholder="创建后请让本人尽快修改" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={pwdUser ? `重置密码：${pwdUser.display_name}（${pwdUser.username}）` : "重置密码"}
        open={!!pwdUser}
        onCancel={() => {
          setPwdUser(null);
          setPwdValue("");
        }}
        onOk={submitPassword}
        okText="确认重置"
        cancelText="取消"
      >
        <Input.Password
          placeholder="新密码（至少 8 位）"
          value={pwdValue}
          onChange={(e) => setPwdValue(e.target.value)}
        />
        <div style={{ fontSize: 12, color: "#8fb2d6", marginTop: 8 }}>
          重置后请通过安全渠道告知本人，并让其登录后自行修改。
        </div>
      </Modal>
    </div>
  );
}
