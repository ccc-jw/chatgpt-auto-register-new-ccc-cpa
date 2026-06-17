# Register Diagnostics and Outlook Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make phone registration `status=400` failures diagnosable while proving that phone-stage failures do not reserve or mark Outlook email accounts.

**Architecture:** Keep the change isolated to `auto_register.py` by adding a small sanitizer/formatter for registration failure summaries and using it only at the `register_rejected` return point. Add tests in `test_auto_register_retry.py` that exercise the new formatter through `register_one()` and guard the current `web_gui.py` behavior where Outlook reservation only happens after `phone_ok` and Phase2 starts.

**Tech Stack:** Python 3.12, unittest, pytest, existing `auto_register.py` registration flow, existing `web_gui.py` worker flow.

---

## File Structure

- Modify: `auto_register.py`
  - Add `_safe_register_error_summary(result: dict) -> str` near the registration helpers.
  - Use it in the `register_rejected` branch inside `register_one()`.
- Modify: `test_auto_register_retry.py`
  - Add a fake register class that returns a 400-style response with safe and sensitive fields.
  - Add a unit test proving `register_one()` includes safe diagnostic fields and redacts sensitive fields.
- Modify: `test_web_gui_stats.py`
  - Add a small source-level regression test that verifies the ordinary registration worker records/registers failure before Phase2 email reservation and that `reserve_phase2_email()` is only called inside the Phase2 block.

## Task 1: Add diagnostic summary for registration 400 failures

**Files:**
- Modify: `test_auto_register_retry.py`
- Modify: `auto_register.py`

- [ ] **Step 1: Write the failing test**

Append this fake class after `FakeRegister` in `test_auto_register_retry.py`:

```python
class RejectingRegister(FakeRegister):
    def register_user(self, phone, password):
        return {
            "_status": 400,
            "error": "invalid_request",
            "code": "phone_rejected",
            "message": "Phone number rejected",
            "csrf": "secret-csrf",
            "access_token": "secret-token",
            "password": "secret-password",
            "_body": '{"error":"invalid_request","message":"Phone number rejected","access_token":"secret-token"}',
        }
```

Add this test method inside `AutoRegisterRetryTests`:

```python
    def test_register_rejected_includes_safe_diagnostic_summary(self):
        sms = FakeSms()

        with patch.object(ar, "ChatGPTRegister", RejectingRegister), patch.object(ar._time, "sleep", return_value=None):
            result = ar.register_one(sms, self.config, verbose=False, no_phase2=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_stage"], "register_rejected")
        self.assertIn("status=400", result["error"])
        self.assertIn("error=invalid_request", result["error"])
        self.assertIn("code=phone_rejected", result["error"])
        self.assertIn("message=Phone number rejected", result["error"])
        self.assertNotIn("secret-csrf", result["error"])
        self.assertNotIn("secret-token", result["error"])
        self.assertNotIn("secret-password", result["error"])
        self.assertTrue(sms.cancelled)
        self.assertFalse(sms.completed)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest test_auto_register_retry.py::AutoRegisterRetryTests::test_register_rejected_includes_safe_diagnostic_summary -v
```

Expected: FAIL because the error currently only contains `注册被拒(status=400)` and does not include `error=invalid_request`, `code=phone_rejected`, or `message=Phone number rejected`.

- [ ] **Step 3: Add the minimal formatter implementation**

In `auto_register.py`, add this helper before `register_one()`:

```python
_SENSITIVE_REGISTER_ERROR_KEYS = (
    "authorization",
    "cookie",
    "csrf",
    "password",
    "session",
    "token",
)


def _is_sensitive_register_error_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SENSITIVE_REGISTER_ERROR_KEYS)


def _clean_register_error_value(value) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text[:160]


def _safe_register_error_summary(result: dict) -> str:
    parts = []
    status = result.get("_status")
    if status:
        parts.append(f"status={status}")

    for key in ("error", "code", "message", "detail"):
        value = result.get(key)
        if value and not _is_sensitive_register_error_key(key):
            parts.append(f"{key}={_clean_register_error_value(value)}")

    if len(parts) == (1 if status else 0):
        body = result.get("_body")
        if body:
            body_text = _clean_register_error_value(body)
            lowered = body_text.lower()
            if not any(part in lowered for part in _SENSITIVE_REGISTER_ERROR_KEYS):
                parts.append(f"body={body_text}")

    return ", ".join(parts) if parts else "status=unknown"
```

- [ ] **Step 4: Use the formatter in the rejected branch**

Replace this block in `auto_register.py`:

```python
            if verbose:
                print(f"  [5/9] 注册被拒 status={result.get('_status')}")
            return _fail_result(phone, "register_rejected", f"注册被拒(status={result.get('_status')})", sms_provider, used_country, aid)
```

