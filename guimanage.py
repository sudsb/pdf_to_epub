"""HTML 配置操作界面：本地 HTTP 服务 + 浏览器 UI（后端部分）。

镜像 correctmanage.py 的服务端模式：ThreadingHTTPServer + 内嵌 HTML 界面，
浏览器关闭（pagehide 信标 / 心跳失联）超过 idle_timeout 秒后自动退出。

UI 文件由另一条工作流单独产出，集成阶段由编排者把真实 UI 内容替换
_UI_HTML 占位符；本模块只负责后端端点与 CLI 接线，不自行编写整个 UI。

端点一览：
  GET  /                → 返回 _UI_HTML（text/html）
  GET  /api/config      → 读取配置 + 模型文件存在性 + 默认值 + 配置路径
  GET  /api/status      → 引擎 / 服务探测 / 端口 / 启动中状态
  GET  /api/ping        → 页面心跳（刷新 last_beat）
  GET  /api/convert/status → 转换进度快照（运行中/完成/日志/结果/待答弹窗询问）
  GET  /api/correct/status → 矫正进度快照（运行中/完成/日志）
  GET  /api/tools/merge/status → 合并进度快照（运行中/完成/日志/结果）
  POST /api/config      → 校验并原子写配置
  POST /api/server/start→ 后台线程启动推理服务（llamamanage.runserver）
  POST /api/server/stop → 停止推理服务
  POST /api/pick        → 文件/目录选择对话框（tkinter 在主线程弹出；multiple 支持多选）
  POST /api/bye         → 页面关闭信标（置 gone_at）
  POST /api/convert/start → 子进程启动完整 PDF→EPUB 转换（流式日志）
  POST /api/convert/prompt → 回答子进程的弹窗询问（OCR 断点续传选择，写回 stdin）
  POST /api/convert/stop  → 停止正在运行的转换
  POST /api/correct/start → 子进程启动矫正界面（流式日志）
  POST /api/correct/stop  → 停止正在运行的矫正
  POST /api/tools/merge/start → 后台线程合并多 EPUB（epubmergemanage.merge_epubs）
  POST /api/tools/merge/stop  → 请求停止合并（当前章节完成后才会停止）

全程中文错误信息，响应 shape 统一 {ok: bool, ...}。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 占位符：集成阶段由编排者替换成真实 UI 文件内容（不得删除该占位行）
_UI_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PToEA 配置界面</title>
<style>
:root{--accent:#2f6fed;--accent-light:#eef3fb;--accent-hover:#2256c2;--border:#d8dee6;--bg:#f4f6f9;--card:#fff;--text:#1c2733;--text-sec:#5a6b7c;--text-dim:#8a97a6;--green:#1a7f37;--green-bg:#e6f4ea;--red:#c0392b;--red-bg:#fdecea;--yellow:#b8860b;--yellow-bg:#fef8e1;--radius:8px;--radius-sm:5px;--shadow:0 1px 3px rgba(0,0,0,.08);--shadow-lg:0 4px 20px rgba(0,0,0,.12);--sidebar-w:220px;--header-h:54px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{height:100%}
body{height:100%;font-family:"Microsoft YaHei",system-ui,-apple-system,sans-serif;font-size:14px;color:var(--text);background:var(--bg);line-height:1.6;overflow:hidden}
.header{position:fixed;top:0;left:0;right:0;height:var(--header-h);display:flex;align-items:center;gap:14px;padding:0 20px;background:var(--card);border-bottom:1px solid var(--border);z-index:50}
.header .logo{font-size:16px;font-weight:700;color:var(--accent);white-space:nowrap}
.header .logo span{color:var(--text);font-weight:400;margin-left:6px;font-size:13px}
.header .h-spacer{flex:1}
.header .config-path{font-size:12px;color:var(--text-dim);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#saveBtn{padding:6px 18px;font-size:13px;font-weight:600;background:var(--accent);color:#fff;border:none;border-radius:var(--radius-sm);cursor:pointer;transition:background .15s}
#saveBtn:hover{background:var(--accent-hover)}
#saveBtn:disabled{opacity:.5;cursor:not-allowed}
#saveBtn.saving::after{content:'';display:inline-block;width:12px;height:12px;margin-left:8px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px}
@keyframes spin{to{transform:rotate(360deg)}}
.btn-outline{padding:5px 14px;font-size:13px;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;transition:all .15s}
.btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.btn-outline:disabled{opacity:.5;cursor:not-allowed}
.btn-small{padding:3px 10px;font-size:12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--card);cursor:pointer;color:var(--text)}
.btn-small:hover{border-color:var(--accent);color:var(--accent)}
.btn-add{display:inline-flex;align-items:center;gap:4px;padding:4px 12px;font-size:12px;background:var(--green-bg);color:var(--green);border:1px solid #b7e1c7;border-radius:var(--radius-sm);cursor:pointer;font-weight:600}
.btn-add:hover{background:#c8ebd5}
.layout{display:flex;height:calc(100vh - var(--header-h));margin-top:var(--header-h)}
.sidebar{width:var(--sidebar-w);flex-shrink:0;background:var(--card);border-right:1px solid var(--border);overflow-y:auto;padding:10px 0}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 18px;font-size:13px;color:var(--text-sec);cursor:pointer;transition:all .12s;border-left:3px solid transparent;user-select:none}
.nav-item:hover{background:var(--accent-light);color:var(--text)}
.nav-item.active{background:var(--accent-light);color:var(--accent);font-weight:600;border-left-color:var(--accent)}
.nav-item .nav-icon{font-size:15px;width:20px;text-align:center;flex-shrink:0}
.nav-item .nav-badge{margin-left:auto;font-size:11px;padding:1px 6px;border-radius:10px;font-weight:600}
.nav-item .nav-badge.running{background:var(--green-bg);color:var(--green)}
.nav-item .nav-badge.stopped{background:#f0f0f0;color:var(--text-dim)}
.content{flex:1;overflow-y:auto;padding:24px 32px 40px}
.content::-webkit-scrollbar{width:6px}
.content::-webkit-scrollbar-track{background:transparent}
.content::-webkit-scrollbar-thumb{background:#c8cdd4;border-radius:3px}
.page{display:none;max-width:800px}
.page.active{display:block}
.page-title{font-size:20px;font-weight:700;margin-bottom:6px}
.page-desc{font-size:13px;color:var(--text-sec);margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:14px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.card-title .ct-icon{color:var(--accent)}
.form-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.form-row:last-child{margin-bottom:0}
.form-label{width:130px;flex-shrink:0;font-size:13px;color:var(--text-sec);text-align:right}
.form-ctrl{flex:1;min-width:0}
.form-ctrl input[type="text"],.form-ctrl input[type="number"],.form-ctrl textarea,.form-ctrl select{width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;color:var(--text);background:var(--card);transition:border-color .15s}
.form-ctrl input:focus,.form-ctrl textarea:focus,.form-ctrl select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(47,111,237,.12)}
.form-ctrl textarea{resize:vertical;min-height:80px;line-height:1.6}
.form-ctrl .pick-row{display:flex;gap:6px;align-items:center}
.form-ctrl .pick-row input{flex:1}
.form-hint{font-size:11px;color:var(--text-dim);margin-top:4px}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
.status-item{background:var(--bg);border-radius:var(--radius-sm);padding:12px 14px;display:flex;flex-direction:column;gap:4px}
.status-item .si-label{font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px}
.status-item .si-value{font-size:15px;font-weight:600}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;line-height:1.6}
.badge-green{background:var(--green-bg);color:var(--green)}
.badge-red{background:var(--red-bg);color:var(--red)}
.badge-yellow{background:var(--yellow-bg);color:var(--yellow)}
.badge-gray{background:#f0f0f0;color:var(--text-dim)}
.action-row{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.log-box{background:#1a1e24;color:#c8d3da;border-radius:var(--radius-sm);padding:12px 14px;font-family:"Cascadia Code","Consolas","Courier New","Microsoft YaHei",monospace;font-size:12px;line-height:1.7;max-height:200px;overflow-y:auto;margin-top:12px;white-space:pre-wrap;word-break:break-all}
.log-box .log-ok{color:#4ade80}.log-box .log-err{color:#f87171}.log-box .log-warn{color:#fbbf24}.log-box .log-info{color:#60a5fa}
.dyn-table{width:100%;border-collapse:collapse;font-size:13px}
.dyn-table th{text-align:left;font-weight:600;font-size:12px;color:var(--text-dim);padding:6px 8px;border-bottom:2px solid var(--border)}
.dyn-table td{padding:5px 6px;border-bottom:1px solid #eef0f3;vertical-align:middle}
.dyn-table tr:hover td{background:#fafbfc}
.dyn-table input[type="text"]{width:100%;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font:inherit;font-size:12px}
.dyn-table input[type="text"]:focus{outline:none;border-color:var(--accent)}
.dyn-table .del-cell{width:36px;text-align:center}
.dyn-table .del-btn{width:24px;height:24px;border:none;background:transparent;color:var(--text-dim);cursor:pointer;border-radius:4px;font-size:15px;display:inline-flex;align-items:center;justify-content:center}
.dyn-table .del-btn:hover{background:var(--red-bg);color:var(--red)}
.dyn-table .file-ok{color:var(--green);font-weight:600}
.dyn-table .file-miss{color:var(--red);font-weight:600}
.engine-switch{display:inline-flex;background:var(--bg);border-radius:6px;padding:2px;border:1px solid var(--border);margin-bottom:14px}
.engine-switch button{padding:5px 16px;border:none;border-radius:4px;background:transparent;cursor:pointer;font:inherit;font-size:13px;color:var(--text-sec);transition:all .15s}
.engine-switch button.active{background:var(--accent);color:#fff;font-weight:600}
.engine-switch button:hover:not(.active){color:var(--text)}
.pr-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 20px}
.pr-grid .form-row{margin-bottom:0}
.pr-grid .form-label{width:110px}
.pr-grid .form-ctrl input[type="checkbox"]{width:16px;height:16px;accent-color:var(--accent)}
.rule-card{padding:10px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px;display:flex;align-items:center;gap:10px;background:var(--card)}
.rule-card:hover{border-color:var(--accent)}
.rule-name{font-weight:600;min-width:80px}
.rule-cond{flex:1;font-size:12px;color:var(--text-sec)}
#toast{position:fixed;top:64px;left:50%;transform:translateX(-50%);z-index:999;display:flex;flex-direction:column;gap:6px;align-items:center;pointer-events:none}
.toast-item{padding:8px 18px;border-radius:8px;font-size:13px;color:#fff;line-height:1.5;box-shadow:var(--shadow-lg);opacity:0;transform:translateY(-8px);transition:opacity .25s,transform .25s}
.toast-item.show{opacity:1;transform:translateY(0)}
.toast-item.t-ok{background:var(--green)}.toast-item.t-fail{background:var(--red)}.toast-item.t-warn{background:var(--yellow)}
.empty-state{text-align:center;padding:24px;color:var(--text-dim);font-size:13px}
.menu-toggle{display:none;background:none;border:none;font-size:20px;cursor:pointer;color:var(--text);padding:4px}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:35}
@media(max-width:768px){:root{--sidebar-w:0px}.sidebar{position:fixed;top:var(--header-h);left:0;bottom:0;width:220px;z-index:40;transform:translateX(-100%);transition:transform .2s}.sidebar.open{transform:translateX(0);box-shadow:var(--shadow-lg)}.sidebar.open+.sidebar-overlay{display:block}.menu-toggle{display:block}.content{padding:18px 16px 30px}.form-row{flex-direction:column;align-items:flex-start;gap:4px}.form-label{width:auto;text-align:left}.pr-grid{grid-template-columns:1fr}}
/* 转换页专用 */
.btn-start{padding:8px 24px;font-size:14px;font-weight:600;background:var(--accent);color:#fff;border:none;border-radius:var(--radius-sm);cursor:pointer;transition:background .15s}
.btn-start:hover{background:var(--accent-hover)}
.btn-start:disabled{opacity:.5;cursor:not-allowed}
.btn-start.running::after{content:'';display:inline-block;width:12px;height:12px;margin-left:8px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px}
.btn-stop-convert{padding:8px 24px;font-size:14px;font-weight:600;background:var(--card);color:var(--red);border:1px solid var(--red);border-radius:var(--radius-sm);cursor:pointer;transition:all .15s}
.btn-stop-convert:hover{background:var(--red-bg)}
.btn-stop-convert:disabled{opacity:.5;cursor:not-allowed}
.convert-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 20px}
.convert-grid .form-row{margin-bottom:0}
.convert-grid .form-label{width:90px}
#convertLog{background:#1a1e24;color:#c8d3da;border-radius:var(--radius-sm);padding:12px 14px;font-family:"Cascadia Code","Consolas","Courier New","Microsoft YaHei",monospace;font-size:12px;line-height:1.7;max-height:300px;min-height:80px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
/* 工具页专用 */
.merge-file-list{border:1px solid var(--border);border-radius:var(--radius-sm);max-height:240px;overflow-y:auto;background:var(--card);margin-bottom:12px}
.merge-file-row{display:flex;align-items:center;gap:6px;padding:6px 10px;border-bottom:1px solid #eef0f3;font-size:13px;font-family:"Cascadia Code","Consolas","Courier New","Microsoft YaHei",monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.merge-file-row:last-child{border-bottom:none}
.mfi{color:var(--text-dim);flex-shrink:0}
.mfn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:default}
.mops{display:flex;gap:2px;flex-shrink:0}
.mops .btn-small{padding:2px 6px;font-size:12px}
.mops .del-btn{width:auto;padding:0 4px;font-size:14px;border:none;background:transparent;color:var(--text-dim);cursor:pointer;border-radius:4px;display:inline-flex;align-items:center;justify-content:center}
.mops .del-btn:hover{background:var(--red-bg);color:var(--red)}
@media(max-width:768px){.convert-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header class="header">
  <button class="menu-toggle" onclick="toggleSidebar()" aria-label="菜单">☰</button>
  <div class="logo">PToEA <span>配置中心</span></div>
  <div class="h-spacer"></div>
  <div class="config-path" id="configPath" title="配置文件路径">加载中...</div>
  <button id="saveBtn" onclick="saveConfig()">保存配置</button>
</header>
<div class="layout">
  <nav class="sidebar" id="sidebar">
    <div class="nav-item" data-page="convert" onclick="switchPage('convert')"><span class="nav-icon">▶</span> 转换</div>
    <div class="nav-item active" data-page="status" onclick="switchPage('status')"><span class="nav-icon">◉</span> 服务状态<span class="nav-badge stopped" id="navStatusBadge">--</span></div>
    <div class="nav-item" data-page="basic" onclick="switchPage('basic')"><span class="nav-icon">⚙</span> 基础设置</div>
    <div class="nav-item" data-page="models" onclick="switchPage('models')"><span class="nav-icon">★</span> 模型管理</div>
    <div class="nav-item" data-page="llama" onclick="switchPage('llama')"><span class="nav-icon">▶</span> llama 参数</div>
    <div class="nav-item" data-page="vllm" onclick="switchPage('vllm')"><span class="nav-icon">▶</span> vLLM 参数</div>
    <div class="nav-item" data-page="proofread" onclick="switchPage('proofread')"><span class="nav-icon">✎</span> 校对参数</div>
    <div class="nav-item" data-page="shortcuts" onclick="switchPage('shortcuts')"><span class="nav-icon">⌨</span> 快捷键</div>
    <div class="nav-item" data-page="rules" onclick="switchPage('rules')"><span class="nav-icon">&#9776;</span> 格式规则</div>
    <div class="nav-item" data-page="tools" onclick="switchPage('tools')"><span class="nav-icon">&#9872;</span> 工具</div>
  </nav>
  <div class="sidebar-overlay" onclick="toggleSidebar()"></div>
  <main class="content" id="contentArea">
    <!-- 1. 服务状态 -->
    <div class="page active" id="page-status">
      <h2 class="page-title">服务状态</h2>
      <p class="page-desc">查看和控制 OCR 模型服务的运行状态。</p>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9673;</span> 当前引擎</div>
        <div class="engine-switch" id="engineSwitch">
          <button class="active" data-eng="llama" onclick="setEngine('llama')">llama.cpp</button>
          <button data-eng="vllm" onclick="setEngine('vllm')">vLLM-Omni</button>
        </div>
        <div id="engineHint" style="font-size:12px;color:var(--text-dim);margin-bottom:10px;"></div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9673;</span> 服务信息</div>
        <div class="status-grid">
          <div class="status-item"><span class="si-label">运行状态</span><span class="si-value" id="stRunning"><span class="badge badge-gray">未知</span></span></div>
          <div class="status-item"><span class="si-label">当前模型</span><span class="si-value" id="stModel">--</span></div>
          <div class="status-item"><span class="si-label">端口</span><span class="si-value" id="stPort">--</span></div>
          <div class="status-item"><span class="si-label">引擎</span><span class="si-value" id="stEngine">--</span></div>
        </div>
        <div class="action-row">
          <button class="btn-outline" id="btnStart" onclick="serverStart()">启动服务</button>
          <button class="btn-outline" id="btnStop" onclick="serverStop()">停止服务</button>
          <button class="btn-outline" onclick="refreshStatus()">刷新状态</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> 操作日志</div>
        <div class="log-box" id="logBox">等待操作...</div>
      </div>
    </div>
    <!-- 2. 基础设置 -->
    <div class="page" id="page-basic">
      <h2 class="page-title">基础设置</h2>
      <p class="page-desc">配置核心路径、OCR 模型和引擎选项。</p>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9881;</span> 文件路径</div>
        <div class="form-row"><span class="form-label">llama-server 路径</span><div class="form-ctrl"><div class="pick-row"><input type="text" id="cfgLlamaServer" placeholder="llama-server.exe 路径"><button class="btn-small" onclick="pickFile('cfgLlamaServer')">选择文件</button></div></div></div>
        <div class="form-row"><span class="form-label">模型目录</span><div class="form-ctrl"><div class="pick-row"><input type="text" id="cfgModelsDir" placeholder="模型文件所在目录"><button class="btn-small" onclick="pickDir('cfgModelsDir')">选择目录</button></div></div></div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9733;</span> 模型与引擎</div>
        <div class="form-row"><span class="form-label">当前模型</span><div class="form-ctrl"><select id="cfgSelectedModel"></select></div></div>
        <div class="form-row"><span class="form-label">推理引擎</span><div class="form-ctrl" style="display:flex;gap:16px;align-items:center;"><label style="display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="radio" name="cfgEngine" value="llama" checked> llama.cpp</label><label style="display:flex;align-items:center;gap:4px;cursor:pointer;"><input type="radio" name="cfgEngine" value="vllm"> vLLM-Omni</label></div></div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> OCR 提示词</div>
        <div class="form-row" style="flex-direction:column;align-items:stretch;"><div class="form-ctrl"><textarea id="cfgOcrPrompt" rows="3" placeholder="输入 OCR 提示词..."></textarea></div><div class="form-hint">发送给模型的 OCR 指令。末尾会自动追加「按原文原格式输出」。</div></div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">🖋</span> 字体设置</div>
        <div class="form-row"><span class="form-label">正文字体</span><div class="form-ctrl"><input type="text" id="cfgFontBody" placeholder="body font"></div></div>
        <div class="form-row"><span class="form-label">标题字体</span><div class="form-ctrl"><input type="text" id="cfgFontHeading" placeholder="heading font"></div></div>
        <div class="form-row"><span class="form-label">注释字体</span><div class="form-ctrl"><input type="text" id="cfgFontNote" placeholder="note font"></div></div>
        <div class="form-row"><span class="form-label">引用字体</span><div class="form-ctrl"><input type="text" id="cfgFontCitation" placeholder="citation font"></div></div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">📷</span> 图片预处理</div>
        <div class="form-row"><span class="form-label">启用预处理</span><div class="form-ctrl"><input type="checkbox" id="cfgImgPreEnabled"></div></div>
        <div class="form-row"><span class="form-label">灰度</span><div class="form-ctrl"><input type="checkbox" id="cfgImgGray"></div></div>
        <div class="form-row"><span class="form-label">去噪</span><div class="form-ctrl"><input type="checkbox" id="cfgImgDenoise"></div></div>
        <div class="form-row"><span class="form-label">锐化</span><div class="form-ctrl"><input type="checkbox" id="cfgImgSharpen"></div></div>
        <div class="form-row"><span class="form-label">自适应二值化</span><div class="form-ctrl"><input type="checkbox" id="cfgImgBinarize"></div></div>
        <div class="form-row"><span class="form-label">预处理 workers</span><div class="form-ctrl"><input type="number" id="cfgImgWorkers" min="0" step="1"></div></div>
      </div>
    </div>
    <!-- 3. 模型管理 -->
    <div class="page" id="page-models">
      <h2 class="page-title">模型管理</h2>
      <p class="page-desc">管理已注册的 OCR 模型列表。每个模型需指定 GGUF 主文件和 mmproj 投影文件。</p>
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;"><div class="card-title" style="margin-bottom:0;"><span class="ct-icon">&#9733;</span> 模型列表</div><button class="btn-add" onclick="addModel()">+ 添加模型</button></div>
        <table class="dyn-table"><thead><tr><th style="width:80px;">键名</th><th>主模型文件 (name)</th><th>投影文件 (mmproj)</th><th style="width:64px;">并发</th><th style="width:50px;">主文件</th><th style="width:50px;">投影文件</th><th class="del-cell"></th></tr></thead><tbody id="modelTbody"></tbody></table>
        <div class="form-hint" style="margin-top:6px;">并发 = 该模型的推荐 OCR 并发数（不填则默认 3）。转换页与 CLI 未显式指定并发时按此值运行；显存充足可调大，大模型（如 BF16）建议 2-3 避免 KV 缓存溢出变慢。</div>
      </div>
    </div>
    <!-- 4. llama 启动参数 -->
    <div class="page" id="page-llama">
      <h2 class="page-title">llama 启动参数</h2>
      <p class="page-desc">llama-server 的启动参数键值对。键名对应命令行 --键名（自动转 kebab-case）。</p>
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;"><div class="card-title" style="margin-bottom:0;"><span class="ct-icon">&#9654;</span> 参数列表</div><button class="btn-add" onclick="addArg('llamaArgs')">+ 添加参数</button></div>
        <table class="dyn-table"><thead><tr><th style="width:200px;">参数名</th><th>值</th><th class="del-cell"></th></tr></thead><tbody id="llamaArgs"></tbody></table>
      </div>
    </div>
    <!-- 5. vLLM 启动参数 -->
    <div class="page" id="page-vllm">
      <h2 class="page-title">vLLM 启动参数</h2>
      <p class="page-desc">vLLM-Omni 服务的可执行文件路径和启动参数。</p>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9881;</span> vLLM 可执行文件</div>
        <div class="form-row"><span class="form-label">vllm_server 路径</span><div class="form-ctrl"><div class="pick-row"><input type="text" id="cfgVllmServer" placeholder="vllm 可执行文件路径（留空=仅连接模式）"><button class="btn-small" onclick="pickFile('cfgVllmServer')">选择文件</button></div><div class="form-hint">留空表示不启动本地进程，仅连接远程 vLLM 服务。</div></div></div>
      </div>
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;"><div class="card-title" style="margin-bottom:0;"><span class="ct-icon">&#9654;</span> 参数列表</div><button class="btn-add" onclick="addArg('vllmArgs')">+ 添加参数</button></div>
        <table class="dyn-table"><thead><tr><th style="width:220px;">参数名</th><th>值</th><th class="del-cell"></th></tr></thead><tbody id="vllmArgs"></tbody></table>
      </div>
    </div>
    <!-- 6. 校对参数 -->
    <div class="page" id="page-proofread">
      <h2 class="page-title">校对参数</h2>
      <p class="page-desc">文字校对引擎的参数配置，包括规则开关和 LLM 校对选项。</p>
      <div class="card"><div class="card-title"><span class="ct-icon">&#9998;</span> 基本参数</div><div class="pr-grid" id="proofreadGrid"></div></div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> LLM 校对</div>
        <div class="pr-grid">
          <div class="form-row"><span class="form-label">启用 LLM 校对</span><div class="form-ctrl"><input type="checkbox" id="prEnableLlm"></div></div>
          <div class="form-row"><span class="form-label">校对模型</span><div class="form-ctrl"><select id="prLlmModel"></select></div></div>
          <div class="form-row"><span class="form-label">LLM 超时（秒）</span><div class="form-ctrl"><input type="number" id="prLlmTimeout" min="1" max="300"></div></div>
          <div class="form-row"><span class="form-label">启用旧规则</span><div class="form-ctrl"><input type="checkbox" id="prLegacyRules"></div></div>
        </div>
      </div>
    </div>
    <!-- 7. 快捷键 -->
    <div class="page" id="page-shortcuts">
      <h2 class="page-title">快捷键</h2>
      <p class="page-desc">矫正界面操作快捷键绑定。键值对格式：操作名 → 按键组合。</p>
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;"><div class="card-title" style="margin-bottom:0;"><span class="ct-icon">&#8984;</span> 快捷键列表</div><button class="btn-add" onclick="addArg('shortcuts')">+ 添加快捷键</button></div>
        <table class="dyn-table"><thead><tr><th style="width:220px;">操作名</th><th>按键组合</th><th class="del-cell"></th></tr></thead><tbody id="shortcuts"></tbody></table>
      </div>
    </div>
    <!-- 8. 格式规则 -->
    <div class="page" id="page-rules">
      <h2 class="page-title">格式规则</h2>
      <p class="page-desc">矫正界面的自动格式规则列表（仅展示，编辑请在矫正界面操作）。</p>
      <div class="card"><div class="card-title"><span class="ct-icon">&#9776;</span> 已有规则</div><div id="rulesList"></div></div>
    </div>
    <!-- 9. 转换 -->
    <div class="page" id="page-convert">
      <h2 class="page-title">PDF → EPUB 转换</h2>
      <p class="page-desc">选择 PDF 文件并设置参数，一键启动完整的转换流程。</p>
      <!-- 矫正界面 -->
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> 矫正界面</div>
        <p class="page-desc" style="margin:0 0 12px;">启动矫正界面，在浏览器中手动校对 OCR 结果后生成 EPUB。</p>
        <div class="action-row">
          <button class="btn-start" id="crStartBtn" onclick="startCorrect()">启动矫正</button>
          <button class="btn-stop-convert" id="crStopBtn" onclick="stopCorrect()" style="display:none;">停止</button>
        </div>
        <pre id="correctLog" style="margin-top:10px;">等待矫正任务...</pre>
      </div>
      <!-- 源文件 -->
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#128196;</span> 源文件</div>
        <div class="form-row">
          <span class="form-label">PDF 路径</span>
          <div class="form-ctrl">
            <div class="pick-row">
              <input type="text" id="cvtPdf" placeholder="选择要转换的 PDF 文件..." readonly>
              <button class="btn-small" onclick="pickPdf()">选择文件…</button>
            </div>
            <div class="form-hint" id="cvtPdfHint"></div>
          </div>
        </div>
      </div>
<!-- 转换参数 -->
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9881;</span> 转换参数</div>
        <div class="convert-grid">
          <div class="form-row"><span class="form-label">DPI</span><div class="form-ctrl"><select id="cvtDpi"><option value="0" selected>0 = 100</option><option value="1">1 = 150</option><option value="2">2 = 200</option><option value="3">3 = 300</option><option value="4">4 = 600</option></select></div></div>
          <div class="form-row"><span class="form-label">模型</span><div class="form-ctrl"><select id="cvtModel" onchange="onCvtModelChange()"></select></div></div>
          <div class="form-row"><span class="form-label">引擎</span><div class="form-ctrl"><select id="cvtEngine"><option value="">跟随配置</option><option value="llama">llama.cpp</option><option value="vllm">vLLM-Omni</option></select></div></div>
          <div class="form-row"><span class="form-label">并发数</span><div class="form-ctrl"><input type="number" id="cvtWorkers" value="5" min="1" max="16"></div></div>
          <div class="form-row"><span class="form-label">超时（秒）</span><div class="form-ctrl"><input type="number" id="cvtTimeout" value="600" min="30" max="7200"></div></div>
          <div class="form-row"><span class="form-label">思考模式</span><div class="form-ctrl" style="display:flex;align-items:center;gap:6px;"><input type="checkbox" id="cvtThinking" style="width:16px;height:16px;accent-color:var(--accent)"><span class="form-hint" style="margin:0;">开启后显著变慢</span></div></div>
          <div class="form-row"><span class="form-label">标题</span><div class="form-ctrl"><input type="text" id="cvtTitle" placeholder="可留空（自动提取）"></div></div>
          <div class="form-row"><span class="form-label">作者</span><div class="form-ctrl"><input type="text" id="cvtAuthor" placeholder="可留空"></div></div>
          <div class="form-row"><span class="form-label">语言</span><div class="form-ctrl"><input type="text" id="cvtLang" value="zh-CN"></div></div>
          <div class="form-row"><span class="form-label">输出目录</span><div class="form-ctrl"><div class="pick-row"><input type="text" id="cvtOutDir" placeholder="留空则使用默认目录"><button class="btn-small" onclick="pickOutDir()">选择目录&#8230;</button></div><div class="form-hint">留空时输出到 data/<pdf名>/</div></div></div>
          <div class="form-row"><span class="form-label">排除页</span><div class="form-ctrl"><input type="text" id="cvtExclude" placeholder="如 1-15,17,20（可选）"><div class="form-hint">跳过对指定序号图片的识别，多个用逗号分隔，支持区间。</div></div></div>
        </div>
      </div>
      </div>
      <!-- 操作 -->
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9654;</span> 操作</div>
        <div class="action-row">
          <button class="btn-start" id="cvtStartBtn" onclick="startConvert()">开始转换</button>
          <button class="btn-stop-convert" id="cvtStopBtn" onclick="stopConvert()" style="display:none;">停止</button>
        </div>
      </div>
      <!-- 运行日志 -->
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> 运行日志</div>
        <pre id="convertLog">等待转换任务...</pre>
      </div>
    </div>
    <!-- 10. 工具 -->
    <div class="page" id="page-tools">
      <h2 class="page-title">工具</h2>
      <p class="page-desc">辅助工具。当前可用：多 EPUB 合并。</p>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#128196;</span> 多 EPUB 合并</div>
        <p class="page-desc" style="margin:0 0 12px;">将多个 EPUB 按顺序合并为一个，合并顺序 = 列表顺序。</p>
        <div class="form-row">
          <span class="form-label">EPUB 文件</span>
          <div class="form-ctrl">
            <div class="pick-row">
              <button class="btn-small" onclick="pickMergeFiles()">添加文件</button>
              <span class="form-hint" id="mergeFileCount">共 0 个文件</span>
            </div>
          </div>
        </div>
        <div id="mergeFileList" class="merge-file-list"><div class="empty-state">未添加任何文件，点击「添加文件」选择 EPUB。</div></div>
        <div class="form-row"><span class="form-label">书名</span><div class="form-ctrl"><input type="text" id="mergeTitle" placeholder="可留空（自动使用第一个文件的标题）"></div></div>
        <div class="form-row"><span class="form-label">作者</span><div class="form-ctrl"><input type="text" id="mergeAuthor" placeholder="可选"></div></div>
        <div class="form-row"><span class="form-label">语言</span><div class="form-ctrl"><input type="text" id="mergeLang" value="zh-CN"></div></div>
        <div class="form-row"><span class="form-label">输出路径</span><div class="form-ctrl"><input type="text" id="mergeOutPath" placeholder="留空则保存到第一个文件同目录"></div></div>
        <div class="form-hint">输出路径留空时，默认保存到第一个 EPUB 所在目录。</div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9654;</span> 操作</div>
        <div class="action-row">
          <button class="btn-start" id="mergeStartBtn" onclick="startMerge()">开始合并</button>
          <button class="btn-stop-convert" id="mergeStopBtn" onclick="stopMerge()" style="display:none;">停止</button>
        </div>
      </div>
      <div class="card">
        <div class="card-title"><span class="ct-icon">&#9998;</span> 运行日志</div>
        <pre id="mergeLog" class="log-box">等待合并任务...</pre>
      </div>
    </div>
  </main>
</div>
<!-- 转换弹窗询问（OCR 断点续传等需要用户决策时显示；选择写回子进程） -->
<div id="convertPromptBg" style="display:none;position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.4);align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:10px;padding:22px 26px;max-width:440px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,.3);">
    <div style="font-weight:600;font-size:15px;margin-bottom:10px;">转换需要选择</div>
    <div id="convertPromptQuestion" style="margin-bottom:16px;color:#333;line-height:1.6;font-size:13px;"></div>
    <div id="convertPromptBtns" style="display:flex;flex-direction:column;gap:8px;"></div>
  </div>
</div>
<div id="toast"></div>
<script>
/* PToEA Config UI - Client Logic */
var cfg={},models=[],statusInfo=[],logLines=[],_lastShownError=null;
function apiGet(u){return fetch(u).then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}).catch(function(e){toast("请求失败: "+e.message,"fail");return null})}
function apiPost(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:b!==undefined?JSON.stringify(b):undefined}).then(function(r){return r.json()}).catch(function(e){toast("请求失败: "+e.message,"fail");return null})}
function fetchConfig(){return apiGet("/api/config").then(function(res){if(!res||!res.ok){toast("加载配置失败","fail");return}cfg=res.config||{};models=res.models||[];if(res.path){var el=document.getElementById("configPath");el.textContent=res.path;el.title=res.path}renderAll()})}
function fetchStatus(){return apiGet("/api/status").then(function(res){if(!res||!res.ok)return;statusInfo=res;renderStatus(res)})}
function toast(msg,type,duration){type=type||"ok";duration=duration||(type==="fail"?5000:3000);var c=document.getElementById("toast"),el=document.createElement("div");el.className="toast-item t-"+type;el.textContent=msg;c.appendChild(el);requestAnimationFrame(function(){el.classList.add("show")});setTimeout(function(){el.classList.remove("show");setTimeout(function(){el.remove()},300)},duration)}
function addLog(text,cls){logLines.push({text:text,cls:cls||"log-info"});if(logLines.length>100)logLines.shift();renderLog()}
function renderLog(){var box=document.getElementById("logBox"),now=new Date(),ts=pad2(now.getHours())+":"+pad2(now.getMinutes())+":"+pad2(now.getSeconds());box.innerHTML=logLines.map(function(l){return '<span class="'+l.cls+'">['+ts+']</span> '+escH(l.text)}).join("\n");box.scrollTop=box.scrollHeight}
function pad2(n){return n<10?"0"+n:""+n}
function escH(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function switchPage(name){document.querySelectorAll(".nav-item").forEach(function(el){el.classList.toggle("active",el.dataset.page===name)});document.querySelectorAll(".page").forEach(function(el){el.classList.toggle("active",el.id==="page-"+name)});document.getElementById("sidebar").classList.remove("open")}
function toggleSidebar(){document.getElementById("sidebar").classList.toggle("open")}
function setEngine(eng){cfg.engine=eng;document.querySelectorAll(".engine-switch button").forEach(function(b){b.classList.toggle("active",b.dataset.eng===eng)});updateEngineHint()}
function updateEngineHint(){var eng=cfg.engine||"llama",port=eng==="llama"?(cfg.llama_server_args||{}).port||"8080":(cfg.vllm_server_args||{}).port||"8000";document.getElementById("engineHint").textContent="当前引擎: "+eng+" | 默认端口: "+port}
function renderStatus(s){var badgeEl=document.getElementById("stRunning"),navBadge=document.getElementById("navStatusBadge"),label,cls;if(s.probe==="match"){label="运行中";cls="badge-green";navBadge.textContent="运行中";navBadge.className="nav-badge running"}else if(s.probe==="mismatch"){label="模型不匹配";cls="badge-yellow";navBadge.textContent="异常";navBadge.className="nav-badge stopped"}else{label="未运行";cls=s.busy?"badge-yellow":"badge-gray";navBadge.textContent=s.busy?"启动中":"未运行";navBadge.className="nav-badge "+(s.busy?"running":"stopped")}badgeEl.innerHTML='<span class="badge '+cls+'">'+label+"</span>";if(s.busy&&s.probe==="none")badgeEl.innerHTML+=' <span style="font-size:11px;color:var(--yellow);margin-left:6px;">启动中...</span>';document.getElementById("stModel").textContent=s.model_name||s.model_key||"--";document.getElementById("stPort").textContent=s.port||"--";document.getElementById("stEngine").textContent=s.engine||cfg.engine||"llama";if(s.engine){document.querySelectorAll(".engine-switch button").forEach(function(b){b.classList.toggle("active",b.dataset.eng===s.engine)});cfg.engine=s.engine}updateEngineHint();document.getElementById("btnStart").disabled=s.probe==="match"||s.busy;document.getElementById("btnStop").disabled=s.probe==="none"&&!s.busy;if(s.last_error&&s.last_error!==_lastShownError){_lastShownError=s.last_error;addLog("服务错误: "+s.last_error,"log-err")}}
function renderAll(){renderBasic();renderModels();renderArgs("llamaArgs",cfg.llama_server_args);renderArgs("vllmArgs",cfg.vllm_server_args);renderProofread();renderShortcuts();renderRules();renderConvert();document.querySelectorAll(".engine-switch button").forEach(function(b){b.classList.toggle("active",b.dataset.eng===(cfg.engine||"llama"))});updateEngineHint()}
function renderBasic(){document.getElementById("cfgLlamaServer").value=cfg.llama_server||"";document.getElementById("cfgModelsDir").value=cfg.models_dir||"";document.getElementById("cfgOcrPrompt").value=cfg.ocr_prompt||"";var sel=document.getElementById("cfgSelectedModel");sel.innerHTML="";models.forEach(function(m){var o=document.createElement("option");o.value=m.key;o.textContent=m.key+" - "+m.name;sel.appendChild(o)});sel.value=cfg.selected_model||"";document.querySelectorAll('input[name="cfgEngine"]').forEach(function(r){r.checked=r.value===(cfg.engine||"llama")});try{document.getElementById("cfgFontBody").value=(cfg.fonts&&cfg.fonts.body)||"";document.getElementById("cfgFontHeading").value=(cfg.fonts&&cfg.fonts.heading)||"";document.getElementById("cfgFontNote").value=(cfg.fonts&&cfg.fonts.note)||"";document.getElementById("cfgFontCitation").value=(cfg.fonts&&cfg.fonts.citation)||""}catch(e){}try{var ip=cfg.image_preprocess||{};document.getElementById("cfgImgPreEnabled").checked=!!ip.enabled;document.getElementById("cfgImgGray").checked=!!ip.gray;document.getElementById("cfgImgDenoise").checked=!!ip.denoise;document.getElementById("cfgImgSharpen").checked=!!ip.sharpen;document.getElementById("cfgImgBinarize").checked=!!ip.binarize;document.getElementById("cfgImgWorkers").value=(ip.workers!=null?ip.workers:"")}catch(e){}
 }
function renderModels(){var tbody=document.getElementById("modelTbody");tbody.innerHTML="";var keys=Object.keys(cfg.model_choices||{});if(!keys.length){tbody.innerHTML='<tr><td colspan="7" class="empty-state">暂无模型，点击上方「添加模型」</td></tr>';return}keys.forEach(function(key){var m=cfg.model_choices[key],info=models.find(function(x){return x.key===key}),nameOk=info?info.name_exists:false,mmOk=info?info.mmproj_exists:false,tr=document.createElement("tr");tr.innerHTML='<td><input type="text" value="'+escH(key)+'" data-field="key" style="font-weight:600;background:#f8f9fb;"></td><td><input type="text" value="'+escH(m.name||"")+'" data-field="name"></td><td><input type="text" value="'+escH(m.mmproj||"")+'" data-field="mmproj"></td><td><input type="number" min="1" max="64" value="'+(m.workers!=null?m.workers:"")+'" data-field="workers" style="width:52px;"></td><td class="'+(nameOk?"file-ok":"file-miss")+'">'+(nameOk?"\u2713":"\u2717")+'</td><td class="'+(mmOk?"file-ok":"file-miss")+'">'+(mmOk?"\u2713":"\u2717")+'</td><td class="del-cell"><button class="del-btn" title="删除模型">\u2715</button></td>';tr.querySelector(".del-btn").onclick=function(){if(confirm("确定删除模型「"+key+"」？")){delete cfg.model_choices[key];models=models.filter(function(x){return x.key!==key});renderModels();toast("已删除模型 "+key,"ok")}};tbody.appendChild(tr)})}
function addModel(){var nk="NEW",i=1;while(cfg.model_choices[nk])nk="NEW"+(i++);cfg.model_choices[nk]={name:"",mmproj:""};models.push({key:nk,name:"",mmproj:"",name_exists:false,mmproj_exists:false});renderModels();toast("已添加空模型行，请填写后保存","warn")}
function renderArgs(tid,obj){var tbody=document.getElementById(tid);tbody.innerHTML="";obj=obj||{};var keys=Object.keys(obj);if(!keys.length){tbody.innerHTML='<tr><td colspan="3" class="empty-state">暂无参数，点击上方添加</td></tr>';return}keys.forEach(function(k){var tr=document.createElement("tr");tr.innerHTML='<td><input type="text" value="'+escH(k)+'" data-field="key" style="font-weight:500;"></td><td><input type="text" value="'+escH(String(obj[k]!=null?obj[k]:""))+'" data-field="value"></td><td class="del-cell"><button class="del-btn" title="删除">\u2715</button></td>';tr.querySelector(".del-btn").onclick=function(){tr.remove()};tbody.appendChild(tr)})}
function addArg(tid){var tbody=document.getElementById(tid),empty=tbody.querySelector(".empty-state");if(empty)tbody.innerHTML="";var tr=document.createElement("tr");tr.innerHTML='<td><input type="text" value="" placeholder="参数名"></td><td><input type="text" value="" placeholder="值"></td><td class="del-cell"><button class="del-btn" title="删除">\u2715</button></td>';tr.querySelector(".del-btn").onclick=function(){tr.remove()};tbody.appendChild(tr)}
function renderProofread(){var pr=cfg.proofread||{},grid=document.getElementById("proofreadGrid");grid.innerHTML="";[{k:"similarity_min",l:"相似度阈值",s:0.01},{k:"score_min",l:"最低评分",s:0.01},{k:"max_cand_cache",l:"候选缓存上限",s:100},{k:"max_replacement_combinations",l:"替换组合上限",s:1},{k:"auto_fix_score",l:"自动修正阈值",s:0.01}].forEach(function(f){var d=document.createElement("div");d.className="form-row";d.innerHTML='<span class="form-label">'+f.l+'</span><div class="form-ctrl"><input type="number" data-pr="'+f.k+'" value="'+(pr[f.k]!=null?pr[f.k]:"")+'" step="'+f.s+'"></div>';grid.appendChild(d)});document.getElementById("prEnableLlm").checked=!!pr.enable_llm;document.getElementById("prLegacyRules").checked=!!pr.enable_legacy_rules;document.getElementById("prLlmTimeout").value=pr.llm_timeout!=null?pr.llm_timeout:"";var sel=document.getElementById("prLlmModel");sel.innerHTML='<option value="">（自动使用当前模型）</option>';models.forEach(function(m){var o=document.createElement("option");o.value=m.key;o.textContent=m.key+" - "+m.name;sel.appendChild(o)});sel.value=pr.llm_model||""}
function renderShortcuts(){renderArgs("shortcuts",cfg.shortcuts)}
function renderRules(){var list=document.getElementById("rulesList"),rules=cfg.format_rules||[];if(!rules.length){list.innerHTML='<div class="empty-state">暂无格式规则</div>';return}list.innerHTML="";rules.forEach(function(r,idx){var card=document.createElement("div");card.className="rule-card";var ct="";if(r.conditions&&r.conditions.length){ct=r.conditions.map(function(c){var p=[];if(c.pattern)p.push(c.type==="regex"?"正则「"+c.pattern+"」":"包含「"+c.pattern+"」");else p.push("无条件");p.push("作用域: "+(c.scope==="page"?"当前页":c.scope==="paragraph"?"段落":"选中"));if(c.formats&&c.formats.length)p.push("\u2192 "+c.formats.join(", "));return p.join(" | ")}).join("；")}else if(r.condition){ct=r.condition.pattern?(r.condition.type||"contains")+"「"+r.condition.pattern+"」":"无条件"}var btn=document.createElement("button");btn.className="btn-small";btn.textContent="删除";btn.title="删除规则";btn.onclick=function(){if(confirm("确定删除此格式规则？")){cfg.format_rules.splice(idx,1);renderRules();toast("已删除格式规则","ok")}};card.innerHTML='<span class="rule-name">'+escH(r.name||"未命名")+'</span><span class="rule-cond">'+escH(ct||"无条件")+'</span>';card.appendChild(btn);list.appendChild(card)})}
function collectConfig(){cfg.llama_server=document.getElementById("cfgLlamaServer").value.trim();cfg.models_dir=document.getElementById("cfgModelsDir").value.trim();cfg.ocr_prompt=document.getElementById("cfgOcrPrompt").value;cfg.selected_model=document.getElementById("cfgSelectedModel").value;var er=document.querySelector('input[name="cfgEngine"]:checked');if(er)cfg.engine=er.value;var nm={};document.getElementById("modelTbody").querySelectorAll("tr").forEach(function(tr){var ins=tr.querySelectorAll("input");if(ins.length<2)return;var key=ins[0].value.trim();if(key){var m={name:ins[1].value.trim(),mmproj:ins.length>=3?ins[2].value.trim():""};var w=ins.length>=4?parseInt(ins[3].value,10):0;if(!isNaN(w)&&w>=1&&w<=64)m.workers=w;nm[key]=m}});cfg.model_choices=nm;cfg.llama_server_args=collectKVT("llamaArgs");cfg.vllm_server_args=collectKVT("vllmArgs");cfg.vllm_server=document.getElementById("cfgVllmServer").value.trim();var pr={};document.querySelectorAll("[data-pr]").forEach(function(el){var k=el.dataset.pr,v=el.value.trim();if(el.type==="number"&&v!=="")pr[k]=parseFloat(v);else pr[k]=v});pr.enable_llm=document.getElementById("prEnableLlm").checked;pr.enable_legacy_rules=document.getElementById("prLegacyRules").checked;var lm=document.getElementById("prLlmModel").value;if(lm)pr.llm_model=lm;var lt=document.getElementById("prLlmTimeout").value.trim();if(lt!=="")pr.llm_timeout=parseFloat(lt);cfg.proofread=pr;cfg.shortcuts=collectKVT("shortcuts")}
function collectKVT(tid){var r={};document.getElementById(tid).querySelectorAll("tr").forEach(function(tr){var ins=tr.querySelectorAll("input");if(ins.length<2)return;var k=ins[0].value.trim(),v=ins[1].value;if(k)r[k]=v});return r}
function collectExtraConfig(){
  try{
    cfg.fonts = {
      body: document.getElementById("cfgFontBody").value.trim(),
      heading: document.getElementById("cfgFontHeading").value.trim(),
      note: document.getElementById("cfgFontNote").value.trim(),
      citation: document.getElementById("cfgFontCitation").value.trim()
    };
  }catch(e){/* element missing -> skip */}
  try{
    cfg.image_preprocess = {
      enabled: !!document.getElementById("cfgImgPreEnabled").checked,
      gray: !!document.getElementById("cfgImgGray").checked,
      denoise: !!document.getElementById("cfgImgDenoise").checked,
      sharpen: !!document.getElementById("cfgImgSharpen").checked,
      binarize: !!document.getElementById("cfgImgBinarize").checked,
      workers: parseInt(document.getElementById("cfgImgWorkers").value,10)||0
    };
  }catch(e){/* element missing -> skip */}
  try{
    var ex=document.getElementById("cvtExclude").value.trim();
    cfg.exclude_pages = ex ? ex.split(",").map(function(s){return s.trim()}).filter(Boolean) : [];
  }catch(e){/* element missing -> skip */}
}
function saveConfig(){collectConfig();collectExtraConfig();var btn=document.getElementById("saveBtn");btn.disabled=true;btn.classList.add("saving");addLog("保存配置中...","log-info");apiPost("/api/config",cfg).then(function(res){btn.disabled=false;btn.classList.remove("saving");if(res&&res.ok){var newModel=cfg.selected_model||"";toast("已切换模型："+newModel,"ok",5000);addLog("模型已切换: "+newModel,"log-ok");fetchStatus().then(function(s){if(s&&s.probe!=="none"){var hint="，当前服务仍在运行旧模型，重启后生效";toast("已切换模型："+newModel+hint,"ok",5500);addLog("服务仍运行旧模型，建议重启","log-warn")}else{toast("已切换模型："+newModel,"ok",5000);addLog("模型切换生效（无运行服务）","log-ok")}}).catch(function(){toast("已切换模型："+newModel,"ok",5000)})}else{var msg=res&&res.error?res.error:"未知错误";toast("保存失败: "+msg,"fail",5000);addLog("保存失败: "+msg,"log-err")}})}
function serverStart(){var model=cfg.selected_model||"";document.getElementById("btnStart").disabled=true;document.getElementById("btnStart").textContent="启动中…";addLog("正在启动服务（模型: "+model+"）...","log-info");var startTime=Date.now();var pollInterval=setInterval(function(){apiGet("/api/status").then(function(res){if(!res||!res.ok)return;statusInfo=res;renderStatus(res);if(res.probe==="match"){clearInterval(pollInterval);var modelName=res.model_name||model;toast("模型已启动："+modelName,"ok",5000);addLog("服务已就绪: "+modelName,"log-ok");document.getElementById("btnStart").disabled=false;document.getElementById("btnStart").textContent="启动服务"}var now=Date.now();if(now-startTime>60000){clearInterval(pollInterval);toast("启动超时，请检查模型路径或端口配置","fail",5000);addLog("服务启动超时","log-err");document.getElementById("btnStart").disabled=false;document.getElementById("btnStart").textContent="启动服务"}},2000);setTimeout(function(){clearInterval(pollInterval);toast("启动超时，请检查模型路径或端口配置","fail",5000);addLog("服务启动超时","log-err");document.getElementById("btnStart").disabled=false;document.getElementById("btnStart").textContent="启动服务"},60000)});apiPost("/api/server/start",{model:model}).then(function(res){if(res&&res.ok){addLog("服务启动请求已发送","log-ok")}else{var msg=res&&res.error?res.error:"启动失败";toast(msg,"fail",5000);addLog("启动失败: "+msg,"log-err")}}).catch(function(e){toast("请求失败: "+e.message,"fail",5000);addLog("启动请求异常: "+e.message,"log-err");document.getElementById("btnStart").disabled=false;document.getElementById("btnStart").textContent="启动服务"})}
function serverStop(){document.getElementById("btnStop").disabled=true;addLog("正在停止服务...","log-info");var stopStart=Date.now();apiPost("/api/server/stop").then(function(res){if(res&&res.ok){var pollStop=setInterval(function(){apiGet("/api/status").then(function(res){if(!res||!res.ok)return;statusInfo=res;renderStatus(res);if(res.probe==="none"||res.probe==="mismatch"){clearInterval(pollStop);toast("服务已停止","ok",5000);addLog("服务已停止","log-ok");document.getElementById("btnStop").disabled=false}else{if(Date.now()-stopStart>30000){clearInterval(pollStop);toast("停止超时，请重试","fail",5000);addLog("服务停止超时","log-err");document.getElementById("btnStop").disabled=false}}},2000);setTimeout(function(){clearInterval(pollStop);toast("停止超时，请重试","fail",5000);addLog("服务停止超时","log-err");document.getElementById("btnStop").disabled=false},30000)})}else{toast("停止失败","fail",5000);addLog("停止失败","log-err")}setTimeout(refreshStatus,1000)}).catch(function(e){toast("请求失败: "+e.message,"fail",5000);addLog("停止请求异常: "+e.message,"log-err");document.getElementById("btnStop").disabled=false})}
function refreshStatus(){addLog("刷新服务状态...","log-info");fetchStatus()}
function pickFile(inputId){apiPost("/api/pick",{kind:"file",title:"选择文件"}).then(function(res){if(res&&res.ok&&!res.cancelled&&res.path){document.getElementById(inputId).value=res.path;toast("已选择文件","ok")}else if(res&&res.cancelled){toast("已取消选择","warn")}})}
function pickDir(inputId){apiPost("/api/pick",{kind:"dir",title:"选择目录"}).then(function(res){if(res&&res.ok&&!res.cancelled&&res.path){document.getElementById(inputId).value=res.path;toast("已选择目录","ok")}else if(res&&res.cancelled){toast("已取消选择","warn")}})}
/* ===== 转换页 ===== */
var convertPollTimer=null;
function renderConvert(){document.getElementById("cvtExclude").value=(cfg.exclude_pages||[]).join(",")||"";var sel=document.getElementById("cvtModel");sel.innerHTML="";models.forEach(function(m){var o=document.createElement("option");o.value=m.key;o.textContent=m.key+" - "+m.name;sel.appendChild(o)});sel.value=cfg.selected_model||"";applyModelWorkers();var pdf=document.getElementById("cvtPdf");if(pdf.value){var hint=document.getElementById("cvtPdfHint");if(!pdf.value.toLowerCase().endsWith(".pdf")){hint.textContent="\u26a0 \u6587\u4ef6\u540e\u7f00\u4e0d\u662f .pdf";hint.style.color="var(--red)"}else{hint.textContent="";hint.style.color=""}}}
function applyModelWorkers(){var sel=document.getElementById("cvtModel"),wEl=document.getElementById("cvtWorkers");if(!sel||!wEl)return;var key=sel.value,info=(cfg.model_choices||{})[key]||{},w=parseInt(info.workers,10);if(!isNaN(w)&&w>=1&&w<=64)wEl.value=w}
function onCvtModelChange(){applyModelWorkers()}
function pickPdf(){apiPost("/api/pick",{kind:"file",filter:"pdf",title:"\u9009\u62e9 PDF \u6587\u4ef6"}).then(function(res){if(res&&res.ok&&!res.cancelled&&res.path){document.getElementById("cvtPdf").value=res.path;var hint=document.getElementById("cvtPdfHint");if(!res.path.toLowerCase().endsWith(".pdf")){hint.textContent="\u26a0 \u6587\u4ef6\u540e\u7f00\u4e0d\u662f .pdf";hint.style.color="var(--red)"}else{hint.textContent=""}}else if(res&&res.cancelled){toast("\u5df2\u53d6\u6d88\u9009\u62e9","warn")}})}
function pickOutDir(){apiPost("/api/pick",{kind:"dir",title:"\u9009\u62e9\u8f93\u51fa\u76ee\u5f55"}).then(function(res){if(res&&res.ok&&!res.cancelled&&res.path){document.getElementById("cvtOutDir").value=res.path;toast("\u5df2\u9009\u62e9\u76ee\u5f55","ok")}else if(res&&res.cancelled){toast("\u5df2\u53d6\u6d88\u9009\u62e9","warn")}})}
function collectConvertParams(){var pdf=document.getElementById("cvtPdf").value.trim();if(!pdf)return{error:"\u8bf7\u5148\u9009\u62e9 PDF \u6587\u4ef6"};if(!pdf.toLowerCase().endsWith(".pdf"))return{error:"\u6587\u4ef6\u540e\u7f00\u4e0d\u662f .pdf\uff0c\u8bf7\u9009\u62e9\u6b63\u786e\u7684 PDF"};var engine=document.getElementById("cvtEngine").value;var workers=parseInt(document.getElementById("cvtWorkers").value,10);var timeout=parseInt(document.getElementById("cvtTimeout").value,10);if(isNaN(workers)||workers<1){var _k=document.getElementById("cvtModel").value,_m=(cfg.model_choices||{})[_k]||{},_w=parseInt(_m.workers,10);workers=isNaN(_w)||_w<1?3:_w}if(isNaN(timeout)||timeout<30)timeout=600;return{pdf:pdf,dpi:parseInt(document.getElementById("cvtDpi").value,10),model:document.getElementById("cvtModel").value,engine:engine,workers:workers,timeout:timeout,thinking:document.getElementById("cvtThinking").checked,title:document.getElementById("cvtTitle").value.trim(),author:document.getElementById("cvtAuthor").value.trim(),lang:document.getElementById("cvtLang").value.trim()||"zh-CN",out_dir:document.getElementById("cvtOutDir").value.trim(),epub_path:"",exclude:document.getElementById("cvtExclude").value.trim()}}
function setConvertBusy(busy){var start=document.getElementById("cvtStartBtn"),stop=document.getElementById("cvtStopBtn");if(busy){start.disabled=true;start.classList.add("running");start.textContent="\u8f6c\u6362\u4e2d\u2026";stop.style.display="";stop.disabled=false}else{start.disabled=false;start.classList.remove("running");start.textContent="\u5f00\u59cb\u8f6c\u6362";stop.style.display="none";stop.disabled=true}}
function startConvert(){var params=collectConvertParams();if(params.error){toast(params.error,"warn");return}setConvertBusy(true);var log=document.getElementById("convertLog");log.textContent="";addLog("\u542f\u52a8\u8f6c\u6362: "+params.pdf,"log-info");apiPost("/api/convert/start",params).then(function(res){if(res&&res.ok){toast("\u8f6c\u6362\u5df2\u542f\u52a8","ok");addLog("\u8f6c\u6362\u5df2\u542f\u52a8","log-ok");startPollConvert()}else{var msg=res&&res.error?res.error:"\u542f\u52a8\u5931\u8d25";toast(msg,"fail");addLog("\u542f\u52a8\u5931\u8d25: "+msg,"log-err");setConvertBusy(false)}})}
function startPollConvert(){if(convertPollTimer)clearInterval(convertPollTimer);convertPollTimer=setInterval(pollConvertStatus,500)}
function stopPollConvert(){if(convertPollTimer){clearInterval(convertPollTimer);convertPollTimer=null}}
function pollConvertStatus(){apiGet("/api/convert/status").then(function(res){if(!res||!res.ok)return;renderConvertLog(res.lines||[]);if(res.prompt){showConvertPrompt(res.prompt);stopPollConvert();return}if(res.running)return;stopPollConvert();setConvertBusy(false);if(res.done&&res.success===true){var ep=res.epub_path||"";var msg="\u8f6c\u6362\u5b8c\u6210";if(ep)msg+="\uff1a"+ep;toast(msg,"ok");addLog(msg,"log-ok");var log=document.getElementById("convertLog");log.textContent+="\n\u2714 "+msg+"\n"}else if(res.done&&res.success===false){var errmsg=res.error||"\u8f6c\u6362\u5931\u8d25";toast(errmsg,"fail");addLog("\u8f6c\u6362\u5931\u8d25: "+errmsg,"log-err")}})}
function showConvertPrompt(p){var bg=document.getElementById("convertPromptBg");if(!bg)return;document.getElementById("convertPromptQuestion").textContent=p.question||"\u8bf7\u9009\u62e9\u64cd\u4f5c";var box=document.getElementById("convertPromptBtns");box.innerHTML="";(p.options||[]).forEach(function(o){var b=document.createElement("button");b.className=o.value===p.default?"btn-start":"btn-small";b.textContent=o.label||o.value;b.style.cssText="padding:10px 14px;font-size:13px;cursor:pointer;";b.onclick=function(){apiPost("/api/convert/prompt",{choice:o.value}).then(function(res){if(res&&res.ok){bg.style.display="none";addLog("\u5df2\u9009\u62e9: "+(o.label||o.value),"log-info");startPollConvert()}else{toast((res&&res.error)||"\u56de\u7b54\u5931\u8d25","fail");startPollConvert()}})};box.appendChild(b)});bg.style.display="flex"}
function renderConvertLog(lines){var el=document.getElementById("convertLog");if(!lines.length)return;el.textContent=lines.join("\n");el.scrollTop=el.scrollHeight}
function stopConvert(){apiPost("/api/convert/stop").then(function(res){if(res&&res.ok){toast("\u5df2\u8bf7\u6c42\u505c\u6b62","warn");addLog("\u5df2\u8bf7\u6c42\u505c\u6b62\u8f6c\u6362","log-warn");var bg=document.getElementById("convertPromptBg");if(bg)bg.style.display="none";setConvertBusy(false);stopPollConvert()}else{var msg=res&&res.error?res.error:"\u505c\u6b62\u5931\u8d25";toast(msg,"fail")}})}
/* ===== 矫正界面 ===== */
var correctPollTimer=null;
function setCorrectBusy(busy){var start=document.getElementById("crStartBtn"),stop=document.getElementById("crStopBtn");if(busy){start.disabled=true;start.classList.add("running");start.textContent="\u77eb\u6b63\u4e2d\u2026";stop.style.display="";stop.disabled=false}else{start.disabled=false;start.classList.remove("running");start.textContent="\u542f\u52a8\u77eb\u6b63";stop.style.display="none";stop.disabled=true}}
function startCorrect(){var pdf=document.getElementById("cvtPdf").value.trim();var engine=document.getElementById("cvtEngine").value;var params={pdf:pdf||null,engine:engine||null,title:document.getElementById("cvtTitle").value.trim(),author:document.getElementById("cvtAuthor").value.trim(),lang:document.getElementById("cvtLang").value.trim()||"zh-CN"};setCorrectBusy(true);var log=document.getElementById("correctLog");log.textContent="";addLog("\u542f\u52a8\u77eb\u6b63: "+(pdf||"\u65e0\u6587\u4ef6\u542f\u52a8"),"log-info");apiPost("/api/correct/start",params).then(function(res){if(res&&res.ok){toast("\u77eb\u6b63\u5df2\u542f\u52a8","ok");addLog("\u77eb\u6b63\u5df2\u542f\u52a8","log-ok");startPollCorrect()}else{var msg=res&&res.error?res.error:"\u542f\u52a8\u5931\u8d25";toast(msg,"fail");addLog("\u542f\u52a8\u5931\u8d25: "+msg,"log-err");setCorrectBusy(false)}})}
function startPollCorrect(){if(correctPollTimer)clearInterval(correctPollTimer);correctPollTimer=setInterval(pollCorrectStatus,500)}
function stopPollCorrect(){if(correctPollTimer){clearInterval(correctPollTimer);correctPollTimer=null}}
function pollCorrectStatus(){apiGet("/api/correct/status").then(function(res){if(!res||!res.ok)return;renderCorrectLog(res.lines||[]);if(res.running)return;stopPollCorrect();setCorrectBusy(false);if(res.done&&res.success===true){toast("\u77eb\u6b63\u5b8c\u6210","ok");addLog("\u77eb\u6b63\u5b8c\u6210","log-ok")}else if(res.done&&res.success===false){var errmsg=res.error||"\u77eb\u6b63\u5931\u8d25";toast(errmsg,"fail");addLog("\u77eb\u6b63\u5931\u8d25: "+errmsg,"log-err")}})}
function renderCorrectLog(lines){var el=document.getElementById("correctLog");if(!lines.length)return;el.textContent=lines.join("\n");el.scrollTop=el.scrollHeight}
function stopCorrect(){apiPost("/api/correct/stop").then(function(res){if(res&&res.ok){toast("\u5df2\u8bf7\u6c42\u505c\u6b62","warn");addLog("\u5df2\u8bf7\u6c42\u505c\u6b62\u77eb\u6b63","log-warn")}else{var msg=res&&res.error?res.error:"\u505c\u6b62\u5931\u8d25";toast(msg,"fail")}})}
/* ===== 工具页：多 EPUB 合并 ===== */
var mergePollTimer=null;
var mergeFiles=[];
function setMergeBusy(busy){var start=document.getElementById("mergeStartBtn"),stop=document.getElementById("mergeStopBtn");if(busy){start.disabled=true;start.classList.add("running");start.textContent="\u5408\u5e76\u4e2d\u2026";stop.style.display="";stop.disabled=false}else{start.disabled=false;start.classList.remove("running");start.textContent="\u5f00\u59cb\u5408\u5e76";stop.style.display="none";stop.disabled=true}}
function basename(p){return p.split(/[\\\/]/).pop()}
function renderMergeFileList(){var list=document.getElementById("mergeFileList"),cnt=document.getElementById("mergeFileCount");cnt.textContent="\u5171 "+mergeFiles.length+" \u4e2a\u6587\u4ef6";if(!mergeFiles.length){list.innerHTML='<div class="empty-state">\u672a\u6dfb\u52a0\u4efb\u4f55\u6587\u4ef6\uff0c\u70b9\u51fb\u300a\u6dfb\u52a0\u6587\u4ef6\u300b\u9009\u62e9 EPUB\u3002</div>';return}list.innerHTML="";mergeFiles.forEach(function(p,i){var row=document.createElement("div");row.className="merge-file-row";row.innerHTML='<span class="mfi">\u2192'+(i+1)+'</span><span class="mfn" title="'+escH(p)+'">'+escH(basename(p))+'</span><span class="mops"><button class="btn-small" onclick="moveMergeFile('+i+',-1)" title="\u4e0a\u79fb" '+(i===0?'disabled':'')+'>&#8593;</button><button class="btn-small" onclick="moveMergeFile('+i+',1)" title="\u4e0b\u79fb" '+(i===mergeFiles.length-1?'disabled':'')+'>&#8595;</button><button class="btn-small del-btn" onclick="removeMergeFile('+i+')" title="\u79fb\u9664"></button></span>';list.appendChild(row)})}
function moveMergeFile(idx,dir){var n=idx+dir;if(n<0||n>=mergeFiles.length)return;var t=mergeFiles[idx];mergeFiles.splice(idx,1);mergeFiles.splice(n,0,t);renderMergeFileList()}
function removeMergeFile(idx){mergeFiles.splice(idx,1);renderMergeFileList()}
function pickMergeFiles(){apiPost("/api/pick",{kind:"file",filter:"epub",multiple:true,title:"\u9009\u62e9 EPUB \u6587\u4ef6"}).then(function(res){if(!res)return;if(res.ok&&!res.cancelled&&res.paths){res.paths.forEach(function(p){if(mergeFiles.indexOf(p)<0)mergeFiles.push(p)});renderMergeFileList();toast("\u5df2\u6dfb\u52a0 "+res.paths.length+" \u4e2a\u6587\u4ef6","ok")}else if(res&&res.cancelled){toast("\u5df2\u53d6\u6d88\u9009\u62e9","warn")}else if(res&&res.error){toast("选择失败: "+res.error,"fail")}})}
function collectMergeParams(){var files=mergeFiles.slice();if(files.length<2)return{error:"\u8bf7\u9009\u62e9\u81f3\u5c11 2 \u4e2a EPUB \u6587\u4ef6"};return{paths:files,title:document.getElementById("mergeTitle").value.trim(),author:document.getElementById("mergeAuthor").value.trim(),lang:document.getElementById("mergeLang").value.trim()||"zh-CN",out_path:document.getElementById("mergeOutPath").value.trim()||null}}
function startMerge(){var params=collectMergeParams();if(params.error){toast(params.error,"warn");return}setMergeBusy(true);var log=document.getElementById("mergeLog");log.textContent="";addLog("\u5f00\u59cb\u5408\u5e76: "+params.paths.length+" \u4e2a\u6587\u4ef6","log-info");apiPost("/api/tools/merge/start",params).then(function(res){if(res&&res.ok){toast("\u5408\u5e76\u5df2\u542f\u52a8","ok");addLog("\u5408\u5e76\u5df2\u542f\u52a8","log-ok");startPollMerge()}else{var msg=res&&res.error?res.error:"\u542f\u59cb\u5931\u8d25";toast(msg,"fail");addLog("\u542f\u59cb\u5931\u8d25: "+msg,"log-err");setMergeBusy(false)}})}
function startPollMerge(){if(mergePollTimer)clearInterval(mergePollTimer);mergePollTimer=setInterval(pollMergeStatus,500)}
function stopPollMerge(){if(mergePollTimer){clearInterval(mergePollTimer);mergePollTimer=null}}
function pollMergeStatus(){apiGet("/api/tools/merge/status").then(function(res){if(!res||!res.ok)return;renderMergeLog(res.lines||[]);if(res.running)return;stopPollMerge();setMergeBusy(false);if(res.done&&res.success===true){var out=res.out_path||"";var msg="\u5408\u5e76\u5b8c\u6210";if(out)msg+="\uff1a"+out;toast(msg,"ok");addLog(msg,"log-ok")}else if(res.done&&res.success===false){var errmsg=res.error||"\u5408\u5e76\u5931\u8d25";toast(errmsg,"fail");addLog("\u5408\u5e76\u5931\u8d25: "+errmsg,"log-err")}})}
function renderMergeLog(lines){var el=document.getElementById("mergeLog");if(!lines.length)return;el.textContent=lines.join("\n");el.scrollTop=el.scrollHeight}
function stopMerge(){apiPost("/api/tools/merge/stop").then(function(res){if(res&&res.ok){toast(res.message||"\u5df2\u8bf7\u6c42\u505c\u6b62","warn");addLog("\u5df2\u8bf7\u6c42\u505c\u6b62\u5408\u5e76","log-warn")}else{var msg=res&&res.error?res.error:"\u505c\u6b62\u5931\u8d25";toast(msg,"fail")}})}
setInterval(function(){fetch("/api/ping").catch(function(){})},30000);
window.addEventListener("pagehide",function(){stopPollConvert();stopPollCorrect();stopPollMerge();navigator.sendBeacon("/api/bye")});
window.addEventListener("pageshow",function(){fetch("/api/ping").catch(function(){})});
fetchConfig().then(function(){fetchStatus()});
</script>
</body>
</html>
"""

