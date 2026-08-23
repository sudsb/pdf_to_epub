

## Verification checklist (copy into PR Verification section)

- Prerequisites (for full verification)
  - Node not required for these checks. Optional packages for full test-run: requests, zhconv, PyMuPDF, urllib3, pytest
  - Install with (optional):
    python -m pip install --upgrade requests zhconv PyMuPDF urllib3 pytest

- JS static check
  - Command:
    python check_js.py
  - Expectation:
    - Exits 0, prints no template/syntax errors.
  - Evidence to attach:
    - stdout of command (or excerpt showing "All template literals closed" / no error lines).

- Focused unit tests (fast)
  - Commands:
    python -m unittest test_stringmanage -v
    python -m unittest test_llamamanage -v
  - Expectation:
    - Both commands run and complete with OK (no ImportError/ModuleNotFoundError for zhconv or requests).
  - Evidence to attach:
    - stdout of both runs (showing OK).

- GUI smoke (server + config endpoint)
  1. Start GUI (no browser):
     python mian.py gui --no-browser --host 127.0.0.1 --port 0
     - Watch stdout for the ready banner: "配置界面已启动: http://127.0.0.1:<port>/"
  2. Test API:
     curl -sS http://127.0.0.1:<port>/api/config | python -m json.tool
  - Expectation:
    - GET /api/config returns HTTP 200 JSON.
    - JSON contains top-level "config" whose keys include at least:
      - fonts
      - image_preprocess
      - llama_server, models_dir, engine (sanity check)
  - Evidence to attach:
    - GUI startup banner (copy the printed URL).
    - `curl` response (first 200–600 chars or full JSON if small).

- Full test suite (optional, slower; run only if CI has optional deps)
  - Commands:
    python -m pip install --upgrade requests zhconv PyMuPDF urllib3 pytest
    python -m unittest discover -v
  - Expectation:
    - No errors/failures (or only documented/skipped tests). Example earlier run: "Ran 552 tests — OK (skipped=1)".
  - Evidence to attach:
    - Full unittest discover stdout (or top/bottom excerpts showing summary).

- Quick triage guidance (if a check fails)
  - JS check fails:
    - Inspect guimanage._UI_HTML region you modified; run check_js.py and fix syntax/template errors; re-run.
  - test_stringmanage ImportError for zhconv:
    - Either install zhconv in the test env, or confirm guarded import present (stringmanage.py top) and rerun focused test.
  - test_llamamanage adapter assertions failing:
    - Confirm llamamanage.py adapter creation; tests expect adapter.max_retries to be present, and DummySession._DummyAdapter.max_retries present when requests absent.
  - GUI /api/config missing keys:
    - Confirm guimanage.js `renderBasic()` populates cfg.fonts and cfg.image_preprocess and `saveConfig()` calls `collectExtraConfig()` before POSTing.
  - Full-suite failures referencing other native libs (opencv, external tools):
    - These are out-of-scope for this PR; either install the missing system libs or run the full suite in CI that provides them.

- What to attach to PR for easy review
  - check_js.py stdout (paste stdout).
  - Focused test outputs (test_stringmanage/test_llamamanage).
  - If running full suite: tail of unittest discover output and any failing tracebacks.
  - GUI smoke: printed GUI URL and curl /api/config output (JSON).

Copy this checklist into the PR Verification section and tick items as you run them.