with:

```python
            error_summary = _safe_register_error_summary(result)
            if verbose:
                print(f"  [5/9] 注册被拒 {error_summary}")
            return _fail_result(phone, "register_rejected", f"注册被拒({error_summary})", sms_provider, used_country, aid)
```

- [ ] **Step 5: Run the focused test to verify it passes**

Run:

```bash
.venv/bin/python -m pytest test_auto_register_retry.py::AutoRegisterRetryTests::test_register_rejected_includes_safe_diagnostic_summary -v
```

Expected: PASS.

- [ ] **Step 6: Commit the diagnostic change if the user has requested commits for this implementation**

Only commit if explicitly authorized in the current conversation. If authorized, run:

```bash
git add auto_register.py test_auto_register_retry.py
git commit -m "fix: add safe register rejection diagnostics"
```

## Task 2: Guard Outlook reservation against phone-stage failures

**Files:**
- Modify: `test_web_gui_stats.py`
- Read-only reference: `web_gui.py:2200-2361`

- [ ] **Step 1: Write the regression test**

Append this test to `test_web_gui_stats.py`:

```python
def test_outlook_reservation_only_happens_after_phone_stage_success():
    source = Path("web_gui.py").read_text(encoding="utf-8")

    failure_continue = """elif not phone_ok:
                    finish_registration(False)
                    _record_stat(False, result)
                    continue"""
    phase2_gate = """if (
                    phase2_enabled
                    and phase2_configured
                    and result.get("session_token")
                ):"""
    reserve_call = "new_email = reserve_phase2_email(thread_id)"

    failure_index = source.index(failure_continue)
    phase2_index = source.index(phase2_gate)
    reserve_index = source.index(reserve_call)

    assert failure_index < phase2_index < reserve_index
```

If `Path` is not already imported in `test_web_gui_stats.py`, add this import near the top:

```python
from pathlib import Path
```

- [ ] **Step 2: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest test_web_gui_stats.py::test_outlook_reservation_only_happens_after_phone_stage_success -v
```

Expected: PASS if the current `web_gui.py` already has the safe ordering. If it fails, inspect the ordering in `web_gui.py` and move Outlook reservation so that `elif not phone_ok: ... continue` occurs before any call to `reserve_phase2_email(thread_id)`.

- [ ] **Step 3: If the test fails, apply the minimal web flow fix**

Only do this if Step 2 fails. In `web_gui.py`, ensure this failure branch remains before the Phase2 block and before any email reservation:

```python
                elif not phone_ok:
                    finish_registration(False)
                    _record_stat(False, result)
                    continue
```

The Phase2 email reservation must remain inside this existing block:

```python
                if (
                    phase2_enabled
                    and phase2_configured
                    and result.get("session_token")
                ):
```

- [ ] **Step 4: Run the focused test again**

Run:

```bash
.venv/bin/python -m pytest test_web_gui_stats.py::test_outlook_reservation_only_happens_after_phone_stage_success -v
```

Expected: PASS.

- [ ] **Step 5: Commit the Outlook protection regression test if the user has requested commits for this implementation**

Only commit if explicitly authorized in the current conversation. If authorized, run:

```bash
git add test_web_gui_stats.py web_gui.py
git commit -m "test: guard outlook reservation after phone success"
```

## Task 3: Run full verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run the full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: all tests pass, including the new register diagnostics test and the Outlook reservation ordering regression test.

- [ ] **Step 2: Review the final diff**

Run:

```bash
git diff -- auto_register.py test_auto_register_retry.py test_web_gui_stats.py docs/superpowers/specs/2026-06-17-register-diagnostics-outlook-protection-design.md docs/superpowers/plans/2026-06-17-register-diagnostics-outlook-protection.md
```

Expected: only the safe register rejection summary helper/use, focused tests, and planning/spec documents are changed.

- [ ] **Step 3: Report verification evidence**

Report these facts to the user:

```text
Implemented register rejection diagnostics and Outlook reservation protection tests.
Verification: .venv/bin/python -m pytest -> <exact pass count and runtime>
Notes: <whether web_gui.py needed behavior changes or only a regression test>
```

## Self-Review

- Spec coverage: Task 1 covers diagnostic 400 summaries; Task 2 covers Outlook protection; Task 3 covers full verification.
- Placeholder scan: No implementation placeholders remain; conditional web flow fix is explicitly bounded to the case where the regression test fails.
- Type consistency: New helper accepts the same `dict` returned by `ChatGPTRegister.register_user()` and returns a string used in the existing `_fail_result()` error field.