# 文件选择对话框等待超时（秒）：主循环未处理时防御性返回
_PICK_TIMEOUT = 120

# 请求体解析失败哨兵（区别于「无请求体」的 None）
_BAD_JSON = object()

# 转换日志环形缓冲上限（行）：超出丢弃最旧行，避免内存无限增长
_CONVERT_MAX_LINES = 2000

# 合并日志环形缓冲上限（行）：与转换同步
_MERGE_MAX_LINES = 2000

# 转换子进程的弹窗询问协议标记（与 mian.py _PROMPT_MARKER 同值）：子进程在
# 需要用户决策（OCR 断点续传选择）时打印 `__PTOE_PROMPT__ <json>` 单行，
# 监控线程截获后存入 state["convert"]["prompt"]，浏览器弹窗选择后经
# /api/convert/prompt 写回子进程 stdin。
_PROMPT_MARKER = "__PTOE_PROMPT__"

# 仓库根目录：开发环境用 sys.executable 跑 mian.py 时定位脚本路径
ROOT = os.path.dirname(os.path.abspath(__file__))


def _convert_argv(
    pdf: str,
    *,
    dpi: int | None = None,
    model: str | None = None,
    engine: str | None = None,
    workers: int | None = None,
    timeout: int | None = None,
    thinking: bool = False,
    title: str | None = None,
    author: str | None = None,
    lang: str | None = None,
    out_dir: str | None = None,
    epub_path: str | None = None,
    exclude: str | None = None,
) -> list[str]:
    """组装「epub」子命令的 argv（冻结 exe 直接跑自身，开发环境跑 mian.py）。

    参数与 mian.py epub 子命令一一对应；None 表示不传（走默认值）。
    """
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "epub"]
    else:
        argv = [sys.executable, "-u", os.path.join(ROOT, "mian.py"), "epub"]
    argv.append(pdf)
    if dpi is not None:
        argv += ["--dpi", str(dpi)]
    if model:
        argv += ["--model", model]
    if engine:
        argv += ["--engine", engine]
    if workers is not None:
        argv += ["--workers", str(workers)]
    if timeout is not None:
        argv += ["--timeout", str(timeout)]
    if thinking:
        argv.append("--thinking")
    if title:
        argv += ["--title", title]
    if author:
        argv += ["--author", author]
    if lang:
        argv += ["--lang", lang]
    if out_dir:
        argv += ["--out-dir", out_dir]
    if epub_path:
        argv += ["--epub-path", epub_path]
    if exclude:
        argv += ["--exclude", exclude]
    return argv


