# Phase 2 补跑成功判定修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Phase 2 补跑中缺少密码直接 KeyError、原始绑定邮箱不可用却错误换新邮箱、以及 sub2api/CPA 成功判定不一致的问题。

**Architecture:** 保持现有 `web_gui.py` 大文件结构，不引入新模块。新增小型校验函数集中判断补跑记录是否具备 Phase 2 必需字段，并修改补跑邮箱预留逻辑：已有原始绑定邮箱时只能使用原始邮箱凭据，不能回退到新邮箱；只有未绑定邮箱的记录才允许从邮箱池取新邮箱。成功状态继续由上传结果驱动，只有上传且验证成功才写入 `final_ok=True` 并计入补跑成功。

**Tech Stack:** Python 3, Flask app helper functions, `unittest`, `unittest.mock`, existing `test_web_gui_stats.py` test suite.

---

## File Structure

- Modify: `web_gui.py`
  - Add helper `_validate_phase2_result(result)` near `_phase2_for_result`.
  - Modify `_phase2_for_result` to fail with clear `missing_phase2_field:<field>` instead of raw `KeyError`.
  - Modify `_run_batch_phase2._reserve_email` so original Outlook email unavailable is a hard failure, not fallback.
  - Modify `_run_batch_phase2._batch_worker` to validate records before reserving email.
  - Preserve success count semantics: success only when upload is verified.
- Modify: `test_web_gui_stats.py`
  - Replace old fallback test expectation with “do not fallback when original email is unavailable”.
  - Add regression test for missing `password` records being skipped with a clear log/error and without calling `_phase2_for_result`.
  - Add regression test that records without `bind_email` may still reserve a new Outlook email.

---

### Task 1: Make missing Phase 2 fields explicit

**Files:**
- Modify: `test_web_gui_stats.py:250-324`
- Modify: `web_gui.py:1193-1308`

- [ ] **Step 1: Write the failing test**

Add this test to `test_web_gui_stats.py` near the existing Phase 2 tests:

```python
def test_phase2_for_result_reports_missing_password(self):
    config = {
        "upload_target": "cpa",
        "bind_email": "target@outlook.com",
        "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
        "outlook_pool": "",
    }
    account = {"phone": "+15551234567"}

    with self.assertRaisesRegex(RuntimeError, "missing_phase2_field:password"):
        web_gui._phase2_for_result(account, config)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_phase2_for_result_reports_missing_password -v
```

Expected: FAIL because `_phase2_for_result` currently raises raw `KeyError: 'password'`, not `RuntimeError: missing_phase2_field:password`.

- [ ] **Step 3: Write minimal implementation**

In `web_gui.py`, add this helper before `_phase2_for_result`:

```python
def _validate_phase2_result(result: dict) -> None:
    for field in ("phone", "password"):
        if not str(result.get(field) or "").strip():
            raise RuntimeError(f"missing_phase2_field:{field}")
```

Then call it at the start of `_phase2_for_result`:

