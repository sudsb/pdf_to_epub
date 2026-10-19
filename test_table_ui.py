"""表格模块可靠性重构（2026-10）静态不变量测试。

回归动机（用户反馈三条）：
1. 拖拽列宽/行高时尺寸暴涨、无法控制；
2. 「加行/加列」偶发点了没反应（且行列增删存在重复/不一致实现）；
3. 表格内选中文字时，选中菜单与表格操作条重叠冲突；
另新增能力：表格存在个别缺失单元格时可通过「修复缺失单元格」补齐。

本测试对 ui/app.js 与 correctmanage.py 做静态断言（无浏览器运行时），锁定：
A. 行列增删只有一个网格感知实现（无重复定义、无旧启发式实现）；
B. 尺寸拖拽为「起手快照 + 增量」模式，杜绝读取已改 DOM 再叠加的累加式暴涨；
C. 选中菜单在表格内叠于操作条上方并留间距，且两者点击互不关闭；
D. 残表修复函数/菜单项/分派齐备；
E. 浮层层级：#popup 高于 #tblBar。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestTableRefactor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cm = _read("correctmanage.py")

    # ---- A. 行列增删：唯一且网格感知 ----
    def test_row_col_ops_have_single_definition(self):
        for name in ("_tblAddRow", "_tblAddCol", "_tblDelRow", "_tblDelCol"):
            n = len(re.findall(r"function %s\s*\(" % re.escape(name), self.js))
            self.assertEqual(n, 1, "%s 定义次数应为 1，实际 %d（存在重复定义）" % (name, n))

    def test_add_row_col_delegate_to_grid_impl(self):
        self.assertIn("function _tblAddRow() { _tblInsertRow('below'); }", self.js)
        self.assertIn("function _tblAddCol() { _tblInsertCol('right'); }", self.js)

    def test_no_old_index_based_add_col(self):
        # 旧实现用 children[indexOf] 定位，遇 colspan/rowspan 会错位，必须已移除
        self.assertNotIn("const cellIdx = curTr ? Array.prototype.indexOf.call(curTr.children, _tblKey.cell) : 0;", self.js)

    def test_tblbar_buttons_preserve_selection(self):
        # 保住编辑区选区（否则 mouseup 会置空 _tblKey → 点击静默失效）
        self.assertIn(
            "if (tblBar) tblBar.addEventListener('mousedown', function (e) { e.preventDefault(); });",
            self.js,
        )
        self.assertIn("_tblMenuEl.addEventListener('mousedown', function (e) { e.preventDefault(); });", self.js)
        self.assertIn("function _tblInOwnUI(el) {", self.js)
        self.assertIn("if (_tblInOwnUI(e.target)) return;", self.js)

    def test_align_grid_popup_and_range_targets(self):
        # 3×3 对齐九宫格（2026-10）：一次设置水平+垂直；有跨格选区时作用于整个选区
        self.assertIn("let _tblAlignGridEl = null;", self.js)
        for fn in ("_ensureTblAlignGrid", "_tblAlignGridOpen", "_tblAlignGridClose",
                   "_tblSyncAlignGridActive", "_tblTargetCells", "_tblVAlignOf",
                   "_tblVAlignClassOf"):
            self.assertIn("function %s(" % fn, self.js, "缺函数 " + fn)
        self.assertIn("_tblDoOp('aligngrid:' + b.dataset.tblAgH + ':' + b.dataset.tblAgV);", self.js)
        self.assertIn("op === 'aligngrid'", self.js)
        # 操作条「对齐 ▾」按钮 + 九宫格容器，且九宫格层级高于「更多」菜单
        self.assertIn('data-tbl-op="aligngrid"', self.cm)
        self.assertIn('id="tblAlignGrid"', self.cm)
        m_grid = re.search(r"#tblAlignGrid\{position:fixed;z-index:(\d+)", self.cm)
        m_menu = re.search(r"#tblMenu\{position:fixed;z-index:(\d+)", self.cm)
        self.assertIsNotNone(m_grid, "未找到 #tblAlignGrid z-index")
        self.assertIsNotNone(m_menu, "未找到 #tblMenu z-index")
        self.assertGreater(int(m_grid.group(1)), int(m_menu.group(1)),
                           "九宫格层级须高于「更多」菜单")

    def test_table_rows_participate_in_line_numbers(self):
        # 表格行也显示行号（连续编号）：CSS 计数器给 tr 计数，命令行口径一致
        self.assertIn(".editable > table tr{counter-increment:ptoe-ln;}", self.cm)
        self.assertIn("c.querySelectorAll('tr')", self.js)
        self.assertIn("const tr = n.closest('tr');", self.js)


    # ---- B. 尺寸拖拽：快照 + 增量 ----
    def test_resize_helpers_exist(self):
        self.assertIn("function _tblSnapshotColPcts(table) {", self.js)
        self.assertIn("function _tblApplyColPcts(table, pcts) {", self.js)
        self.assertIn("function _tblRowHeightsPx(table) {", self.js)
        self.assertIn("function _tblGridWidthAll(grid) {", self.js)

    def test_resize_uses_snapshot_not_live_readback(self):
        # 旧实现在每帧重读被改写的 right/leftEdge 再叠加 delta → 指数暴涨，必须消失
        self.assertNotIn("((right - leftEdge + delta) / origin) * 100", self.js)
        self.assertNotIn("Math.max(8, curH + delta)", self.js)
        # 新实现：起手抓一次快照，之后仅快照 + delta
        self.assertIn("const snapCols = isCol ? _tblSnapshotColPcts(table) : null;", self.js)
        self.assertIn("const snapRows = isCol ? null : _tblRowHeightsPx(table);", self.js)
        self.assertIn("snapRows[idx] + delta", self.js)
        self.assertIn("_tblApplyColPcts(table, next);", self.js)
        # 相邻两列总占比守恒
        self.assertIn("const pair = snapCols[idx] + snapCols[idx + 1];", self.js)

    # ---- C. 浮层错开且互不冲突 ----
    def test_popup_stacks_above_tblbar(self):
        self.assertIn("const barVisible = _tblBarVisible && tblBar && tblBar.style.display === 'flex';", self.js)
        self.assertIn("const above = br.top - r.height - GAP;", self.js)
        self.assertIn("const GAP = 8;", self.js)

    def test_clicking_floating_layers_does_not_close_each_other(self):
        # 点击 操作条/更多菜单/对齐九宫格/选中菜单 任一都不应关闭表格操作条。
        # 2026-10 修复：守卫原先逐条列举 #tblBar/#tblMenu/#popup，漏了新上线的
        # #tblAlignGrid —— 点九宫格选项时 mousedown 先触发 hideTblBar() 把 _tblKey
        # 清空，随后 click 的 _tblDoOp 在入口守卫处静默返回，9 个位置全部「点了没反应」。
        # 改为统一走 _tblInOwnUI（单一事实源），以后每加一个表格浮层都不会再漏。
        self.assertIn(
            "if (_tblKey && !_tblInOwnUI(e.target) && !e.target.closest('.editable td,.editable th')) hideTblBar();",
            self.js,
            "「点击浮层外 → 收起操作条」守卫未覆盖全部表格浮层（九宫格/手柄层/尺寸弹窗）",
        )
        # _tblInOwnUI 的白名单必须含 #tblAlignGrid（否则点九宫格仍会误关操作条）
        self.assertIn(
            "'#tblBar,#tblMenu,#tblAlignGrid,#tblResizeLayer,#tblSizeModalBg,#popup'",
            self.js,
            "_tblInOwnUI 白名单缺 #tblAlignGrid",
        )

    # ---- D. 残表修复 ----
    def test_repair_function_menu_and_dispatch(self):
        self.assertIn("function _tblRepairCells(table) {", self.js)
        self.assertIn("{ op: 'repair', label: '修复缺失单元格' },", self.js)
        self.assertIn("run('表格-修复缺失单元格'", self.js)
        self.assertIn("_tblInsertIdxForCol(tr, c)", self.js)

    # ---- E. 层级 ----
    def test_popup_above_tblbar_zindex(self):
        m_popup = re.search(r"#popup\{position:fixed;z-index:(\d+)", self.cm)
        m_bar = re.search(r"#tblBar\{position:fixed;z-index:(\d+)", self.cm)
        self.assertIsNotNone(m_popup, "未找到 #popup z-index")
        self.assertIsNotNone(m_bar, "未找到 #tblBar z-index")
        self.assertGreater(
            int(m_popup.group(1)), int(m_bar.group(1)),
            "#popup 层级必须高于 #tblBar（选中菜单叠在操作条之上）",
        )


class TestTableConvertOp(unittest.TestCase):
    """「转成表格」弹出菜单项（手动把选中文字转成原生表格）的静态不变量。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cfg = _read("configmanage.py")

    def _has(self, text, needle, label):
        # 用 assertTrue（而非 assertIn）避免失败时把整个万行文件打进报告
        self.assertTrue(needle in text, "%s：未找到 %r" % (label, needle))

    def test_op_registered_in_ops_and_icon(self):
        self._has(self.js, "['table','转成表格']", "app.js OPS")
        self._has(self.js, "table:'表'", "app.js OP_ICON")

    def test_eligible_in_both_pools(self):
        m = re.search(r"const POPUP_ELIGIBLE_OPS = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(m, "未找到 js POPUP_ELIGIBLE_OPS")
        self._has(m.group(1), "'table'", "app.js POPUP_ELIGIBLE_OPS")
        m2 = re.search(r"POPUP_ELIGIBLE_OPS = \[(.*?)\]", self.cfg, re.S)
        self.assertIsNotNone(m2, "未找到 configmanage POPUP_ELIGIBLE_OPS")
        self._has(m2.group(1), '"table"', "configmanage POPUP_ELIGIBLE_OPS")

    def test_applyop_dispatches_to_handler_and_endpoint(self):
        self._has(self.js, "op === 'table'", "applyOp 分派")
        self._has(self.js, "function convertSelectionToTable(", "转换函数")
        self._has(self.js, "/api/table_convert", "端点调用")


class TestTableConvertUiEntry(unittest.TestCase):
    """「转成表格」的三个 UI 入口（2026-10）：工具栏按钮 / 悬浮菜单默认项 / 设置开关。

    两处 HTML（correctmanage._UI_HTML 与抽取副本 ui_full.html）必须同步，否则
    用户看到的按钮与静态测试读到的副本会不一致。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cfg = _read("configmanage.py")
        cls.cm = _read("correctmanage.py")
        cls.html = _read("ui_full.html")

    def _has(self, text, needle, label):
        self.assertTrue(needle in text, "%s：未找到 %r" % (label, needle))

    def test_toolbar_button_in_both_htmls(self):
        btn_id = 'id="tableConvertBtn"'
        self._has(self.html, btn_id, "ui_full.html 工具栏按钮")
        self._has(self.cm, btn_id, "correctmanage._UI_HTML 工具栏按钮")
        # data-op 让按钮自动落入 #toolbar 通用绑定（点击 → applyOp('table')）
        self.assertRegex(
            self.html, r'id="tableConvertBtn"[^>]*data-op="table"', "ui_full.html data-op"
        )
        self.assertRegex(
            self.cm, r'id="tableConvertBtn"[^>]*data-op="table"', "correctmanage data-op"
        )
        # 保住编辑区选区：靠 mousedown preventDefault（与既有工具栏按钮一致）
        self.assertRegex(
            self.html,
            r'id="tableConvertBtn"[^>]*onmousedown="event\.preventDefault\(\)"[^>]*转成表格',
            "ui_full.html 按钮 mousedown 防夺焦",
        )

    def test_popup_row2_default_contains_table(self):
        m = re.search(r"POPUP_ROW2_DEFAULT = \[(.*?)\]", self.cfg, re.S)
        self.assertIsNotNone(m, "未找到 configmanage POPUP_ROW2_DEFAULT")
        self._has(m.group(1), '"table"', "configmanage POPUP_ROW2_DEFAULT")
        self.assertRegex(
            self.js,
            r"popup_row2: \[[^\]]*'table'",
            "app.js UI_SETTINGS_DEFAULTS.popup_row2 应含 'table'",
        )

    def test_settings_checkbox_in_both_htmls(self):
        box = 'id="tableConvertChk"'
        self._has(self.html, box, "ui_full.html 设置复选框")
        self._has(self.cm, box, "correctmanage._UI_HTML 设置复选框")

    def test_settings_checkbox_wired_to_api_config(self):
        self._has(self.js, "function saveTableConvert()", "保存函数")
        self._has(self.js, "getElementById('tableConvertChk')", "复选框读取")
        self._has(self.js, "table_convert: !!", "POST 载荷")
        # GET /api/config 的 table_convert 必须回填复选框（否则初值永远未勾选）
        self._has(self.js, "res.table_convert === 'boolean'", "GET 回填类型判断")

    def test_backend_config_api_supports_table_convert(self):
        # GET 下发默认 True（与 stringmanage 回退规则一致）、POST 校验布尔值
        self._has(self.cm, '"table_convert": bool(cfg.get("table_convert", True))', "GET 默认")
        self._has(self.cm, "table_convert 必须是布尔值", "POST 校验")
        self._has(self.cm, 'update_config("table_convert"', "POST 写回")


class TestTableToTextUiEntry(unittest.TestCase):
    """「表格转文本」（2026-10）：工具栏 / 悬浮菜单入口与前后端接线。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cfg = _read("configmanage.py")
        cls.cm = _read("correctmanage.py")
        cls.html = _read("ui_full.html")

    def _has(self, text, needle, label):
        self.assertTrue(needle in text, "%s：未找到 %r" % (label, needle))

    def test_op_registered(self):
        self._has(self.js, "['table2text','表格转文本']", "app.js OPS")
        self._has(self.js, "table2text:'文'", "app.js OP_ICON")
        self._has(self.js, "table2text: '表格转文本", "app.js OP_TIP")

    def test_eligible_in_both_pools(self):
        m = re.search(r"const POPUP_ELIGIBLE_OPS = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(m, "未找到 js POPUP_ELIGIBLE_OPS")
        self._has(m.group(1), "'table2text'", "app.js POPUP_ELIGIBLE_OPS")
        m2 = re.search(r"POPUP_ELIGIBLE_OPS = \[(.*?)\]", self.cfg, re.S)
        self.assertIsNotNone(m2, "未找到 configmanage POPUP_ELIGIBLE_OPS")
        self._has(m2.group(1), '"table2text"', "configmanage POPUP_ELIGIBLE_OPS")

    def test_applyop_dispatches_to_handler_and_endpoint(self):
        self._has(self.js, "op === 'table2text'", "applyOp 分派")
        self._has(self.js, "function convertTablesToText(", "转换函数")
        self._has(self.js, "/api/table_to_text", "端点调用")
        self._has(self.cm, '"/api/table_to_text"', "服务端端点")

    def test_toolbar_button_in_both_htmls(self):
        btn = 'id="tableToTextBtn"'
        self._has(self.html, btn, "ui_full.html 工具栏按钮")
        self._has(self.cm, btn, "correctmanage._UI_HTML 工具栏按钮")
        self.assertRegex(
            self.html, r'id="tableToTextBtn"[^>]*data-op="table2text"', "ui_full.html data-op"
        )
        self.assertRegex(
            self.cm, r'id="tableToTextBtn"[^>]*data-op="table2text"', "correctmanage data-op"
        )
        self.assertRegex(
            self.html,
            r'id="tableToTextBtn"[^>]*onmousedown="event\.preventDefault\(\)"[^>]*表格转文本',
            "ui_full.html 按钮 mousedown 防夺焦",
        )

    def test_popup_row2_default_contains_table2text(self):
        m = re.search(r"POPUP_ROW2_DEFAULT = \[(.*?)\]", self.cfg, re.S)
        self.assertIsNotNone(m, "未找到 configmanage POPUP_ROW2_DEFAULT")
        self._has(m.group(1), '"table2text"', "configmanage POPUP_ROW2_DEFAULT")
        self.assertRegex(
            self.js,
            r"popup_row2: \[[^\]]*'table2text'",
            "app.js UI_SETTINGS_DEFAULTS.popup_row2 应含 'table2text'",
        )

    def test_writes_plain_text_lines(self):
        # 回写必须是纯文本行（textContent），否则管道符/尖括号会被当 HTML 解释
        self._has(self.js, "d.textContent = lines[m]", "纯文本行构建")

    def test_backend_endpoint_registered(self):
        self._has(self.cm, "table_html_to_pipe_text", "服务端转换函数")

    def test_reverse_round_trip_hint(self):
        # 与「转成表格」互为反向：提示里应点明可再转回（幂等往返）
        self._has(self.js, "table2text: '表格转文本", "OP_TIP 反向说明")


class TestApplyAfterProofreadTableConvert(unittest.TestCase):
    """重识别/校正「应用」后自动补跑表格转换（2026-10 修复）。

    应用纠错是纯前端 DOM 编辑，服务端 initial_html 的自动档不会跑；此处用
    /api/table_convert 的 lines 模式（返回 chunks）补齐。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cm = _read("correctmanage.py")

    def _has(self, text, needle, label):
        self.assertTrue(needle in text, "%s：未找到 %r" % (label, needle))

    def test_auto_convert_hooked_into_apply_paths(self):
        self._has(self.js, "async function _autoConvertPageTables(", "补跑函数")
        self._has(self.js, "let _autoTableConvert = true;", "全局开关")
        self.assertRegex(
            self.js,
            r"histRun\('应用纠错', \[i\][\s\S]{0,900}_autoConvertPageTables\(",
            "单页应用接线",
        )
        self._has(self.js, "conv += await _autoConvertPageTables(k);", "全部应用接线")
        self._has(self.js, "_autoTableConvert = res.table_convert;", "GET 同步开关")
        self._has(self.js, "_autoTableConvert = !!el.checked;", "设置变更即时生效")

    def test_lines_mode_chunks_contract(self):
        self._has(self.js, "body: JSON.stringify({ lines: lines })", "lines 模式请求")
        self._has(self.js, "res.chunks", "chunks 读取")
        self._has(self.js, "'\\u0001'", "含纠错标注行哨兵")
        self._has(self.cm, '"lines"', "服务端 lines 分支")
        self._has(self.cm, "convert_lines(", "服务端 chunks 计算")


class TestTableSizeLimits(unittest.TestCase):
    """表格行/列上限放宽（2026-10：原 20 行上限挡住省份表这类长表格）。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cm = _read("correctmanage.py")
        cls.html = _read("ui_full.html")

    def _has(self, text, needle, label):
        self.assertTrue(needle in text, "%s：未找到 %r" % (label, needle))

    def test_dialog_max_in_both_htmls(self):
        for label, text in (("ui_full.html", self.html), ("correctmanage._UI_HTML", self.cm)):
            self._has(
                text, 'id="tableRows" value="4" min="1" max="200"', label + " 行数上限"
            )
            self._has(
                text, 'id="tableCols" value="5" min="1" max="50"', label + " 列数上限"
            )
            self.assertTrue(
                'id="tableRows" value="4" min="1" max="20"' not in text,
                label + " 仍残留 20 行上限",
            )

    def test_js_clamp_and_toolbar_limits_relaxed(self):
        self._has(self.js, "const TBL_MAX_ROWS = 200, TBL_MAX_COLS = 50;", "对话框钳制上限")
        self._has(self.js, "const TBL_MAX_R = 200, TBL_MAX_C = 50;", "表格操作条上限")
        self.assertRegex(self.js, r"if \(v > max\) v = max;", "clamp 用上限变量")


class TestTableDragLeftButtonOnly(unittest.TestCase):
    """表格拖拽只认鼠标左键（2026-10 用户反馈：中键/右键也能拖）。

    编辑器里真算「拖拽」的只有两处：尺寸手柄（列宽/行高）与单元格区域拖选。
    两处都必须只认 button === 0 起手，且拖拽中一旦左键松开/按下中·右键即冻结。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))

    def _slice(self, a, b):
        i = self.js.index(a)
        j = self.js.index(b, i)
        return self.js[i:j]

    def test_resize_handle_requires_left_button(self):
        body = self._slice(
            "_tblResizeLayer.addEventListener('mousedown'", "function onMove(e2) {"
        )
        self.assertIn("if (ev.button !== 0) return;", body, "尺寸手柄 mousedown 未过滤按键")

    def test_resize_drag_freezes_without_left_button(self):
        body = self._slice("function onMove(e2) {", "function onUp(e2) {")
        self.assertIn("if (e2.buttons !== 1) return;", body, "尺寸拖拽中未锁左键")

    def test_resize_mouseup_ignores_other_buttons(self):
        body = self._slice(
            "function onUp(e2) {", "document.removeEventListener('mousemove', onMove);"
        )
        self.assertIn("if (e2 && e2.button !== 0) return;", body, "mouseup 未过滤按键")

    def test_cell_range_drag_requires_left_button(self):
        body = self._slice(
            "if (mdMode || !e.target || !e.target.closest) return;", "const hit = _tblCellOfNode(e.target);"
        )
        self.assertIn("if (e.button !== 0) return;", body, "单元格拖选 mousedown 未过滤按键")
        # 自家浮层（含对齐九宫格）不起手拖选：否则点九宫格选项会顺手把多格矩形
        # 选区（_tblSel）清空，对齐就只作用于光标那一格而非整个选区。
        self.assertIn("if (_tblInOwnUI(e.target)) return;", body,
                      "单元格拖选未排除自家浮层（点九宫格会清空多格选区）")


class TestTableNoborderAndColumnsUi(unittest.TestCase):
    """无边框表格 + 分栏 + 表格内浮层菜单开关（2026-10）静态契约。

    锁定四条链路：
    A. 无边框：表格菜单开关 → class="ptoe-noborder" → 编辑 CSS / 保存白名单 /
       EPUB CSS / DOCX 边框同步；
    B. 分栏：工具栏「分栏」按钮 → applyOp('columns') → convertSelectionToColumns，
       表格菜单「表格转段落」可还原；
    C. 表格内浮层菜单：设置弹窗三个复选框 → ui_settings 三键（默认关）→
       选中菜单 / 右键菜单 / 表格菜单触发点门控（只影响表格）；
    D. 两处 HTML（correctmanage._UI_HTML 与 ui_full.html 副本）同步。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        cls.cm = _read("correctmanage.py")
        cls.html = _read("ui_full.html")
        cls.cfg = _read("configmanage.py")
        cls.hm = _read("htmlmanage.py")

    def test_noborder_toggle_frontend(self):
        self.assertIn("const TBL_NOBORDER_CLASS = 'ptoe-noborder'", self.js)
        self.assertIn("function _tblToggleNoborder(", self.js)
        self.assertIn("function _tblHasNoborder(", self.js)
        # 菜单项 + 激活态 + 分派
        self.assertIn("{ op: 'noborder', label: '不显示边框' }", self.js)
        self.assertIn("it.op === 'noborder' && _tblHasNoborder(_tblKey.table)", self.js)
        self.assertIn("op === 'noborder'", self.js)
        # 两处 HTML 的编辑区 CSS 同步（勿单边改动）
        for name, html in (("correctmanage", self.cm), ("ui_full", self.html)):
            self.assertIn(
                ".editable table.ptoe-noborder th,.editable table.ptoe-noborder td{border:none;}",
                html,
                "%s 编辑区无边框 CSS 缺失" % name,
            )

    def test_noborder_backend(self):
        # 保存白名单（表格级 class 只放行 ptoe-noborder）
        self.assertIn('TBL_TABLE_CLASSES = ("ptoe-noborder",)', self.cm)
        self.assertIn('_tbl_table_attrs', self.cm)
        # EPUB CSS
        self.assertIn("table.ptoe-noborder th, table.ptoe-noborder td {", self.hm)
        # DOCX：六向 none + 调用点传 noborder
        self.assertIn("noborder: bool = False", self.cm)
        self.assertIn('noborder=bool(block.get("noborder"))', self.cm)
        self.assertIn('self.tbl_noborder = "ptoe-noborder" in str(dict(attrs).get("class") or "").split()', self.cm)

    def test_columns_entry_and_reversal(self):
        # 工具栏「分栏」按钮：两处 HTML 同步 + data-op 走通用绑定
        for name, html in (("correctmanage", self.cm), ("ui_full", self.html)):
            self.assertRegex(
                html, r'id="colsBtn"[^>]*data-op="columns"', "%s 分栏按钮" % name
            )
        self.assertIn("if (op === 'columns') { convertSelectionToColumns(); return; }", self.js)
        self.assertIn("function convertSelectionToColumns()", self.js)
        self.assertIn("function buildColumnsTable(", self.js)
        # 分栏表 = 无边框 + fixed + 左窄右宽
        self.assertIn("const COLS_LEFT_PCT = 25;", self.js)
        self.assertIn("const COLS_RIGHT_PCT = 75;", self.js)
        self.assertIn("table.className = TBL_NOBORDER_CLASS;", self.js)
        # 2026-10 修复：左栏垂直居中走 ptoe-vmid class；inline vertical-align 会压过
        # 对齐 class（顶端/底端永不生效），且九宫格只认 class → 恒显左上。
        self.assertIn("ltd.classList.add('ptoe-vmid');", self.js)
        self.assertNotIn("ltd.style.verticalAlign = 'middle';", self.js)
        # 还原：表格菜单「表格转段落」
        self.assertIn("{ op: 'uncolumn', label: '表格转段落' }", self.js)
        self.assertIn("function _tblTableToParagraphs()", self.js)
        self.assertIn("op === 'uncolumn'", self.js)

    def test_in_table_menu_settings(self):
        # 设置弹窗三个复选框（两处 HTML 同步）
        for name, html in (("correctmanage", self.cm), ("ui_full", self.html)):
            for chk in ("tblPopupMenuChk", "tblCtxMenuChk", "tblTblMenuChk"):
                self.assertIn('id="%s"' % chk, html, "%s 缺 %s" % (name, chk))
        # 前端默认值 + 持久化镜像
        for key in ("tbl_popup_menu", "tbl_ctx_menu", "tbl_tbl_menu"):
            self.assertIn("%s: false" % key, self.js)
            self.assertIn("'ptoe_%s'" % key, self.js)
        # 触发点门控：选中菜单 / 表格菜单 / 通用右键菜单（独立控制，只影响表格）
        self.assertIn("_selectionTouchesTable(range, sel) && !uiBool('tbl_popup_menu', false)", self.js)
        self.assertIn("const wantTblMenu = uiBool('tbl_tbl_menu', false);", self.js)
        self.assertIn("const wantCtxMenu = uiBool('tbl_ctx_menu', false);", self.js)
        # 判定函数存在
        self.assertIn("function _selectionTouchesTable(", self.js)
        # 后端：GET 合并 + POST 布尔校验
        for key in ("tbl_popup_menu", "tbl_ctx_menu", "tbl_tbl_menu"):
            self.assertIn('"%s": False' % key, self.cfg)
            self.assertIn('"%s"' % key, self.cm)
        self.assertIn('for _tk in ("tbl_popup_menu", "tbl_ctx_menu", "tbl_tbl_menu"):', self.cm)


    def test_drag_paragraph_into_cell_preserves_format(self):
        # 2026-10-19：整段（p/div/h1-6）拖进表格单元格时，浏览器原生拖放会清掉块的
        # class/data-* → 段落设的「引用」等格式落格后显示成正文。前端自行接管：
        # dragstart 记录整块 HTML，drop 落到单元格时按原样插入。
        self.assertIn("let _dragBlkHtml = null;", self.js)
        self.assertIn("let _dragBlkNodes = null;", self.js)
        self.assertIn("function _dragWholeBlocks(ed) {", self.js)
        self.assertIn("e.dataTransfer.setData('text/html', _dragBlkHtml);", self.js)
        # 只接管「落进表格单元格」的场景，其余落点交回原生拖放
        self.assertIn("const cell = nodeEl && nodeEl.closest ? nodeEl.closest('td,th') : null;", self.js)
        self.assertIn("if (!cell || !dstEd.contains(cell)) return;", self.js)
        self.assertIn("range.createContextualFragment(_dragBlkHtml)", self.js)
        self.assertIn("histRun('拖入表格', pagesArr, function () {", self.js)
        # dragstart/drop/dragend 都要清状态，避免残留影响下一次拖拽
        self.assertEqual(self.js.count("_dragBlkHtml = null;"), 4)


if __name__ == "__main__":
    unittest.main()
