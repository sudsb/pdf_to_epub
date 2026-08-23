# -*- coding: utf-8 -*-
"""configmanage / llamamanage 基础行为测试（unittest 版）。

原为 pytest 风格（模块级测试函数 + pytest.main），导致 `python -m unittest
discover` 加载器报错（环境未安装 pytest）。2026-08-23 审计整改：改为标准库
unittest，断言语义保持不变。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import configmanage as cfgm  # noqa: E402
import llamamanage as llm  # noqa: E402


class TestConfigLlama(unittest.TestCase):
    def test_get_config_defaults_and_update(self):
        cfg = cfgm.get_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("llama_server", cfg)
        self.assertIn("models_dir", cfg)

        orig = cfg.get("selected_model")
        try:
            cfgm.update_config("selected_model", "NON_EXISTENT_MODEL")
            cfg2 = cfgm.get_config()
            self.assertEqual(cfg2.get("selected_model"), "NON_EXISTENT_MODEL")
        finally:
            if orig is not None:
                cfgm.update_config("selected_model", orig)

    def test_llama_check_and_request_structure(self):
        sel = cfgm.get_config().get("selected_model")
        ok = llm.check(None, None, sel)
        self.assertIsInstance(ok, bool)

        res = llm.request("test", model_key=sel)
        self.assertIsInstance(res, dict)
        self.assertIn("result", res)
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
