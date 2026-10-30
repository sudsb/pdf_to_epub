# -*- coding: utf-8 -*-
"""nvim 风格键位/命令模型回归护栏（2026-10-28，仅矫正 GUI / ui/app.js）。

覆盖的不变量：
  V1 默认 INSERT：脚本启动不改变既有编辑行为（VIM.mode 初值必须是 'INSERT'）。
  V2 捕获阶段单路由：NORMAL/VISUAL 键由 document 捕获阶段 onKeydown 统一接管，
     INSERT/CMDLINE 不拦截（用 preventDefault+stopPropagation 消费）。
  V3 IME 守卫：合成期间（e.isComposing / compositionstart）一律放行，中文输入不被吞。
  V4 ex 命令接线：:w/:wq/:q/:next/:prev/:g/:map/:s 均注册进 _CMD_BUILTINS 并带 run。
  V5 :s 页范围：默认当前页、N,M 页号范围、% / all 全局三种口径都在实现里。
  V6 模式指示条/样式从 JS 注入，不依赖 _UI_HTML/ui_full.html 双份同步。

静态断言永远执行；node 可用时额外跑 node --check。
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(ROOT, 'ui', 'app.js')


def read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


class VimStaticTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = read(APP_JS)
        cls.cm_html = read(os.path.join(ROOT, 'correctmanage.py'))
        cls.cfg = read(os.path.join(ROOT, 'configmanage.py'))
        # 只取新增模块，避免把既有 17k 行里的同名片段误判为通过
        marker = 'function initPtoeVimMode()'
        i = cls.src.index(marker)
        cls.mod = cls.src[i:]

    def test_v1_default_insert_mode(self):
        self.assertIn("mode: 'INSERT'", self.mod,
                      '默认必须是 INSERT（编辑）模式，启动不改变既有编辑行为')
        self.assertIn('window.__ptoeVimInstalled', self.mod,
                      '新增模块必须幂等安装，避免重复注册捕获监听')

    def test_v2_capture_phase_single_router(self):
        self.assertIn("document.addEventListener('keydown', onKeydown, true);", self.mod,
                      'NORMAL/VISUAL 键必须由捕获阶段单路由接管')
        self.assertIn('function onKeydown(e)', self.mod)
        self.assertIn('if (VIM.mode === \'INSERT\')', self.mod)
        self.assertIn('if (_cmdLineActive) return;', self.mod,
                      'CMDLINE 打开时必须让既有命令行处理，不得抢键')
        self.assertIn('e.preventDefault(); e.stopPropagation();', self.mod,
                      '消费键必须 preventDefault + stopPropagation，避免与既有监听双触发')

    def test_v3_ime_guard(self):
        self.assertIn('e.isComposing || VIM.composing', self.mod,
                      'IME 合成期间必须放行，否则中文输入被吞')
        self.assertIn("'compositionstart'", self.mod)
        self.assertIn("'compositionend'", self.mod)

    def test_v4_ex_commands_registered(self):
        for keys in ("['s']", "['w', 'write']", "['w!']", "['q', 'quit']",
                     "['q!']", "['wq']", "['qa', 'qa!']", "['next', 'n']",
                     "['prev', 'previous']", "['g']", "['map', 'nmap', 'leader']"):
            self.assertIn('keys: ' + keys, self.mod,
                          '缺少 ex 命令注册：' + keys)
        # 每个新命令都必须有 run（与真实动作关联），不能只有描述
        self.assertIn('run: function (ctx) { runSubstitute(ctx); }', self.mod)
        self.assertIn('await save()', self.mod)
        self.assertIn('confirmFinish()', self.mod)
        self.assertIn('_cmdSwitchPage(1)', self.mod)
        self.assertIn('_cmdSwitchPage(-1)', self.mod)
        self.assertIn('_cmdPageJump(ctx && ctx.args)', self.mod)
        # 路由顺序：_cmdExecSpec 必须先分发「带 run 的内置命令」，再走旧的行范围特判，
        # 否则新增的 :s 会被旧的 verb==='s' 分支抢走（页范围语义失效）。
        exec_spec = self.src[self.src.index('async function _cmdExecSpec'):]
        i_builtin = exec_spec.index('if (builtin && builtin.run)')
        i_s = exec_spec.index("verb === 's'")
        self.assertLess(i_builtin, i_s,
                        ':s 内置命令必须先于旧行范围特判分发')

    def test_v5_substitute_page_scope(self):
        self.assertIn('function pageNumberRange(', self.mod)
        self.assertIn('pageNumberRange(loc.line, loc.line2)', self.mod,
                      ':s 必须支持 N,M 页号范围')
        self.assertIn('loc && loc.all', self.mod, ':s 必须支持 % 全局')
        self.assertIn('idxs = [curPageIndex()]', self.mod,
                      ':s 无范围时必须默认当前页')
        self.assertIn('/^all(?![a-z0-9])/i', self.mod, ':s 必须支持 all 关键字全局')
        self.assertIn('inMathOrSvg(nodes[q])', self.mod,
                      ':s 必须跳过 MathML/SVG 文本，避免拆坏公式')

    def test_v6_indicator_injected_not_html(self):
        self.assertIn("st.id = 'vimModeStyles'", self.mod)
        self.assertIn("el.id = 'vimModeBar'", self.mod,
                      '模式指示条应由 JS 注入，避免 _UI_HTML/ui_full.html 双份同步')

    # ---- 2026-10-29 增量：翻页卡死 / 设置命令 / 纠错序号键位 ----

    def test_v7_page_move_anchor(self):
        """j/k 连按不得卡在同一页：翻页目标用锚点推进，不只依赖视口判定。"""
        self.assertIn('function pageBase()', self.mod)
        self.assertIn('ANCHOR_TTL', self.mod)
        self.assertIn('var base = pageBase();', self.mod,
                      'pageMove 必须以翻页锚点为基准，避免视口判定误差导致「翻不动」')
        self.assertIn('setAnchor(t)', self.mod)
        # 用户自己滚动/触摸/点击 → 锚点失效，不干扰手动浏览
        self.assertIn("'wheel', 'touchstart', 'mousedown'", self.mod)
        self.assertIn("window.addEventListener('scroll'", self.mod)
        # 跳转落点二次校正（前缀估算与实测行高不一致的根因修复）
        self.assertIn('row.getBoundingClientRect().top - toolbarOffset()', self.src,
                      'jumpToPageIndex 必须按实测位置校正落点')

    def test_v8_ctrl_bracket_to_normal(self):
        self.assertIn("e.key === '[' && e.ctrlKey", self.mod,
                      'Ctrl+[ 必须等价 Esc 进入 NORMAL（INSERT 与 NORMAL 两侧）')
        self.assertIn("if (k === '[')", self.mod)

    def test_v9_settings_command(self):
        self.assertIn('function openSettings()', self.src)
        self.assertIn('run: function () { openSettings(); }', self.mod,
                      'ex 命令 :st / :settings 必须真正打开设置面板')
        self.assertIn("keys: ['settings', 'st', '设置'", self.mod)
        self.assertIn("'st': 'settings'", self.src,
                      ':st 必须是设置（暂存改由 :sg）')
        self.assertIn("'sg': 'stage'", self.src)
        # NORMAL 和弦 st
        self.assertIn("case 's':", self.mod)
        self.assertIn("if (k === 't') { resetCount(); openSettings(); }", self.mod)

    def test_v10_count_g_jumps_original_page(self):
        # 5g = 5G = 跳到原书第 5 页；gg 仍是首屏
        self.assertIn('if (VIM.count) { var gn = VIM.count; resetCount(); consume(e); gotoPageNumber(gn); return; }',
                      self.mod)
        self.assertIn('if (n) gotoPageNumber(n); else lastPage();', self.mod)
        self.assertIn('function gotoPageNumber(', self.mod)

    def test_v11_error_navigation_keys(self):
        self.assertIn("case 'h': case 'l':", self.mod)
        self.assertIn('ptoeErrStep(k === \'l\' ? 1 : -1, hc)', self.mod)
        self.assertIn("case 'Enter':", self.mod)
        self.assertIn("case 'Delete':", self.mod)
        self.assertIn('ptoeErrApplyNo(ac || null, false)', self.mod)
        self.assertIn('ptoeErrApplyNo(dc || null, true)', self.mod)
        self.assertIn("case 'X': consume(e); resetCount(); clearErrPage(); return;", self.mod)
        self.assertIn('function clearErrPage()', self.mod)
        # 复用既有采纳/忽略按钮，撤销栈/反馈/标注重建口径一致
        self.assertIn("btn.click()", self.src)
        self.assertIn('ptoeErrApplyNo(a || null, false);', self.mod, 'ex :err 必须走同一套应用逻辑')
        self.assertIn('ptoeErrApplyNo(a || null, true);', self.mod, 'ex :errd 必须走同一套取消逻辑')

    def test_v12_error_numbering_does_not_touch_text(self):
        # 序号只写 data-err-no 属性 + CSS ::before 绝对定位 → 不产生文本节点
        self.assertIn("sEl.setAttribute('data-err-no'", self.src)
        self.assertIn('content:attr(data-err-no)', self.src)
        self.assertIn('position:absolute', self.mod,
                      '序号必须绝对定位，避免改变正文行高/排版')
        self.assertIn('function _prIdxFromNo(', self.src)
        self.assertIn('function _prLiveIdxList(', self.src)
        # 序号的用途是按序号应用/取消（不是显示）：编号口径只看状态，不看 DOM
        idx_from_no = self.src[self.src.index('function _prIdxFromNo('):]
        self.assertIn('_prLiveIdxList(i)', idx_from_no[:400],
                      '序号→条目必须按状态列表换算，关闭角标后仍可用')
        # 2026-10-30：序号是稳定编号（存进条目 err.no），采纳/忽略某处后其余序号不前移
        self.assertIn('function _prAssignStableNos(', self.src)
        self.assertIn('e.no = maxNo;', self.src, '新条目只能追加编号，绝不重排已有编号')
        self.assertIn('_prAssignStableNos(k);', self.src,
                      '每轮校正结果落盘时一次性分配编号（之后不再改）')
        self.assertIn("parseInt(errors[k].no, 10) === n", self.src,
                      '序号→条目必须按稳定编号查找，不再按序位')

    def test_v13_error_number_badge_toggle(self):
        """角标显示可在设置里开关；关掉只影响视觉，不影响按序号操作。"""
        self.assertIn('show_err_number', self.src)
        self.assertIn("host.classList.toggle('ptoe-err-num'", self.src)
        self.assertIn('.ptoe-err-num .ptoe-err[data-err-no]::before', self.src,
                      '角标必须由 .ptoe-err-num 类控制显隐（纯 CSS，不重建标注）')
        self.assertIn("id=\"errNumShow\"", self.cm_html)
        self.assertIn('"show_err_number": True', self.cm_html,
                      'GET 默认值与 configmanage 必须同名同默认')
        self.assertIn('"show_err_number": True', self.cfg)
        self.assertIn('if "show_err_number" in ui:', self.cfg)
        self.assertIn('if "show_err_number" in ui:', self.cm_html)
        # 勾选框接线：改完即时生效（切 CSS 类）+ 持久化到 ui_settings
        self.assertIn("const _errNumShowChk = document.getElementById('errNumShow');", self.src)
        self.assertIn('uiSettings.show_err_number = !!e.target.checked', self.src)

    def test_v14_error_nav_robust_and_ime_switch(self):
        """h/l 不会因标注缺失而卡死；Esc 后可自动切英文输入法（2026-10-30）。"""
        # 定位不到标注 → 先按状态补渲染一次（大模型校正/翻页挂载后 DOM 可能缺标注）
        self.assertIn('try { renderProofread(i); }', self.src)
        # 找不到可聚焦项时继续向后找，命中过至少一处即算移动成功
        self.assertIn('if (steps > 0) {', self.src)
        # NORMAL/VISUAL 下没有正文输入：IME 合成期间也要认 j/k/h/l，否则输入法把字母吞成拼音
        self.assertIn("if (VIM.mode !== 'NORMAL' && VIM.mode !== 'VISUAL') return;", self.mod)
        # Esc 后自动切英文输入法：设置项（前后端同默认，默认开）+ 触发点 + 服务端能力。
        # 2026-10-30 需求变更：只切「输入法内部中/英模式」，不切键盘布局（不能变成美式键盘）。
        self.assertIn('id="imeSwitchChk"', self.cm_html)
        self.assertIn('"ime_switch_on_esc": True', self.cm_html)
        self.assertIn('"ime_switch_on_esc": True', self.cfg)
        self.assertIn('if "ime_switch_on_esc" in ui:', self.cfg)
        self.assertIn('ime_switch_on_esc: true,', self.src)
        self.assertIn("uiBool('ime_switch_on_esc', true)", self.src)
        self.assertIn('function ptoeImeToEnglish()', self.src)
        self.assertIn('ptoeImeToEnglish();', self.mod, 'INSERT→NORMAL 时必须按设置尝试切英文')
        self.assertIn("'/api/ime_english'", self.src)
        self.assertIn('def ime_to_english(', self.cm_html)
        # 实现落在 imemanage：WM_IME_CONTROL 转换模式消息；绝不允许再切键盘布局
        self.ime_src = read(os.path.join(ROOT, 'imemanage.py'))
        self.assertIn('def ime_to_english(', self.ime_src)
        self.assertIn('_IMC_GETCONVERSIONMODE', self.ime_src)
        self.assertIn('_IMC_SETCONVERSIONMODE', self.ime_src)
        self.assertIn('IME_CMODE_ALPHANUMERIC', self.ime_src)
        self.assertIn('IME_CMODE_NATIVE', self.ime_src)
        self.assertNotIn('WM_INPUTLANGCHANGEREQUEST', self.ime_src)
        self.assertNotIn('LoadKeyboardLayout', self.ime_src)
        self.assertNotIn('WM_INPUTLANGCHANGEREQUEST', self.cm_html)
        self.assertNotIn('LoadKeyboardLayout', self.cm_html)

    def test_v15_page_jump_moves_focus(self):
        """命令/快捷键翻页后焦点必须搬到目标页：否则还得鼠标点一下，按页操作会落到旧页。"""
        self.assertIn('function focusPageIndex(', self.src)
        jump = self.src[self.src.index('function jumpToPageIndex('):]
        self.assertIn('focusPageIndex(idx);', jump[:2500],
                      'jumpToPageIndex 落点后必须聚焦目标页文字区（含光标复位）')
        self.assertIn('_lastFocusedEd = ed;', self.src)
        # 程序性 focus 不得触发 NORMAL→INSERT 自动切换（只有用户 mousedown 才切编辑态）
        self.assertIn("if (t.closest('.editable') && VIM.mode === 'NORMAL') setMode('INSERT');", self.mod)


class ModelMemoryStaticTest(unittest.TestCase):
    """模型记忆（2026-10-29）：多开矫正界面时模型不得停在过期值。"""

    @classmethod
    def setUpClass(cls):
        cls.js = read(APP_JS)
        cls.cm = read(os.path.join(ROOT, 'correctmanage.py'))

    def test_llm_status_reports_running_model(self):
        self.assertIn('"running_model": running_model or None', self.cm)
        self.assertIn('llamamanage._loaded_model_ids()', self.cm)

    def test_frontend_adopts_running_model(self):
        self.assertIn('async function ptoeAdoptRunningModel(res)', self.js)
        self.assertIn('async function ptoeSyncProofreadModel()', self.js)
        self.assertIn('await ptoeAdoptRunningModel(res)', self.js,
                      '状态轮询必须把本地模型收敛到服务端实际运行的模型')
        # 重识别前主动对齐一次，避免等到下一轮 5s 轮询
        reocr = self.js[self.js.index('async function _reocrIndices'):]
        self.assertIn('await ptoeSyncProofreadModel();', reocr[:4000])


class VimSyntaxTest(unittest.TestCase):

    def test_node_check_app_js(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('未安装 node，跳过语法检查')
        r = subprocess.run([node, '--check', APP_JS], capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, 'ui/app.js 语法错误：\n' + (r.stderr or ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
