import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./style.css";

/**
 * 企业门户浅色主题：品牌蓝顶栏 + 白卡片 + 浅灰底（antd 默认算法 + token 微调）。
 * 视觉细节见 style.css；此处只统一色板、圆角、控件高度与表格密度。
 */
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: "#1668dc",
          colorInfo: "#1668dc",
          colorLink: "#1668dc",
          colorSuccess: "#18a058",
          colorWarning: "#e8901a",
          colorError: "#d9363e",
          colorBgLayout: "#f5f7fa",
          colorBorder: "#d9dee7",
          colorBorderSecondary: "#e9edf3",
          colorText: "#1f2937",
          colorTextSecondary: "#6b7280",
          borderRadius: 6,
          fontSize: 13,
          controlHeight: 32,
          fontFamily:
            "'Segoe UI', 'Microsoft YaHei', 'PingFang SC', system-ui, -apple-system, sans-serif",
          wireframe: false,
        },
        components: {
          Layout: {
            headerBg: "#1668dc",
            headerHeight: 48,
            headerPadding: "0 16px",
            bodyBg: "#f5f7fa",
            footerBg: "#ffffff",
            footerPadding: "10px 20px",
          },
          Card: {
            headerFontSize: 15,
            paddingLG: 16,
          },
          Table: {
            headerBg: "#fafbfc",
            headerColor: "#475569",
            headerSplitColor: "#eef1f6",
            borderColor: "#f0f2f5",
            rowHoverBg: "#f0f7ff",
            cellPaddingBlockSM: 6,
            cellPaddingInlineSM: 8,
            headerBorderRadius: 6,
          },
          Menu: {
            darkItemBg: "transparent",
            darkSubMenuItemBg: "transparent",
            darkItemColor: "rgba(255,255,255,0.88)",
            darkItemHoverColor: "#ffffff",
            darkItemHoverBg: "rgba(255,255,255,0.14)",
            darkItemSelectedColor: "#ffffff",
            darkItemSelectedBg: "transparent",
            horizontalItemSelectedColor: "#ffffff",
            itemBorderRadius: 6,
            itemMarginInline: 4,
          },
          Button: {
            primaryShadow: "0 1px 2px rgba(22,104,220,0.20)",
          },
          Modal: {
            titleFontSize: 15,
          },
          Tag: {
            defaultBg: "#f3f5f9",
            defaultColor: "#4b5563",
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