def _correct_argv(
    pdf: str | None = None,
    *,
    engine: str | None = None,
    title: str | None = None,
    author: str | None = None,
    lang: str | None = None,
    out_dir: str | None = None,
    epub_path: str | None = None,
    correct_timeout: int | None = None,
) -> list[str]:
    """组装「correct」子命令的 argv。pdf 可为 None（无文件启动）。"""
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "correct"]
    else:
        argv = [sys.executable, "-u", os.path.join(ROOT, "mian.py"), "correct"]
    if pdf:
        argv.append(pdf)
    if engine:
        argv += ["--engine", engine]
    if title:
        argv += ["--title", title]
    if author:
        argv += ["--author", author]
    if lang:
        argv += ["--lang", lang]
    if out_dir:
        argv += ["--out-dir", out_dir]
    if epub_path:
        argv += ["--epub-path", epub_path]
    if correct_timeout is not None:
        argv += ["--correct-timeout", str(correct_timeout)]
    return argv


def _correct_server_info_path():
    """懒 import correctmanage 调其 _server_info_path()，import 失败返回 None。

    供 GUI 发现并恢复已存活的矫正界面（浏览器关闭但 correctmanage 服务仍在）。
    """
    try:
        import correctmanage

        return correctmanage._server_info_path()
    except Exception:  # noqa: BLE001
        return None


