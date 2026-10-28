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
