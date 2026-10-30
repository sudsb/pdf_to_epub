# UI Visual Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一美化全部应用 UI 与静态手册，提高长文阅读性，并以轻量动效改善交互反馈。

**Architecture:** 保留现有内嵌 HTML/CSS 与原生 JS 架构，只调整样式及 `tabhost` Toast 的内联视觉属性，不引入依赖、不改 DOM 接口和业务逻辑。主校阅页修改后重新抽取 `ui_full.html`，保持运行页与测试镜像同步。

**Tech Stack:** Python 内嵌 HTML/CSS、原生 JavaScript、stdlib unittest。

**Spec:** 用户 2026-10-30 请求：分析并美化所有 UI，添加简单交互动效并提升可阅读性；用户选择直接开始。

## Global Constraints

- 不修改快捷键、Vim 路由、翻页锚点、稳定纠错序号或序列化正文 class。
- 不改变 `.page-row` / `.art-row` 的绝对定位模型，不改变 iframe 的 `visibility` 切换方式。
- 不动三处共享的题注与浮动图片 CSS 契约，不修改 EPUB 导出排版。
- 动效仅使用短暂 opacity/transform，并支持 `prefers-reduced-motion` 与 `body[data-no-anim="1"]`。
- 保留现有未提交改动；不提交 Git。

## Review Focus

- 虚拟列表行：样式不得覆盖定位、尺寸或滚动锚定；由现有 Vim/表格/弹窗测试检查。
- 动态显隐：新增动画不得覆盖 `display:none`、`hidden`、`open/active/show`；由弹窗与标签页测试检查。
- 暗色模式：主校阅新表面样式必须沿用变量，不能写死浅色正文。
- 键盘操作：不得添加键盘监听或移除焦点样式；由快捷键/Vim 测试检查。
- 镜像同步：`correctmanage._UI_HTML` 与 `ui_full.html` 必须一致；由镜像相关测试检查。

### Task 1: 主校阅与配置中心

**Files:**
- Modify: `correctmanage.py`（仅 `_UI_HTML` 样式）
- Modify: `guimanage.py`（仅 `_UI_HTML` 样式）
- Modify: `ui_full.html`（由 `extract_ui2.py` 同步）

- [x] 统一色彩、阴影、圆角、焦点与动效变量；增强正文行距、页面层级、菜单/模态框反馈。
- [x] 优化配置中心导航、卡片、表单、表格与状态区，避免悬停位移干扰阅读。
- [x] 同步主校阅镜像并运行相关测试。

### Task 2: EPUB、标签页与手册

**Files:**
- Modify: `ui/epubedit.html`
- Modify: `ui/tabhost.html`
- Modify: `使用手册.html`

- [x] 提升 EPUB 正文、目录和章节卡片的可读性，统一菜单/模态框动效。
- [x] 优化标签栏激活态、加载态与 Toast，保留 iframe 显隐机制。
- [x] 优化静态手册的长文排版、代码块、标题和窄屏/打印样式。

### Task 3: 验证

- [x] 运行 `node --check ui/app.js`、`node --check ui/epubedit.js`、`python check_js.py`。
- [ ] 运行 UI 相关 unittest 与最小冒烟测试；若有 jsdom 依赖则运行对应 `ptoe_*_test.cjs`。
- [ ] 检查 Git diff，确认没有业务逻辑、共享导出 CSS 或无关文件改动。