def _read_correct_server_info():
    """读取矫正服务 sidecar JSON，校验 port/pid 合法性。

    返回 {"port": int, "pid": int, "started": float} 或 None（文件缺失/损坏/
    字段非法）。
    """
    p = _correct_server_info_path()
    if p is None or not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    port = info.get("port")
    pid = info.get("pid")
    if not isinstance(port, int) or not isinstance(pid, int):
        return None
    if not (0 < port < 65536):
        return None
    return info


def _probe_correct_ui(port: int) -> bool:
    """探测矫正界面是否存活：GET /api/ping，200 即 True。

    超时 1.5 秒，任何异常返回 False。
    """
    try:
        import requests

        r = requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _convert_monitor(st: dict, proc) -> None:
    """转换子进程监控线程：流式收集 stdout 日志，进程结束后写完成状态。

    st 为 state["convert"]（锁内读写）；proc 为 subprocess.Popen 对象。
    成功时从日志尾部解析「Done: <路径>」行作为 epub_path。
    截获 __PTOE_PROMPT__ 决策标记（OCR 断点续传弹窗询问）：载荷存入
    st["prompt"]（浏览器弹窗），标记行本身不进日志。

    关键：stdout 由独立读线程搬进队列，主循环以 proc.poll() 兜底收尾——
    转换子进程会拉起 llama-server，后者继承 stdout 管道且常驻，导致管道
    永不 EOF；若主循环死等 EOF，转换完成后 running 恒为 True、按钮停在
    「转换中」且停止无效（2026-08-17 修复）。主进程一旦退出（poll() !=
    None），排空已缓冲输出后即收尾，不再等待 EOF。
    注意：矫正子进程同样复用本监控（state 无 prompt 键），prompt 访问全部
    按「键存在才写」防御处理。
    """
    has_prompt = "prompt" in st
    lines_q = queue.Queue()

    def _reader() -> None:
        """后台读线程：逐行搬进队列；stdout EOF/异常时放 None 哨兵。"""
        try:
            for line in proc.stdout:
                lines_q.put(line)
        except Exception:  # noqa: BLE001  读取异常按 EOF 处理
            pass
        finally:
            lines_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def _handle_line(text: str) -> None:
        marker = text.strip()
        if marker.startswith(_PROMPT_MARKER):
            payload_text = marker[len(_PROMPT_MARKER) :].strip()
            try:
                payload = json.loads(payload_text)
            except Exception:  # noqa: BLE001  载荷损坏不阻断转换
                payload = None
            with st["lock"]:
                if has_prompt:
                    st["prompt"] = (
                        payload
                        if isinstance(payload, dict) and payload.get("options")
                        else {
                            "id": "ocr_resume",
                            "question": "转换需要选择操作",
                            "options": [{"value": "abort", "label": "取消"}],
                            "default": "abort",
                        }
                    )
                st["lines"].append("—— 需要选择：请在弹出的窗口中选择操作 ——")
                if len(st["lines"]) > _CONVERT_MAX_LINES:
                    del st["lines"][: len(st["lines"]) - _CONVERT_MAX_LINES]
            return
        with st["lock"]:
            st["lines"].append(text)
            if len(st["lines"]) > _CONVERT_MAX_LINES:
                del st["lines"][: len(st["lines"]) - _CONVERT_MAX_LINES]

    try:
        while True:
            try:
                line = lines_q.get(timeout=0.5)
            except queue.Empty:
                # 主进程已退出：stdout 可能被子进程（llama-server）继承而永不
                # EOF，给一点时间让读线程排空剩余缓冲（如结尾的 Done: 行）
                if proc.poll() is not None:
                    drain_deadline = time.time() + 2.0
                    while time.time() < drain_deadline:
                        try:
                            extra = lines_q.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        if extra is None:
                            break
                        _handle_line(extra)
                    break
                continue
            if line is None:
                break  # stdout 已 EOF（子进程未继承管道或已全部关闭）
            _handle_line(line)
        rc = proc.wait()
        with st["lock"]:
            st["running"] = False
            st["done"] = True
            st["exit_code"] = rc
            if has_prompt:
                st["prompt"] = None  # 结束前未回答的弹窗一并清除
            if rc == 0:
                epub_path = None
                for line in reversed(st["lines"]):
                    if "Done:" in line:
                        epub_path = line.split("Done:", 1)[1].strip()
                        break
                if epub_path:
                    st["success"] = True
                    st["epub_path"] = epub_path
                else:
                    # rc=0 但没有 Done 行：通常是被用户取消（pdf_to_epub 返回
                    # cancelled，主流程打印「已取消」）
                    st["success"] = False
                    tail = [ln for ln in st["lines"][-3:] if ln.strip()]
                    cancelled = any("已取消" in ln for ln in tail)
                    st["error"] = (
                        "已取消" if cancelled else ("；".join(tail) if tail else "未生成 EPUB")
                    )
            else:
                tail = [ln for ln in st["lines"][-3:] if ln.strip()]
                st["error"] = "；".join(tail) if tail else "转换进程异常退出"
    except Exception as e:  # noqa: BLE001  监控线程异常不崩溃服务
        with st["lock"]:
            st["running"] = False
            st["done"] = True
            st["success"] = False
            if has_prompt:
                st["prompt"] = None
            st["error"] = str(e)


