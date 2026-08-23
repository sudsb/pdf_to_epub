Make optional runtime deps lazy (zhconv, requests) and add config UI fields

- Guard zhconv import in stringmanage.py and fallback to identity conversion:
  try:
      from zhconv import zhconv
  except Exception:
      zhconv = None
  ttos/stot return input when zhconv is None.

- Guard requests import in llamamanage.py; create _REQUESTS_AVAILABLE flag.
  - When available: create requests.Session() and mount an HTTPAdapter with Retry (POST allowed).
  - When absent: provide DummySession and _DummyAdapter with a minimal max_retries shim so tests that inspect adapter.max_retries and code that monkeypatches _SESSION continue to work.

- Expose DEFAULT_CONFIG keys in the GUI (guimanage.py _UI_HTML):
  - Add "字体设置" inputs: cfgFontBody, cfgFontHeading, cfgFontNote, cfgFontCitation.
  - Add "图片预处理" controls: cfgImgPreEnabled, cfgImgGray, cfgImgDenoise, cfgImgSharpen, cfgImgBinarize, cfgImgWorkers.
  - JS: update renderBasic() to populate the new controls; add collectExtraConfig(); call collectExtraConfig() from saveConfig() so the fields are posted to /api/config.

Verification notes:
- JS static check: python check_js.py → OK
- Focused tests: python -m unittest test_stringmanage -v, python -m unittest test_llamamanage -v → OK
- Full suite after installing optional deps (requests, zhconv, PyMuPDF, urllib3, pytest): python -m unittest discover -v → Ran 552 tests — OK (skipped=1)
- GUI smoke: started gui and verified GET /api/config includes fonts and image_preprocess.

Rationale: avoid import-time failures in minimal test/CI environments while preserving runtime behavior when optional deps are present.