```python
def _phase2_for_result(result: dict, config: dict, thread_tag: str = "", thread_id=None) -> dict:
    """Run Phase 2 for one registered account and upload to selected target."""
    _validate_phase2_result(result)
    upload_target = str(config.get("upload_target") or "sub2api").lower()
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_phase2_for_result_reports_missing_password -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Do not commit unless the user explicitly asks for a commit. If committing is requested later, stage only `web_gui.py` and `test_web_gui_stats.py`.

---

### Task 2: Skip invalid batch Phase 2 records before reserving email

**Files:**
- Modify: `test_web_gui_stats.py`
- Modify: `web_gui.py:1519-1544`

- [ ] **Step 1: Write the failing test**

Add this test to `test_web_gui_stats.py`:

```python
def test_batch_phase2_skips_record_missing_password_before_oauth(self):
    results_dir = Path(web_gui.__file__).with_name("results")
    result_path = results_dir / "test_phase2_missing_password.json"
    try:
        results_dir.mkdir(exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "phone_ok": True,
                    "phone": "+15551234567",
                    "bind_email": "old@outlook.com",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        config = {
            "email_provider": "outlook",
            "outlook_pool": "old@outlook.com----pw----client----refresh",
            "outlook_used": "outlook_used.txt",
            "upload_target": "cpa",
            "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
            "bind_email": "",
        }

        with mock.patch.object(web_gui, "_phase2_for_result") as phase2:
            with mock.patch.object(web_gui, "_log") as log:
                web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

        phase2.assert_not_called()
        messages = "\n".join(str(call.args[0]) for call in log.call_args_list if call.args)
        self.assertIn("missing_phase2_field:password", messages)
    finally:
        if result_path.exists():
            result_path.unlink()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_skips_record_missing_password_before_oauth -v
```

Expected: FAIL because current code reserves an email and calls `_phase2_for_result`, which raises later.

- [ ] **Step 3: Write minimal implementation**

In `_run_batch_phase2._batch_worker`, immediately after the `_result_upload_complete` skip block and before `phone = result.get("phone", "?")`, add:

```python
                try:
                    _validate_phase2_result(result)
                except RuntimeError as e:
                    phone = result.get("phone", "?")
                    tlog(f"[补跑] {tag} [{phone}] 跳过无效记录: {e}", "error")
                    with counter_lock:
                        counters["fail"] += 1
                    continue
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_skips_record_missing_password_before_oauth -v
```

Expected: PASS.

---

### Task 3: Stop falling back to a new Outlook email for already-bound accounts

**Files:**
- Modify: `test_web_gui_stats.py:273-324`
- Modify: `web_gui.py:1427-1446`

- [ ] **Step 1: Replace the old fallback test with the new expected behavior**

Rename and rewrite the existing test `test_batch_phase2_falls_back_to_new_outlook_when_original_email_missing_from_pool` as:

```python
def test_batch_phase2_does_not_fallback_when_original_outlook_email_missing_from_pool(self):
    results_dir = Path(web_gui.__file__).with_name("results")
    result_path = results_dir / "test_phase2_original_email_missing.json"
    try:
        results_dir.mkdir(exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "phone_ok": True,
                    "phone": "+15551234567",
                    "password": "pw",
                    "bind_email": "old@outlook.com",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"
            used_path.write_text(
                "2026-06-14 10:00:00\told@outlook.com\treserved\n",
                encoding="utf-8",
            )
            config = {
                "email_provider": "outlook",
                "outlook_pool": "new@outlook.com----pw----client----refresh",
                "outlook_used": str(used_path),
                "upload_target": "cpa",
                "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                "bind_email": "",
            }

            with mock.patch.object(web_gui, "_phase2_for_result") as phase2:
                with mock.patch.object(web_gui, "_log") as log:
                    web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

        phase2.assert_not_called()
        messages = "\n".join(str(call.args[0]) for call in log.call_args_list if call.args)
        self.assertIn("原始邮箱不可用", messages)
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["bind_email"], "old@outlook.com")
    finally:
        if result_path.exists():
            result_path.unlink()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_does_not_fallback_when_original_outlook_email_missing_from_pool -v
```

Expected: FAIL because current code falls back to `new@outlook.com` and calls `_phase2_for_result`.

- [ ] **Step 3: Write minimal implementation**

In `_reserve_email`, replace this block:

```python
                except Exception as e:
                    tlog(f"[补跑] {tag} [{phone}] 原始邮箱不可用，改用邮箱池新邮箱: {e}", "warn")
```

with:

```python
                except Exception as e:
                    tlog(f"[补跑] {tag} [{phone}] 原始邮箱不可用，无法补跑已绑定邮箱账号: {e}", "error")
                    return ""
```

Keep the existing non-Outlook branch unchanged:

```python
            else:
                tlog(f"[补跑] {tag} [{phone}] 使用原始邮箱: {original_email}", "info")
                return original_email
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_does_not_fallback_when_original_outlook_email_missing_from_pool -v
```

Expected: PASS.

---

### Task 4: Preserve new-email fallback only for records without original bind_email

**Files:**
- Modify: `test_web_gui_stats.py`
- Verify: `web_gui.py:1427-1509`

- [ ] **Step 1: Write the failing-or-regression test**

Add this test to `test_web_gui_stats.py`:

```python
def test_batch_phase2_uses_new_outlook_email_when_record_has_no_original_email(self):
    results_dir = Path(web_gui.__file__).with_name("results")
    result_path = results_dir / "test_phase2_no_original_email.json"
    captured = {}
    try:
        results_dir.mkdir(exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "phone_ok": True,
                    "phone": "+15551234567",
                    "password": "pw",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"
            config = {
                "email_provider": "outlook",
                "outlook_pool": "new@outlook.com----pw----client----refresh",
                "outlook_used": str(used_path),
                "upload_target": "cpa",
                "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                "bind_email": "",
            }

            def fake_phase2(result, config, thread_tag="", thread_id=None):
                captured["bind_email"] = config["bind_email"]
                return {"ok": True, "uploaded": True, "upload_verified": True, "upload_target": "cpa"}

            with mock.patch.object(web_gui, "_phase2_for_result", side_effect=fake_phase2):
                with mock.patch.object(web_gui, "_log"):
                    web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

        self.assertEqual(captured["bind_email"], "new@outlook.com")
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["bind_email"], "new@outlook.com")
        self.assertTrue(saved["final_ok"])
    finally:
        if result_path.exists():
            result_path.unlink()
```

- [ ] **Step 2: Run test**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_uses_new_outlook_email_when_record_has_no_original_email -v
```

Expected: PASS after Task 3. If it fails, adjust only the `_reserve_email` flow so `original_email == ""` still reaches the existing provider-specific reservation logic.

---

### Task 5: Verify success semantics for batch Phase 2

**Files:**
- Modify: `test_web_gui_stats.py`
- Verify: `web_gui.py:1546-1587`

- [ ] **Step 1: Write the regression test**

Add this test to `test_web_gui_stats.py`:

```python
def test_batch_phase2_upload_without_verification_is_not_final_ok(self):
    results_dir = Path(web_gui.__file__).with_name("results")
    result_path = results_dir / "test_phase2_upload_unverified.json"
    try:
        results_dir.mkdir(exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "phone_ok": True,
                    "phone": "+15551234567",
                    "password": "pw",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"
            config = {
                "email_provider": "outlook",
                "outlook_pool": "new@outlook.com----pw----client----refresh",
                "outlook_used": str(used_path),
                "upload_target": "sub2api",
                "sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw"},
                "bind_email": "",
            }

            def fake_phase2(result, config, thread_tag="", thread_id=None):
                return {"ok": True, "uploaded": True, "upload_verified": False, "upload_target": "sub2api"}

            with mock.patch.object(web_gui, "_phase2_for_result", side_effect=fake_phase2):
                with mock.patch.object(web_gui, "_log"):
                    web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

        saved = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["final_ok"])
        self.assertEqual(saved["status"], "upload_unverified")
    finally:
        if result_path.exists():
            result_path.unlink()
```

- [ ] **Step 2: Run test**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_upload_without_verification_is_not_final_ok -v
```

Expected: PASS with current code; keep it as a regression guard.

---

### Task 6: Run focused and related tests

**Files:**
- Test: `test_web_gui_stats.py`
- Test: `test_icloud_phase2.py`

- [ ] **Step 1: Run focused Phase 2 tests**

Run:

```bash
python -m unittest \
  test_web_gui_stats.WebGuiStatsTest.test_phase2_for_result_reports_missing_password \
  test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_skips_record_missing_password_before_oauth \
  test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_does_not_fallback_when_original_outlook_email_missing_from_pool \
  test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_uses_new_outlook_email_when_record_has_no_original_email \
  test_web_gui_stats.WebGuiStatsTest.test_batch_phase2_upload_without_verification_is_not_final_ok \
  -v
```

Expected: all PASS.

- [ ] **Step 2: Run existing web GUI stats tests**

Run:

```bash
python -m unittest test_web_gui_stats -v
```

Expected: all PASS.

- [ ] **Step 3: Run Phase 2 integration-adjacent tests**

Run:

```bash
python -m unittest test_icloud_phase2 -v
```

Expected: all PASS.

---

## Self-Review

- Spec coverage: covers missing `password`, original Outlook email unavailable, new email only when no original email, and final success only after upload verification.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: helper uses existing dict result shape and existing `RuntimeError`-string error flow.