def _merge_worker(st: dict, paths, title, author, lang, out_path) -> None:
    """合并 EPUB 的后台监控线程。

    st 为 state["merge"]（锁内读写）；调用 epubmergemanage.merge_epubs
    （延迟导入，缺失时记录「合并模块未就绪」）。progress 回调把进度行
    追加进环形缓冲；should_stop 读取 stop_event 以支持中止。
    """
    try:
        import epubmergemanage  # noqa: F401  延迟导入：合并引擎由并行Agent构建
    except Exception:
        with st["lock"]:
            st["lines"].append("合并模块未就绪")
            st["running"] = False
            st["done"] = True
            st["success"] = False
            st["error"] = "合并模块未就绪"
        return
    try:

        def progress(msg: str) -> None:
            with st["lock"]:
                st["lines"].append(msg)
                if len(st["lines"]) > _MERGE_MAX_LINES:
                    del st["lines"][: len(st["lines"]) - _MERGE_MAX_LINES]

        result = epubmergemanage.merge_epubs(
            paths,
            out_path=out_path or None,
            title=title or "",
            author=author or "",
            lang=lang or "zh-CN",
            progress=progress,
            should_stop=st["stop_event"].is_set,
        )
        with st["lock"]:
            st["running"] = False
            st["done"] = True
            if isinstance(result, dict) and result.get("ok"):
                st["success"] = True
                st["out_path"] = result.get("out_path")
            else:
                st["success"] = False
                st["error"] = (
                    (result or {}).get("error") or "合并失败"
                    if isinstance(result, dict)
                    else "合并失败"
                )
    except Exception as e:  # noqa: BLE001  监控线程异常不崩溃服务
        with st["lock"]:
            st["running"] = False
            st["done"] = True
            st["success"] = False
            st["error"] = str(e)


