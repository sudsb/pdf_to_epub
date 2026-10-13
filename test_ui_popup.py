"""选中文字快捷菜单（弹出菜单）按钮字形 / 候选池一致性测试。

回归动机（2026-10）：
- 「矫正界面 → 选中文字」弹出的快捷菜单里，操作按钮曾直接显示英文 op 名
  （如 underline / indent，见 _makePopBtn 的 `OP_ICON[op] || op`），
  bold / italic 也显示英文字母 B / I；
- 候选池（设置-弹出菜单下方下拉框）新增操作后，若没有对应字形就会露出英文。

本测试锁定以下不变量：
1. 前端 POPUP_ELIGIBLE_OPS 与后端 configmanage.POPUP_ELIGIBLE_OPS 完全一致；
2. 候选池每个操作（paint 特例除外）都有字形，且字形全为简体中文、不含 ASCII；
3. 弹出菜单所有固定按钮字形两两不重复；
4. 候选池操作都有 OPS 中文标签（保证设置面板/提示显示中文）；
5. 默认两行按钮的操作都有字形。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _js_string_array(js, var):
    """提取 `const var = [ 'a', 'b' ];` 中的字符串列表。"""
    m = re.search(r"const %s\s*=\s*\[(.*?)\];" % re.escape(var), js, re.S)
    if not m:
        raise AssertionError("未找到 JS 数组 " + var)
    return re.findall(r"'([^']*)'", m.group(1))


def _js_string_object(js, var):
    """提取 `const var = { key: 'value' };` 中的字符串键值对（忽略行注释）。"""
    m = re.search(r"const %s\s*=\s*\{(.*?)\n\};" % re.escape(var), js, re.S)
    if not m:
        raise AssertionError("未找到 JS 对象 " + var)
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return dict(re.findall(r"(\w+)\s*:\s*'([^']*)'", body))


def _js_ops_pairs(js):
    """提取 OPS 注册表 [[op, label], ...]。"""
    m = re.search(r"const OPS\s*=\s*\[(.*?)\];", js, re.S)
    if not m:
        raise AssertionError("未找到 JS 数组 OPS")
    return re.findall(r"\['([^']+)'\s*,\s*'([^']*)'\]", m.group(1))


def _js_assigned_literal(js, lhs):
    """提取 `lhs = '字面量';` 中的字面量（找不到返回 None）。"""
    m = re.search(re.escape(lhs) + r"\s*=\s*'([^']*)'", js)
    return m.group(1) if m else None


def _strip_tags(s):
    return re.sub(r"<[^>]*>", "", s).strip()


class TestPopupMenuButtons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("ui", "app.js"))
        import configmanage as cm
        cls.cm = cm

    def test_js_and_backend_pool_in_sync(self):
        """前端候选池必须与后端逐项一致（顺序也一致，便于人工比对）。"""
        js_pool = _js_string_array(self.js, "POPUP_ELIGIBLE_OPS")
        self.assertEqual(js_pool, list(self.cm.POPUP_ELIGIBLE_OPS))

    def test_new_popup_features_present(self):
        """新增到下拉列表的功能：下加点、去空、字符格式。"""
        js_pool = _js_string_array(self.js, "POPUP_ELIGIBLE_OPS")
        for op in ("underdot", "strip_ws", "charformat"):
            self.assertIn(op, list(self.cm.POPUP_ELIGIBLE_OPS))
            self.assertIn(op, js_pool)

    def test_charformat_op_opens_dialog(self):
        """「字符格式」候选 op 必须经 applyOp 打开字号/颜色弹窗（不能是空操作）。"""
        self.assertIn(
            "if (op === 'charformat') { openCharFormatDialog(); return; }",
            self.js,
            "applyOp 未把 charformat 路由到 openCharFormatDialog",
        )
        self.assertIn("['charformat','字符格式']", self.js, "OPS 缺 charformat 中文标签")

    def test_every_pool_op_has_unique_chinese_icon(self):
        icons = _js_string_object(self.js, "OP_ICON")
        display = {k: _strip_tags(v) for k, v in icons.items()}
        pool = list(self.cm.POPUP_ELIGIBLE_OPS)

        # 候选池每个操作（paint 用独立按钮）都必须有字形，杜绝英文回退
        for op in pool:
            if op == "paint":
                continue
            self.assertIn(op, display, "候选池操作缺字形（会显示英文 op 名）: " + op)
            self.assertTrue(display[op], "字形为空: " + op)
            self.assertIsNone(
                re.search(r"[A-Za-z]", display[op]),
                "字形含英文字母: %s=%s" % (op, display[op]),
            )
            self.assertLessEqual(len(display[op]), 2, "字形过长: %s=%s" % (op, display[op]))

        # 固定按钮字形（含 paint 与第三行「规」）两两不重复
        paint = _js_assigned_literal(self.js, "paintBtn.textContent")
        rule = _js_assigned_literal(self.js, "ruleBtn.textContent")
        self.assertIsNotNone(paint, "未找到格式刷按钮字形")
        self.assertIsNotNone(rule, "未找到规则按钮字形")
        texts = list(display.values()) + [paint or "", rule or ""]
        dup = sorted({t for t in texts if texts.count(t) > 1})
        self.assertEqual(dup, [], "弹出菜单按钮字形重复: " + repr(dup))

    def test_pool_ops_have_chinese_labels(self):
        """每个候选池操作都要在 OPS 注册表里有中文标签（供设置面板/提示）。"""
        ops_keys = {k for k, _ in _js_ops_pairs(self.js)}
        for op in self.cm.POPUP_ELIGIBLE_OPS:
            if op == "paint":
                continue
            self.assertIn(op, ops_keys, "候选池操作无中文标签(OPS): " + op)

    def test_default_rows_ops_have_icons(self):
        icons = _js_string_object(self.js, "OP_ICON")
        for op in list(self.cm.POPUP_ROW1_DEFAULT) + list(self.cm.POPUP_ROW2_DEFAULT):
            if op == "paint":
                continue  # 格式刷用独立按钮（_makePaintBtn）
            self.assertIn(op, icons, "默认行操作缺字形: " + op)

    def test_no_english_fallback_in_make_pop_btn(self):
        """_makePopBtn 不得再回退成英文 op 名。"""
        self.assertNotIn("b.innerHTML = OP_ICON[op] || op", self.js)
        self.assertIn("b.innerHTML = OP_ICON[op] || esc(popupOpIcon(op))", self.js)


if __name__ == "__main__":
    unittest.main()
