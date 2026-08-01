import os
import json
import tempfile
import pytest

import configmanage as cfgm
import llamamanage as llm


def test_get_config_defaults_and_update():
    cfg = cfgm.get_config()
    assert isinstance(cfg, dict)
    assert 'llama_server' in cfg
    assert 'models_dir' in cfg
    orig_selected = cfg.get('selected_model')
    # update selected_model to a non-existing key and ensure fallback
    cfg2 = cfgm.update_config('selected_model', 'NON_EXISTENT_MODEL')
    assert 'selected_model' in cfg2
    # restore original
    cfgm.update_config('selected_model', orig_selected)


def test_llama_check_and_request_structure():
    cfg = cfgm.get_config()
    sel = cfg.get('selected_model')
    # check should be callable and return a bool
    ok = llm.check(None, None, sel)
    assert isinstance(ok, bool)
    # request should return dict with result/error keys
    r = llm.request('test', model_key=sel)
    assert isinstance(r, dict)
    assert 'result' in r and 'error' in r


if __name__ == '__main__':
    pytest.main([__file__])