# 心跳失联场景（未收到信标，如浏览器被强杀/崩溃）需连续失联这么久才判定，
# 避免电脑休眠唤醒后短暂失联导致误判。
_STALE_CONFIRM_SECONDS = 3.0


def _browser_gone(
    state: dict,
    *,
    idle_timeout: int,
    now: float | None = None,
    stale_since: float | None = None,
) -> tuple[bool, float | None]:
    """判断浏览器是否已关闭且应自动退出。

    返回 (gone, stale_since)：gone=True 表示判定成立；stale_since 用于心跳
    失联场景的连续确认（首次失联记时刻，持续 _STALE_CONFIRM_SECONDS 才认定）。
    - gone_at 有值（收到 /api/bye 页面关闭信标）：now - gone_at >= idle_timeout 判定；
    - 否则距 last_beat（/api/ping 心跳）超过 idle_timeout*2 视为失联，
      需连续失联确认（防休眠唤醒误判）。
    """
    now = time.monotonic() if now is None else now
    gone_at = state.get("gone_at")
    if gone_at is not None:
        # 收到过 pagehide 信标（标签页被关闭）：信标为准，倒计时满即判定
        return (now - gone_at >= idle_timeout), None
    if now - state.get("last_beat", 0.0) >= idle_timeout * 2:
        # 心跳失联（无信标，如浏览器被强杀）：需连续失联确认，防休眠唤醒误判
        if stale_since is None:
            return False, now
        return (now - stale_since >= _STALE_CONFIRM_SECONDS), stale_since
    return False, None


def _pick_path(kind: str, title: str | None, filt: str | None = None, multiple: bool = False) -> dict:
    """弹 tkinter 文件/目录选择对话框（仅主线程调用）。

    filt 仅对 kind=="file" 生效："pdf" 限定 PDF，"epub" 限定 EPUB。
    multiple=True 时多选，返回 {ok, paths:[...]}。
    返回 {ok: True, path}（单选选中）| {ok: True, paths:[...]}（多选）|
    {ok: True, cancelled: True}（取消）| {ok: False, error}（tkinter 不可用）。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return {"ok": False, "error": "无法弹出文件选择对话框"}
    try:
        root = tk.Tk()
        root.withdraw()
        # 置顶：避免对话框出现在浏览器窗口后面（root 已 withdraw，无任务栏入口）
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001  个别环境不支持 topmost，忽略
            pass
        try:
            if kind == "dir":
                path = filedialog.askdirectory(title=title or "选择文件夹")
            elif multiple:
                ftypes = {
                    "pdf": [("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
                    "epub": [("EPUB 文件", "*.epub"), ("所有文件", "*.*")],
                }.get(filt or "")
                path = filedialog.askopenfilenames(
                    title=title or "选择文件",
                    filetypes=ftypes,
                )
            elif filt == "pdf":
                path = filedialog.askopenfilename(
                    title=title or "选择 PDF 文件",
                    filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
                )
            else:
                path = filedialog.askopenfilename(title=title or "选择文件")
        finally:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
    except Exception:
        # headless 无 display：tkinter 抛 TclError
        return {"ok": False, "error": "无法弹出文件选择对话框"}
    if multiple:
        if not path:
            return {"ok": True, "cancelled": True}
        return {"ok": True, "paths": list(path)}
    if not path:
        return {"ok": True, "cancelled": True}
    return {"ok": True, "path": path}


def _drain_dialog_queue(state: dict) -> None:
    """主循环调用：取出待弹的文件选择对话框请求，逐个弹框并回填结果。

    tkinter 不能在 HTTP worker 线程里可靠弹窗，因此统一挪到 gui_serve 的
    主循环里执行（与 correctmanage 的导出保存对话框同一模式）。
    """
    while True:
        try:
            req = state["dlg_queue"].get_nowait()
        except queue.Empty:
            return
        try:
            req["result"] = _pick_path(
                str(req.get("kind") or "file"),
                req.get("title"),
                req.get("filter"),
                req.get("multiple", False),
            )
        except Exception:  # noqa: BLE001
            req["result"] = {"ok": False, "error": "无法弹出文件选择对话框"}
        finally:
            req["done"].set()


def _abort_dialog_queue(state: dict) -> None:
    """服务关闭时唤醒所有阻塞在 /api/pick 上的请求（handler 返回 500）。"""
    while True:
        try:
            req = state["dlg_queue"].get_nowait()
        except queue.Empty:
            return
        req["aborted"] = True
        req["done"].set()


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class _GuiHandler(BaseHTTPRequestHandler):
    server_version = "ptoe-gui/1.0"

    # -- helpers --

    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str = "application/json; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            # client disconnected or socket error; swallow to keep server alive
            pass

    def _json(self, obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _read_body(self):
        """读取请求体并解析 JSON。

        返回解析后的对象；无请求体返回 None；请求体存在但解析失败返回 _BAD_JSON。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return None
        try:
            data = self.rfile.read(length)
            return json.loads(data.decode("utf-8"))
        except Exception:
            return _BAD_JSON

    def log_message(self, fmt: str, *args) -> None:
        # 静默访问日志，避免终端刷屏
        return

    # -- GET --

    def _api_config_get(self) -> None:
        """GET /api/config：读取配置 + 模型文件存在性 + 默认值 + 配置路径。"""
        try:
            import configmanage

            cfg = configmanage.get_config(show_dialogs=False)
            models_dir = cfg.get("models_dir") or ""
            models = []
            for key, info in (cfg.get("model_choices") or {}).items():
                info = info or {}
                name = str(info.get("name") or "")
                mmproj = str(info.get("mmproj") or "")
                models.append(
                    {
                        "key": key,
                        "name": name,
                        "mmproj": mmproj,
                        "workers": info.get("workers"),
                        "name_exists": bool(name)
                        and os.path.isfile(os.path.join(models_dir, name)),
                        "mmproj_exists": bool(mmproj)
                        and os.path.isfile(os.path.join(models_dir, mmproj)),
                    }
                )
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "config": cfg,
                        "defaults": configmanage.DEFAULT_CONFIG,
                        "models": models,
                        "path": os.path.abspath(configmanage._CONFIG_PATH),
                    }
                ),
            )
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))

    def _api_status(self) -> None:
        """GET /api/status：引擎 / 服务探测 / 端口 / 启动中状态。"""
        try:
            import configmanage
            import llamamanage

            cfg = configmanage.get_config(show_dialogs=False)
            engine = llamamanage._active_engine()
            model_key = cfg.get("selected_model") or ""
            info = (cfg.get("model_choices") or {}).get(model_key) or {}
            model_name = str(info.get("name") or model_key)
            try:
                probe = llamamanage._probe_server(model_name)
            except Exception as e:  # noqa: BLE001  探测异常视为无服务
                probe = "none"
                if not self.server.state.get("last_error"):
                    self.server.state["last_error"] = str(e)
            if engine == "vllm":
                args = cfg.get("vllm_server_args") or {}
                port = str(args.get("port") or "8000")
            else:
                args = cfg.get("llama_server_args") or {}
                port = str(args.get("port") or "8080")
            busy = self.server.state["serve_lock"].locked()
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "engine": engine,
                        "probe": probe,
                        "model_key": model_key,
                        "model_name": model_name,
                        "port": port,
                        "busy": busy,
                        "last_error": self.server.state.get("last_error") or "",
                    }
                ),
            )
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))

    def _api_ping(self) -> None:
        """GET /api/ping：页面心跳：刷新存活时刻，并取消可能存在的关闭倒计时（标签页被恢复/刷新）。"""
        st = self.server.state
        with st["beat_lock"]:
            st["last_beat"] = time.monotonic()
            st["gone_at"] = None
        self._send(200, self._json({"ok": True}))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, _UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/config":
            self._api_config_get()
            return
        if path == "/api/status":
            self._api_status()
            return
        if path == "/api/ping":
            self._api_ping()
            return
        if path == "/api/convert/status":
            self._api_convert_status()
            return
        if path == "/api/correct/status":
            self._api_correct_status()
            return
        if path == "/api/tools/merge/status":
            self._api_tools_merge_status()
            return
        self._send(404, self._json({"ok": False, "error": "未找到"}))

    # -- POST --

    def _api_config_post(self, body) -> None:
        """POST /api/config：校验并原子写配置。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        try:
            import configmanage

            cfg = configmanage.get_config(show_dialogs=False)
            # 校验：engine / selected_model / llama_server / models_dir
            engine = body.get("engine", cfg.get("engine"))
            if engine not in ("llama", "vllm"):
                self._send(400, self._json({"ok": False, "error": "engine 仅支持 llama / vllm"}))
                return
            choices = body.get("model_choices", cfg.get("model_choices") or {})
            sel = body.get("selected_model", cfg.get("selected_model"))
            if isinstance(choices, dict) and isinstance(sel, str) and sel.strip() and sel not in choices:
                canonical, matches = configmanage.find_canonical_model_key(choices, sel)
                if canonical is not None:
                    sel = canonical
                    body["selected_model"] = sel
                elif matches:
                    self._send(400, self._json({"ok": False, "error": f"ambiguous model: '{sel}' matches {matches}, use exact key or remove duplicates"}))
                    return
                else:
                    self._send(400, self._json({"ok": False, "error": f"未知模型：{sel}"}))
                    return

            for key in ("llama_server", "models_dir"):
                val = body.get(key, cfg.get(key))
                if not isinstance(val, str):
                    self._send(400, self._json({"ok": False, "error": f"{key} 必须是字符串"}))
                    return
            cfg.update(body)
            cfg = configmanage.validate_and_patch_config(cfg)
            with configmanage._CFG_LOCK:
                configmanage._atomic_write_json(configmanage._CONFIG_PATH, cfg)
            self._send(200, self._json({"ok": True}))
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))

    def _api_server_start(self, body) -> None:
        """POST /api/server/start：后台线程启动推理服务（立即返回）。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        model = body.get("model")
        if not isinstance(model, str) or not model:
            self._send(400, self._json({"ok": False, "error": "缺少模型参数"}))
            return
        try:
            import configmanage

            cfg = configmanage.get_config(show_dialogs=False)
            if model not in (cfg.get("model_choices") or {}):
                self._send(400, self._json({"ok": False, "error": f"未知模型：{model}"}))
                return
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))
            return
        st = self.server.state
        if not st["serve_lock"].acquire(blocking=False):
            self._send(409, self._json({"ok": False, "error": "服务正在启动中"}))
            return

        def _worker() -> None:
            try:
                import llamamanage

                llamamanage.runserver(model, with_mmproj=True)
            except Exception as e:  # noqa: BLE001
                st["last_error"] = str(e)
                traceback.print_exc()
            finally:
                st["serve_lock"].release()

        threading.Thread(target=_worker, daemon=True).start()
        self._send(200, self._json({"ok": True, "message": "服务启动中（后台加载模型）"}))

    def _api_server_stop(self) -> None:
        """POST /api/server/stop：停止推理服务。"""
        try:
            import llamamanage

            llamamanage.stopserver()
            self._send(200, self._json({"ok": True}))
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))

    def _api_pick(self, body) -> None:
        """POST /api/pick：把文件/目录选择请求交给主线程弹框并等待结果。

        multiple=true 时多选，返回 {ok, paths:[...]}；否则单选 {ok, path}。
        """
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        kind = body.get("kind")
        if kind not in ("file", "dir"):
            self._send(400, self._json({"ok": False, "error": "kind 仅支持 file / dir"}))
            return
        multiple = body.get("multiple", False)
        if not isinstance(multiple, bool):
            self._send(400, self._json({"ok": False, "error": "multiple 必须是布尔值"}))
            return
        filt = body.get("filter")
        if filt is not None and filt not in ("pdf", "epub"):
            self._send(400, self._json({"ok": False, "error": "filter 仅支持 pdf / epub"}))
            return
        req = {
            "kind": kind,
            "title": body.get("title"),
            "filter": filt if kind == "file" else None,
            "multiple": multiple,
            "done": threading.Event(),
            "result": None,
            "aborted": False,
        }
        st = self.server.state
        with st["dlg_lock"]:
            st["dlg_queue"].put(req)
        if not req["done"].wait(timeout=_PICK_TIMEOUT):
            # 主循环未处理（防御性）：超时返回
            self._send(500, self._json({"ok": False, "error": "文件选择对话框超时"}))
            return
        if req.get("aborted"):
            self._send(500, self._json({"ok": False, "error": "服务已关闭"}))
            return
        self._send(200, self._json(req["result"]))

    def _api_bye(self) -> None:
        """POST /api/bye：页面关闭信标，记录关闭时刻。"""
        st = self.server.state
        with st["beat_lock"]:
            st["gone_at"] = time.monotonic()
        self._send(200, self._json({"ok": True}))

    def _api_convert_start(self, body) -> None:
        """POST /api/convert/start：子进程启动完整 PDF→EPUB 转换（流式日志）。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        try:
            import configmanage

            cfg = configmanage.get_config(show_dialogs=False)
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))
            return
        # -- 参数校验（中文错误） --
        pdf = body.get("pdf")
        if not isinstance(pdf, str) or not pdf:
            self._send(400, self._json({"ok": False, "error": "缺少 PDF 文件路径"}))
            return
        if not pdf.lower().endswith(".pdf"):
            self._send(400, self._json({"ok": False, "error": "请选择 PDF 文件"}))
            return
        if not os.path.isfile(pdf):
            self._send(400, self._json({"ok": False, "error": f"文件不存在：{pdf}"}))
            return
        dpi = body.get("dpi")
        if dpi is not None and (type(dpi) is not int or not 0 <= dpi <= 4):
            self._send(400, self._json({"ok": False, "error": "dpi 仅支持 0-4"}))
            return
        model = body.get("model") or cfg.get("selected_model")
        if model not in (cfg.get("model_choices") or {}):
            self._send(400, self._json({"ok": False, "error": f"未知模型：{model}"}))
            return
        engine = body.get("engine") or ""
        if engine not in ("", "llama", "vllm"):
            self._send(400, self._json({"ok": False, "error": "engine 仅支持 llama / vllm"}))
            return
        workers = body.get("workers")
        if workers is not None and (type(workers) is not int or workers < 1):
            self._send(400, self._json({"ok": False, "error": "workers 必须 >= 1"}))
            return
        timeout = body.get("timeout")
        if timeout is not None and (type(timeout) is not int or timeout < 1):
            self._send(400, self._json({"ok": False, "error": "timeout 必须 >= 1"}))
            return
        thinking = body.get("thinking")
        if thinking is not None and not isinstance(thinking, bool):
            self._send(400, self._json({"ok": False, "error": "thinking 必须是布尔值"}))
            return
        for key in ("title", "author", "out_dir", "epub_path"):
            val = body.get(key)
            if val is not None and not isinstance(val, str):
                self._send(400, self._json({"ok": False, "error": f"{key} 必须是字符串"}))
                return
        exclude = body.get("exclude")
        if exclude is not None and not isinstance(exclude, str):
            self._send(400, self._json({"ok": False, "error": "exclude 必须是字符串"}))
            return
        lang = body.get("lang") or "zh-CN"
        # -- 单飞：已有转换在运行则拒绝 --
        st = self.server.state
        cv = st["convert"]
        with cv["lock"]:
            if cv["running"]:
                self._send(409, self._json({"ok": False, "error": "已有转换在运行"}))
                return
            cv["lines"] = []
            cv["done"] = False
            cv["success"] = False
            cv["exit_code"] = None
            cv["epub_path"] = None
            cv["error"] = None
            cv["prompt"] = None
            cv["running"] = True
        argv = _convert_argv(
            pdf,
            dpi=dpi,
            model=model,
            engine=engine or None,
            workers=workers,
            timeout=timeout,
            thinking=bool(thinking),
            title=body.get("title"),
            author=body.get("author"),
            lang=lang,
            out_dir=body.get("out_dir"),
            epub_path=body.get("epub_path"),
            exclude=exclude,
        )
        try:
            # 注入 PYTHONIOENCODING=utf-8：子进程 stdout 默认用系统编码（GBK），父进程按 utf-8 解码→乱码
            # （冻结 exe 实测忽略该变量，真正的编码修复在 mian.main() 内按 tty 判断 reconfigure；
            #   此处保留用于开发模式 python 子进程。PYTHONUNBUFFERED 关闭子进程块缓冲，
            #   避免管道下日志成块延迟到达、界面显示不完整）
            # PTOE_UI_PROMPT=1 + stdin=PIPE：子进程需要用户决策（OCR 断点续传
            # 选择）时打印 __PTOE_PROMPT__ 标记，GUI 弹窗后写回 stdin
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["PTOE_UI_PROMPT"] = "1"
            kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "env": env,
            }
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if flags:
                kwargs["creationflags"] = flags
            proc = subprocess.Popen(argv, **kwargs)
        except Exception as e:
            with cv["lock"]:
                cv["running"] = False
                cv["done"] = True
                cv["success"] = False
                cv["error"] = str(e)
            self._send(500, self._json({"ok": False, "error": f"启动转换失败：{e}"}))
            return
        with cv["lock"]:
            cv["proc"] = proc
        threading.Thread(
            target=_convert_monitor, args=(cv, proc), daemon=True
        ).start()
        self._send(
            200,
            self._json(
                {
                    "ok": True,
                    "message": "转换已启动",
                    "argv": argv,
                }
            ),
        )

    def _api_convert_status(self) -> None:
        """GET /api/convert/status：转换进度快照（运行中/完成/日志/结果/待答询问）。"""
        cv = self.server.state["convert"]
        with cv["lock"]:
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "running": cv["running"],
                        "done": cv["done"],
                        "success": cv["success"],
                        "exit_code": cv["exit_code"],
                        "lines": cv["lines"][-500:],
                        "error": cv["error"],
                        "epub_path": cv["epub_path"],
                        "prompt": cv["prompt"],
                    }
                ),
            )

    def _api_convert_prompt(self, body) -> None:
        """POST /api/convert/prompt：回答子进程的弹窗询问（选择写回子进程 stdin）。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        choice = body.get("choice")
        if not isinstance(choice, str) or not choice:
            self._send(400, self._json({"ok": False, "error": "缺少选择"}))
            return
        cv = self.server.state["convert"]
        with cv["lock"]:
            prompt = cv["prompt"]
            proc = cv.get("proc")
            if prompt is None:
                self._send(400, self._json({"ok": False, "error": "当前没有待回答的询问"}))
                return
            valid = [
                o.get("value")
                for o in (prompt.get("options") or [])
                if isinstance(o, dict) and o.get("value")
            ]
            if choice not in valid:
                self._send(400, self._json({"ok": False, "error": f"无效选择：{choice}"}))
                return
            if proc is None or proc.stdin is None or proc.poll() is not None:
                self._send(400, self._json({"ok": False, "error": "转换进程已退出"}))
                return
            stdin = proc.stdin
            cv["prompt"] = None
        try:
            stdin.write(choice + "\n")
            stdin.flush()
        except Exception as e:  # noqa: BLE001  子进程可能已退出
            self._send(500, self._json({"ok": False, "error": f"写入选择失败：{e}"}))
            return
        self._send(200, self._json({"ok": True, "message": f"已选择：{choice}"}))

    def _api_convert_stop(self) -> None:
        """POST /api/convert/stop：停止正在运行的转换（kill 子进程）。

        同时关闭子进程 stdin（解除其可能阻塞在弹窗询问上的等待）并立即标记
        完成——即使监控线程卡住（如子进程继承 stdout 管道），界面也能从
        「转换中」恢复；监控线程随后以真实退出码覆盖状态。
        """
        cv = self.server.state["convert"]
        with cv["lock"]:
            proc = cv.get("proc")
            if not cv["running"] or proc is None:
                self._send(400, self._json({"ok": False, "error": "没有正在运行的转换"}))
                return
            stdin = proc.stdin
        try:
            proc.kill()
        except Exception:  # noqa: BLE001  进程可能已退出，忽略
            pass
        try:
            if stdin is not None:
                stdin.close()
        except Exception:  # noqa: BLE001  管道可能已关闭，忽略
            pass
        with cv["lock"]:
            cv["running"] = False
            cv["done"] = True
            cv["success"] = False
            cv["error"] = "已手动停止"
            cv["prompt"] = None
        self._send(200, self._json({"ok": True, "message": "已请求停止"}))

    # -- 矫正界面 --

    def _api_correct_start(self, body) -> None:
        """POST /api/correct/start：子进程启动矫正界面。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        try:
            import configmanage
            cfg = configmanage.get_config(show_dialogs=False)
        except Exception as e:
            self._send(500, self._json({"ok": False, "error": str(e)}))
            return
        pdf = body.get("pdf")
        if pdf is not None and not isinstance(pdf, str):
            self._send(400, self._json({"ok": False, "error": "pdf 必须是字符串"}))
            return
        if pdf and not os.path.isfile(pdf):
            self._send(400, self._json({"ok": False, "error": f"文件不存在：{pdf}"}))
            return
        engine = body.get("engine") or ""
        if engine not in ("", "llama", "vllm"):
            self._send(400, self._json({"ok": False, "error": "engine 仅支持 llama / vllm"}))
            return
        for key in ("title", "author", "out_dir", "epub_path"):
            val = body.get(key)
            if val is not None and not isinstance(val, str):
                self._send(400, self._json({"ok": False, "error": f"{key} 必须是字符串"}))
                return
        lang = body.get("lang") or "zh-CN"
        correct_timeout = body.get("correct_timeout")
        if correct_timeout is not None and (type(correct_timeout) is not int or correct_timeout < 1):
            self._send(400, self._json({"ok": False, "error": "correct_timeout 必须 >= 1"}))
            return
        # 单飞：矫正或转换在运行则拒绝
        # 已有存活的矫正界面（如浏览器被关闭但服务仍在等待）→ 返回地址供前端恢复
        info = _read_correct_server_info()
        if info is not None:
            if _probe_correct_ui(info["port"]):
                url = f"http://127.0.0.1:{info['port']}/"
                self._send(
                    200,
                    self._json(
                        {
                            "ok": True,
                            "already_running": True,
                            "url": url,
                            "message": "矫正已在运行，已重新打开界面",
                        }
                    ),
                )
                return
            # 探测失败 = 记录已过期，清掉后照常启动
            try:
                p = _correct_server_info_path()
                if p is not None and p.exists():
                    p.unlink()
            except Exception:  # noqa: BLE001
                pass
        st = self.server.state
        cr = st["correct"]
        cv = st["convert"]
        with cr["lock"]:
            if cr["running"]:
                self._send(409, self._json({"ok": False, "error": "已有矫正在运行"}))
                return
        with cv["lock"]:
            if cv["running"]:
                self._send(409, self._json({"ok": False, "error": "已有转换在运行"}))
                return
        with cr["lock"]:
            cr["lines"] = []
            cr["done"] = False
            cr["success"] = False
            cr["exit_code"] = None
            cr["error"] = None
            cr["running"] = True
        argv = _correct_argv(
            pdf=pdf or None,
            engine=engine or None,
            title=body.get("title"),
            author=body.get("author"),
            lang=lang,
            out_dir=body.get("out_dir"),
            epub_path=body.get("epub_path"),
            correct_timeout=correct_timeout,
        )
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "env": env,
            }
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if flags:
                kwargs["creationflags"] = flags
            proc = subprocess.Popen(argv, **kwargs)
        except Exception as e:
            with cr["lock"]:
                cr["running"] = False
                cr["done"] = True
                cr["success"] = False
                cr["error"] = str(e)
            self._send(500, self._json({"ok": False, "error": f"启动矫正失败：{e}"}))
            return
        with cr["lock"]:
            cr["proc"] = proc
        threading.Thread(
            target=_convert_monitor, args=(cr, proc), daemon=True
        ).start()
        self._send(
            200,
            self._json({"ok": True, "message": "矫正已启动", "argv": argv}),
        )

    def _api_correct_status(self) -> None:
        """GET /api/correct/status：矫正进度快照。"""
        cr = self.server.state["correct"]
        with cr["lock"]:
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "running": cr["running"],
                        "done": cr["done"],
                        "success": cr["success"],
                        "exit_code": cr["exit_code"],
                        "lines": cr["lines"][-500:],
                        "error": cr["error"],
                    }
                ),
            )

    def _api_correct_stop(self) -> None:
        """POST /api/correct/stop：停止正在运行的矫正（kill 子进程）。"""
        cr = self.server.state["correct"]
        with cr["lock"]:
            proc = cr.get("proc")
            if not cr["running"] or proc is None:
                self._send(400, self._json({"ok": False, "error": "没有正在运行的矫正"}))
                return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self._send(200, self._json({"ok": True, "message": "已请求停止"}))

    # -- 工具：多 EPUB 合并 --

    def _api_tools_merge_start(self, body) -> None:
        """POST /api/tools/merge/start：后台线程合并多 EPUB。"""
        if not isinstance(body, dict):
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        # -- 参数校验（中文错误） --
        paths = body.get("paths")
        if not isinstance(paths, list) or len(paths) < 2:
            self._send(400, self._json({"ok": False, "error": "请至少选择 2 个 EPUB 文件"}))
            return
        for p in paths:
            if not isinstance(p, str) or not p:
                self._send(400, self._json({"ok": False, "error": "路径列表包含非法项"}))
                return
            if not p.lower().endswith(".epub"):
                self._send(400, self._json({"ok": False, "error": "仅支持 .epub 文件：" + p}))
                return
            if not os.path.isfile(p):
                self._send(400, self._json({"ok": False, "error": "文件不存在：" + p}))
                return
        for key in ("title", "author", "lang", "out_path"):
            val = body.get(key)
            if val is not None and not isinstance(val, str):
                self._send(400, self._json({"ok": False, "error": key + " 必须是字符串"}))
                return
        # -- 单飞：已有合并在运行则拒绝 --
        st = self.server.state
        mg = st["merge"]
        with mg["lock"]:
            if mg["running"]:
                self._send(409, self._json({"ok": False, "error": "已有合并任务在运行"}))
                return
            mg["lines"] = []
            mg["done"] = False
            mg["success"] = False
            mg["error"] = None
            mg["out_path"] = None
            mg["stop_event"].clear()
            mg["running"] = True
        threading.Thread(
            target=_merge_worker,
            args=(
                mg,
                paths,
                body.get("title"),
                body.get("author"),
                body.get("lang"),
                body.get("out_path"),
            ),
            daemon=True,
        ).start()
        self._send(200, self._json({"ok": True, "message": "合并已启动"}))

    def _api_tools_merge_status(self) -> None:
        """GET /api/tools/merge/status：合并进度快照。"""
        mg = self.server.state["merge"]
        with mg["lock"]:
            self._send(
                200,
                self._json(
                    {
                        "ok": True,
                        "running": mg["running"],
                        "done": mg["done"],
                        "success": mg["success"],
                        "lines": mg["lines"][-500:],
                        "error": mg["error"],
                        "out_path": mg["out_path"],
                    }
                ),
            )

    def _api_tools_merge_stop(self) -> None:
        """POST /api/tools/merge/stop：请求停止合并（当前章节完成后才会停止）。"""
        mg = self.server.state["merge"]
        with mg["lock"]:
            if not mg["running"]:
                self._send(400, self._json({"ok": False, "error": "没有正在运行的合并"}))
                return
            mg["stop_event"].set()
        self._send(
            200,
            self._json(
                {"ok": True, "message": "已请求停止（当前章节完成后才会停止）"}
            ),
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if body is _BAD_JSON:
            self._send(400, self._json({"ok": False, "error": "无效的 JSON"}))
            return
        if path == "/api/config":
            self._api_config_post(body)
            return
        if path == "/api/server/start":
            self._api_server_start(body)
            return
        if path == "/api/server/stop":
            self._api_server_stop()
            return
        if path == "/api/pick":
            self._api_pick(body)
            return
        if path == "/api/bye":
            self._api_bye()
            return
        if path == "/api/convert/start":
            self._api_convert_start(body)
            return
        if path == "/api/convert/prompt":
            self._api_convert_prompt(body)
            return
        if path == "/api/convert/stop":
            self._api_convert_stop()
            return
        if path == "/api/correct/start":
            self._api_correct_start(body)
            return
        if path == "/api/correct/stop":
            self._api_correct_stop()
            return
        if path == "/api/tools/merge/start":
            self._api_tools_merge_start(body)
            return
        if path == "/api/tools/merge/stop":
            self._api_tools_merge_stop()
            return
        self._send(404, self._json({"ok": False, "error": "未找到"}))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def gui_serve(
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    idle_timeout: int = 120,
) -> None:
    """启动 HTML 配置操作界面并阻塞，直到浏览器关闭（或 Ctrl+C）。

    - host/port：监听地址与端口（port=0 自动分配，实际端口见启动打印）；
    - open_browser：True 时自动用默认浏览器打开界面；
    - idle_timeout：浏览器关闭（pagehide 信标）后等待的秒数，超过即自动退出；
      心跳（/api/ping）失联超过 idle_timeout*2 秒同样视为浏览器已关闭。
    """
    state: dict = {
        "finished": threading.Event(),
        "dlg_queue": queue.Queue(),
        "dlg_lock": threading.Lock(),
        "serve_lock": threading.Lock(),
        "gone_at": None,
        "last_beat": time.monotonic(),
        "beat_lock": threading.Lock(),
        "last_error": None,
        "convert": {
            "lock": threading.Lock(),
            "proc": None,
            "lines": [],
            "running": False,
            "done": False,
            "success": False,
            "exit_code": None,
            "epub_path": None,
            "error": None,
            "prompt": None,
        },
        "correct": {
            "lock": threading.Lock(),
            "proc": None,
            "lines": [],
            "running": False,
            "done": False,
            "success": False,
            "exit_code": None,
            "error": None,
            "prompt": None,
        },
        "merge": {
            "lock": threading.Lock(),
            "lines": [],
            "running": False,
            "done": False,
            "success": False,
            "error": None,
            "out_path": None,
            "stop_event": threading.Event(),
        },
    }
    server = ThreadingHTTPServer((host, port), _GuiHandler)
    server.daemon_threads = True
    server.state = state
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"配置界面已启动: {url}（Ctrl+C 退出）")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001  打不开浏览器不阻断服务
            pass
    try:
        # 浏览器关闭监测：页面每 30s 发心跳（/api/ping）；关闭标签页时发
        # pagehide 信标（/api/bye）。信标确认关闭或心跳失联超过阈值后自动退出。
        # 心跳失联需连续 _STALE_CONFIRM_SECONDS 确认，防休眠唤醒误判。
        stale_since: float | None = None
        while not state["finished"].is_set():
            time.sleep(0.5)
            # 文件选择对话框只能在主线程弹出（tkinter 线程安全），逐轮取走
            # 队列里的请求弹框，阻塞直到用户选择/取消
            _drain_dialog_queue(state)
            gone, stale_since = _browser_gone(
                state, idle_timeout=idle_timeout, stale_since=stale_since
            )
            if gone:
                state["finished"].set()
                break
    except KeyboardInterrupt:
        state["finished"].set()
        print("\n配置界面已退出")
    finally:
        # Windows 下必须先 shutdown() 再 server_close()（顺序反了抛 WinError 10038）
        server.shutdown()
        server.server_close()
        serve_thread.join(timeout=5)
        # 唤醒可能阻塞在 /api/pick 上的请求（handler 返回 500）
        _abort_dialog_queue(state)
        # 兜底：转换子进程仍在运行时强制终止，避免残留进程占用端口/文件
        cv = state["convert"]
        with cv["lock"]:
            proc = cv.get("proc")
            if cv["running"] and proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001  进程可能已退出，忽略
                    pass
            try:
                if proc is not None and proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001  管道可能已关闭，忽略
                pass
            cv["prompt"] = None
        # 兜底：矫正子进程仍在运行时强制终止
        cr = state["correct"]
        with cr["lock"]:
            proc = cr.get("proc")
            if cr["running"] and proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 兜底：合并任务仍在运行时请求停止（引擎自行检查 stop_event）
        mg = state["merge"]
        with mg["lock"]:
            mg["stop_event"].set()
            mg["running"] = False
            mg["done"] = True
            mg["success"] = False
            mg["error"] = mg["error"] or "服务已关闭"