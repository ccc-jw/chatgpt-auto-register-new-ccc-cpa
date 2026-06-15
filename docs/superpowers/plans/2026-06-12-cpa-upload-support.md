# CPA Upload Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add WebUI-selectable CPA upload support while keeping SUB2API behavior compatible, with CPA OAuth-first import, auth-files fallback, and local failed-upload persistence.

**Architecture:** Introduce a small upload target layer around the existing Phase2 flow. `phase2_codex.py` owns CPA/SUB2API HTTP adapters, `failed_uploads.py` owns durable failed-upload records, `openai_bind_email.py` branches the final `[11]` upload step by target, and `web_gui.py` only saves config, prepares per-account OAuth metadata, schedules work, and displays upload status.

**Tech Stack:** Python 3, Flask, `requests`, `curl_cffi`, `unittest`, existing HTML/vanilla JS WebUI.

---

## File Structure

- Create: `failed_uploads.py`
  - Save/load/retry failed upload JSON files.
  - Enforce “no management secrets in failed files”.
  - Atomic `.tmp` then `.json` writes.
- Modify: `phase2_codex.py`
  - Keep existing `get_oauth_url(...)` and `upload_session(...)` compatible.
  - Add CPA management helpers.
  - Add small upload-target helper functions so callers do not duplicate OAuth URL generation.
- Modify: `openai_bind_email.py`
  - Extend `run_second_half(...)` with `upload_target`, CPA settings, and original account token fields.
  - Refactor existing SUB2API `[11]` logic into helper functions.
  - Add CPA native callback then auth-files fallback.
  - Save failed upload records only after OpenAI OAuth/bind succeeded but final upload failed.
- Modify: `web_gui.py`
  - Save/load `upload_target` and `cpa` config through `/api/config`.
  - Add upload-platform fields to HTML and JS.
  - Use target-aware Phase2 preparation in single-run and batch paths.
  - Preserve per-thread independent OAuth state.
  - Display `uploaded`, `upload_error`, and `failed_upload_file` in stored results/logs.
- Modify: `config.example.json`
  - Add default `upload_target: "sub2api"` and `cpa` object.
- Modify: `test_icloud_phase2.py`
  - Add unit tests for CPA helpers and `run_second_half(...)` upload branching.
- Modify: `test_web_gui_stats.py`
  - Add config round-trip and target-aware Phase2 tests.
- Create: `test_failed_uploads.py`
  - Unit tests for failed-upload save/load/retry boundaries.

---

## Task 1: Add CPA defaults to config loading and example config

**Files:**
- Modify: `auto_register.py:226-263`
- Modify: `config.example.json:27-33`
- Test: `test_auto_register_retry.py`

- [ ] **Step 1: Write failing config default test**

Add this test method to `AutoRegisterRetryTests` in `test_auto_register_retry.py`:

```python
    def test_load_config_defaults_upload_target_to_sub2api_and_cpa_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "sms": {"provider": "smsbower", "api_key": "test", "countries": ["33"], "service": "dr"},
                        "register": {"password": "pw", "name": "A", "birthdate": "2000-01-01"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            cfg = ar.load_config(str(path))

        self.assertEqual(cfg["upload_target"], "sub2api")
        self.assertEqual(
            cfg["cpa"],
            {
                "management_url": "",
                "api_url": "",
                "management_key": "",
                "upload_mode": "auto",
            },
        )
```

Also add these imports at the top of `test_auto_register_retry.py`:

```python
import json
import tempfile
from pathlib import Path
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
python -m unittest test_auto_register_retry.AutoRegisterRetryTests.test_load_config_defaults_upload_target_to_sub2api_and_cpa_auto -v
```

Expected: FAIL with `KeyError: 'upload_target'` or a mismatch for `cpa`.

- [ ] **Step 3: Implement config defaults in `auto_register.load_config`**

In `auto_register.py`, inside the `config = { ... }` literal at `load_config`, add:

```python
        "upload_target": "sub2api",
        "cpa": {
            "management_url": "",
            "api_url": "",
            "management_key": "",
            "upload_mode": "auto",
        },
```

Then, after the existing passthrough loop that copies `found.items()` into `config`, add normalization before `break`:

```python
            upload_target = str(config.get("upload_target") or "sub2api").strip().lower()
            if upload_target not in {"sub2api", "cpa"}:
                upload_target = "sub2api"
            config["upload_target"] = upload_target

            cpa_cfg = dict(config.get("cpa") or {})
            config["cpa"] = {
                "management_url": str(cpa_cfg.get("management_url") or ""),
                "api_url": str(cpa_cfg.get("api_url") or ""),
                "management_key": str(cpa_cfg.get("management_key") or ""),
                "upload_mode": str(cpa_cfg.get("upload_mode") or "auto") or "auto",
            }
```

- [ ] **Step 4: Update `config.example.json`**

Add top-level fields after `sub2api`:

```json
  "upload_target": "sub2api",
  "cpa": {
    "management_url": "http://47.89.129.103:18317",
    "api_url": "http://47.89.129.103:8317",
    "management_key": "",
    "upload_mode": "auto"
  },
```

Keep `management_key` empty in the example file.

- [ ] **Step 5: Run tests and verify they pass**

Run:

```bash
python -m unittest test_auto_register_retry.AutoRegisterRetryTests.test_load_config_defaults_upload_target_to_sub2api_and_cpa_auto -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add auto_register.py config.example.json test_auto_register_retry.py
git commit -m "feat: add upload target config defaults"
```

---

## Task 2: Add failed-upload persistence module

**Files:**
- Create: `failed_uploads.py`
- Create: `test_failed_uploads.py`

- [ ] **Step 1: Write failing failed-upload tests**

Create `test_failed_uploads.py` with:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import failed_uploads


class FailedUploadsTests(unittest.TestCase):
    def test_save_failed_upload_writes_json_without_management_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = failed_uploads.save_failed_upload(
                {
                    "upload_target": "cpa",
                    "upload_mode": "auto",
                    "phone": "+56123456789",
                    "email": "user@example.com",
                    "session_token": "session-token",
                    "access_token": "access-token",
                    "management_key": "must-not-save",
                    "sub2api_pwd": "must-not-save",
                    "last_error": "CPA upload failed",
                },
                base_dir=Path(tmp),
            )

            saved_path = Path(path)
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.suffix, ".json")
            self.assertFalse(saved_path.with_suffix(saved_path.suffix + ".tmp").exists())
            data = json.loads(saved_path.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["upload_target"], "cpa")
        self.assertEqual(data["attempts"], 1)
        self.assertEqual(data["phone"], "+56123456789")
        self.assertNotIn("management_key", data)
        self.assertNotIn("sub2api_pwd", data)

    def test_save_failed_upload_generates_unique_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = failed_uploads.save_failed_upload({"phone": "+1", "email": "a@example.com"}, base_dir=Path(tmp))
            second = failed_uploads.save_failed_upload({"phone": "+1", "email": "a@example.com"}, base_dir=Path(tmp))

        self.assertNotEqual(first, second)

    def test_load_failed_upload_reads_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = failed_uploads.save_failed_upload(
                {"upload_target": "sub2api", "phone": "+1", "access_token": "at"},
                base_dir=Path(tmp),
            )
            record = failed_uploads.load_failed_upload(path)

        self.assertEqual(record["upload_target"], "sub2api")
        self.assertEqual(record["access_token"], "at")

    def test_retry_failed_upload_moves_successful_file_to_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "cpa",
                        "upload_mode": "auto",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"cpa": {"api_url": "https://cpa.example.com", "management_key": "key"}}

            with mock.patch("phase2_codex.upload_cpa_auth_file", return_value={"ok": True, "filename": "codex-user.json"}):
                result = failed_uploads.retry_failed_upload(path, config)

            self.assertTrue(result["ok"])
            self.assertFalse(path.exists())
            self.assertTrue((Path(tmp) / "done" / path.name).exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test and verify it fails because module is missing**

Run:

```bash
python -m unittest test_failed_uploads -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'failed_uploads'`.

- [ ] **Step 3: Create `failed_uploads.py`**

Create `failed_uploads.py` with:

```python
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).parent / "failed_uploads"
_SECRET_KEYS = {
    "management_key",
    "cpa_management_key",
    "sub2api_pwd",
    "sub2api_password",
    "password",
    "pwd",
    "admin_token",
    "authorization",
}
_ALLOWED_KEYS = {
    "schema_version",
    "created_at",
    "upload_target",
    "upload_mode",
    "phone",
    "email",
    "session_token",
    "access_token",
    "refresh_token",
    "id_token",
    "account_id",
    "expires_at",
    "expired",
    "oauth_state",
    "last_error",
    "attempts",
}


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.@-]+", "-", str(value or "unknown")).strip(".-_")
    return (label or "unknown")[:80]


def _clean_record(record: Dict[str, Any]) -> Dict[str, Any]:
    clean = {k: v for k, v in dict(record or {}).items() if k in _ALLOWED_KEYS and k not in _SECRET_KEYS}
    clean.setdefault("schema_version", 1)
    clean.setdefault("created_at", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    clean.setdefault("upload_target", "sub2api")
    clean.setdefault("upload_mode", "auto")
    clean.setdefault("attempts", 1)
    for secret in _SECRET_KEYS:
        clean.pop(secret, None)
    return clean


def save_failed_upload(record: Dict[str, Any], base_dir: Path = None) -> str:
    directory = Path(base_dir) if base_dir is not None else BASE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    clean = _clean_record(record)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    label = _safe_label(clean.get("email") or clean.get("phone") or "account")
    short_id = secrets.token_hex(4)
    final_path = directory / f"{stamp}_codex_{label}_{short_id}.json"
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, final_path)
    return str(final_path)


def load_failed_upload(path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _auth_payload_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"type": "codex"}
    for key in ("email", "access_token", "refresh_token", "id_token", "account_id"):
        if record.get(key):
            payload[key] = record[key]
    if record.get("expired"):
        payload["expired"] = record["expired"]
    elif record.get("expires_at"):
        payload["expired"] = str(record["expires_at"])
    if record.get("last_refresh"):
        payload["last_refresh"] = record["last_refresh"]
    return payload


def retry_failed_upload(path, config: Dict[str, Any]) -> Dict[str, Any]:
    from phase2_codex import upload_cpa_auth_file, upload_session

    failed_path = Path(path)
    record = load_failed_upload(failed_path)
    target = str(record.get("upload_target") or config.get("upload_target") or "sub2api").lower()

    if target == "cpa":
        cpa = config.get("cpa", {})
        filename = f"codex-{_safe_label(record.get('email') or record.get('phone') or 'account')}.json"
        result = upload_cpa_auth_file(
            cpa.get("api_url", ""),
            cpa.get("management_key", ""),
            _auth_payload_from_record(record),
            filename,
        )
    else:
        sub = config.get("sub2api", {})
        result = upload_session(
            record.get("session_token", ""),
            record.get("email", ""),
            sub.get("url", ""),
            sub.get("email", ""),
            sub.get("pwd", ""),
            sub2api_proxy_id=int(sub.get("proxy_id", 0) or 0),
            group_ids=[int(sub.get("group_id", 1) or 1)],
            access_token=record.get("access_token", ""),
        )

    if result.get("ok"):
        done_dir = failed_path.parent / "done"
        done_dir.mkdir(exist_ok=True)
        shutil.move(str(failed_path), str(done_dir / failed_path.name))
    return result
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
python -m unittest test_failed_uploads -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add failed_uploads.py test_failed_uploads.py
git commit -m "feat: persist failed uploads"
```

---

## Task 3: Add CPA HTTP helper functions in `phase2_codex.py`

**Files:**
- Modify: `phase2_codex.py:1-168`
- Test: `test_icloud_phase2.py`

- [ ] **Step 1: Write failing CPA helper tests**

Add these methods to the existing Phase2 test class in `test_icloud_phase2.py` near `test_phase2_codex_get_oauth_url_returns_auth_url_session_and_state`:

```python
    def test_get_cpa_oauth_url_returns_url_and_state(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {
            "status": "ok",
            "state": "cpa-state-123",
            "url": "https://auth.openai.com/oauth/authorize?state=cpa-state-123",
        }

        with mock.patch("requests.get", return_value=resp) as get_mock:
            info = phase2_codex.get_cpa_oauth_url("https://cpa.example.com/", "mgmt-key")

        get_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/codex-auth-url",
            headers={"X-Management-Key": "mgmt-key"},
            timeout=30,
        )
        self.assertEqual(info["auth_url"], "https://auth.openai.com/oauth/authorize?state=cpa-state-123")
        self.assertEqual(info["state"], "cpa-state-123")

    def test_complete_cpa_oauth_callback_posts_provider_code_and_state(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {"status": "ok", "message": "imported"}

        with mock.patch("requests.post", return_value=resp) as post_mock:
            result = phase2_codex.complete_cpa_oauth_callback(
                "https://cpa.example.com",
                "mgmt-key",
                "code-123",
                "state-123",
            )

        post_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/oauth-callback",
            json={"provider": "codex", "code": "code-123", "state": "state-123"},
            headers={"X-Management-Key": "mgmt-key"},
            timeout=120,
        )
        self.assertTrue(result["ok"])

    def test_upload_cpa_auth_file_posts_codex_payload(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {"status": "ok", "filename": "codex-user.json"}

        with mock.patch("requests.post", return_value=resp) as post_mock:
            result = phase2_codex.upload_cpa_auth_file(
                "https://cpa.example.com",
                "mgmt-key",
                {"type": "codex", "email": "user@example.com", "access_token": "at"},
                "codex-user.json",
            )

        post_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/auth-files",
            json={"filename": "codex-user.json", "content": {"type": "codex", "email": "user@example.com", "access_token": "at"}},
            headers={"X-Management-Key": "mgmt-key"},
            timeout=60,
        )
        self.assertTrue(result["ok"])
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_get_cpa_oauth_url_returns_url_and_state test_icloud_phase2.Phase2CodexTests.test_complete_cpa_oauth_callback_posts_provider_code_and_state test_icloud_phase2.Phase2CodexTests.test_upload_cpa_auth_file_posts_codex_payload -v
```

Expected: FAIL with `AttributeError` for missing CPA functions.

- [ ] **Step 3: Add CPA helpers to `phase2_codex.py`**

Append these functions after `get_oauth_url(...)` and before `upload_session(...)`:

```python

def _join_cpa_management_url(cpa_api_url: str, path: str) -> str:
    return f"{str(cpa_api_url or '').rstrip('/')}/v0/management/{path.lstrip('/')}"


def _cpa_headers(management_key: str) -> Dict[str, str]:
    return {"X-Management-Key": management_key}


def _cpa_ok(payload: dict, status_code: int) -> bool:
    status = str(payload.get("status") or payload.get("code") or "").lower()
    return 200 <= status_code < 300 and status in {"ok", "0", "success", ""}


def get_cpa_oauth_url(cpa_api_url: str, management_key: str) -> Dict[str, str]:
    """Generate OAuth URL metadata from CLIProxyAPI management API."""
    import requests as req

    if not cpa_api_url:
        raise RuntimeError("CPA api_url is required")
    if not management_key:
        raise RuntimeError("CPA management key is required")

    resp = req.get(
        _join_cpa_management_url(cpa_api_url, "codex-auth-url"),
        headers=_cpa_headers(management_key),
        timeout=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:300]}
    if not _cpa_ok(data, resp.status_code) or not (data.get("url") or data.get("auth_url")):
        raise RuntimeError(f"CPA OAuth URL failed: status={resp.status_code} data={data}")

    auth_url = data.get("url") or data.get("auth_url")
    state = data.get("state") or parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    return {"auth_url": auth_url, "oauth_url": auth_url, "state": state}


def complete_cpa_oauth_callback(cpa_api_url: str, management_key: str, code: str, state: str) -> Dict[str, object]:
    """Complete CPA native Codex OAuth callback."""
    import requests as req

    resp = req.post(
        _join_cpa_management_url(cpa_api_url, "oauth-callback"),
        json={"provider": "codex", "code": code, "state": state},
        headers=_cpa_headers(management_key),
        timeout=120,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:300]}
    return {"ok": _cpa_ok(data, resp.status_code), "status_code": resp.status_code, "data": data}


def list_cpa_auth_files(cpa_api_url: str, management_key: str) -> Dict[str, object]:
    """List CPA auth-files for best-effort upload verification."""
    import requests as req

    resp = req.get(
        _join_cpa_management_url(cpa_api_url, "auth-files"),
        headers=_cpa_headers(management_key),
        timeout=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:300]}
    return {"ok": _cpa_ok(data, resp.status_code), "status_code": resp.status_code, "data": data}


def upload_cpa_auth_file(cpa_api_url: str, management_key: str, auth_payload: dict, filename: str) -> Dict[str, object]:
    """Upload Codex auth JSON to CPA auth-files fallback endpoint."""
    import requests as req

    resp = req.post(
        _join_cpa_management_url(cpa_api_url, "auth-files"),
        json={"filename": filename, "content": auth_payload},
        headers=_cpa_headers(management_key),
        timeout=60,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"error": resp.text[:300]}
    return {"ok": _cpa_ok(data, resp.status_code), "status_code": resp.status_code, "data": data, "filename": filename}
```

- [ ] **Step 4: Run helper tests and existing SUB2API helper test**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_get_cpa_oauth_url_returns_url_and_state test_icloud_phase2.Phase2CodexTests.test_complete_cpa_oauth_callback_posts_provider_code_and_state test_icloud_phase2.Phase2CodexTests.test_upload_cpa_auth_file_posts_codex_payload test_icloud_phase2.Phase2CodexTests.test_phase2_codex_get_oauth_url_returns_auth_url_session_and_state -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add phase2_codex.py test_icloud_phase2.py
git commit -m "feat: add cpa management api helpers"
```

---

## Task 4: Add target-aware Phase2 upload branching

**Files:**
- Modify: `openai_bind_email.py:66-128`
- Modify: `openai_bind_email.py:630-960`
- Modify: `phase2_codex.py:16-63`
- Test: `test_icloud_phase2.py`

- [ ] **Step 1: Write failing unit tests for target branching**

Add this helper and tests to the Phase2 tests in `test_icloud_phase2.py`:

```python
    def test_phase2_codex_codex_login_forwards_upload_target_and_cpa_config(self):
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "uploaded": True, "upload_target": "cpa"}

        with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
            result = phase2_codex.codex_login(
                session_token="session-1",
                phone="+15551234567",
                password="pw-123",
                bind_email="target@outlook.com",
                oauth_url={"auth_url": "https://auth.openai.com/oauth/authorize?state=cpa-state", "state": "cpa-state"},
                icloud_cookies={},
                upload_target="cpa",
                cpa_api_url="https://cpa.example.com",
                cpa_management_key="mgmt-key",
                cpa_upload_mode="auto",
                verbose=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["upload_target"], "cpa")
        self.assertEqual(captured["cpa_api_url"], "https://cpa.example.com")
        self.assertEqual(captured["cpa_management_key"], "mgmt-key")
        self.assertEqual(captured["cpa_oauth_state"], "cpa-state")

    def test_run_second_half_cpa_callback_success_returns_uploaded(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": True, "data": {"status": "ok"}}):
                result = openai_bind_email.run_second_half(
                    oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                    phone="+15551234567",
                    password="pw",
                    icloud_email="user@example.com",
                    icloud_cookies={},
                    upload_target="cpa",
                    cpa_api_url="https://cpa.example.com",
                    cpa_management_key="mgmt-key",
                    cpa_oauth_state="cpa-state",
                    verbose=False,
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["upload_target"], "cpa")

    def test_run_second_half_cpa_callback_failure_falls_back_to_auth_file(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": False, "data": {"error": "bad"}}):
                with mock.patch("phase2_codex.upload_cpa_auth_file", return_value={"ok": True, "filename": "codex-user.json"}):
                    result = openai_bind_email.run_second_half(
                        oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                        phone="+15551234567",
                        password="pw",
                        icloud_email="user@example.com",
                        icloud_cookies={},
                        upload_target="cpa",
                        cpa_api_url="https://cpa.example.com",
                        cpa_management_key="mgmt-key",
                        cpa_oauth_state="cpa-state",
                        access_token="at",
                        refresh_token="rt",
                        verbose=False,
                    )

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["upload_method"], "cpa_auth_file")

    def test_run_second_half_cpa_final_failure_saves_failed_upload(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": False, "data": {"error": "bad"}}):
                with mock.patch("phase2_codex.upload_cpa_auth_file", return_value={"ok": False, "data": {"error": "bad fallback"}}):
                    with mock.patch("failed_uploads.save_failed_upload", return_value="failed_uploads/fail.json") as save_mock:
                        result = openai_bind_email.run_second_half(
                            oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                            phone="+15551234567",
                            password="pw",
                            icloud_email="user@example.com",
                            icloud_cookies={},
                            upload_target="cpa",
                            cpa_api_url="https://cpa.example.com",
                            cpa_management_key="mgmt-key",
                            cpa_oauth_state="cpa-state",
                            access_token="at",
                            verbose=False,
                        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["failed_upload_file"], "failed_uploads/fail.json")
        saved_record = save_mock.call_args.args[0]
        self.assertEqual(saved_record["phone"], "+15551234567")
        self.assertEqual(saved_record["access_token"], "at")
        self.assertNotIn("cpa_management_key", saved_record)
```

Also ensure `test_icloud_phase2.py` imports `openai_bind_email` if it does not already:

```python
import openai_bind_email
```

- [ ] **Step 2: Run new tests and verify they fail**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_phase2_codex_codex_login_forwards_upload_target_and_cpa_config test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_callback_success_returns_uploaded test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_callback_failure_falls_back_to_auth_file test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_final_failure_saves_failed_upload -v
```

Expected: FAIL because new parameters/CPA branch do not exist.

- [ ] **Step 3: Extend `phase2_codex.codex_login` signature and forwarding**

Change the signature in `phase2_codex.py` to add CPA parameters after `sub2api_state`:

```python
    upload_target: str = "sub2api",
    cpa_api_url: str = "",
    cpa_management_key: str = "",
    cpa_upload_mode: str = "auto",
    cpa_oauth_state: str = "",
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
```

Inside the `if isinstance(oauth_url, dict):` block, add:

```python
        cpa_oauth_state = cpa_oauth_state or oauth_info.get("state", "")
```

In the `run_second_half(...)` call, add:

```python
        upload_target=upload_target,
        cpa_api_url=cpa_api_url,
        cpa_management_key=cpa_management_key,
        cpa_upload_mode=cpa_upload_mode,
        cpa_oauth_state=cpa_oauth_state,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
```

- [ ] **Step 4: Refactor upload helpers in `openai_bind_email.py`**

Add these helper functions after `_find_sub2api_account_id(...)`:

```python

def _stable_codex_filename(email: str, phone: str = "") -> str:
    label = re.sub(r"[^A-Za-z0-9_.@-]+", "-", (email or phone or "account")).strip(".-_") or "account"
    return f"codex-{label[:80]}.json"


def _build_cpa_auth_payload(
    email: str,
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
    expires_at: Any = 0,
) -> Dict[str, Any]:
    payload = {"type": "codex", "email": email or ""}
    if access_token:
        payload["access_token"] = access_token
    if refresh_token:
        payload["refresh_token"] = refresh_token
    if id_token:
        payload["id_token"] = id_token
    if account_id:
        payload["account_id"] = account_id
    if expires_at:
        payload["expired"] = str(expires_at)
    payload["last_refresh"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload
```

Add this import near existing imports:

```python
from datetime import datetime, timezone
```

Move the current SUB2API `[11]` body into a helper named `_upload_sub2api_from_code(...)` with this signature:

```python
def _upload_sub2api_from_code(
    req_lib,
    log,
    sub2api_url: str,
    sub2api_email: str,
    sub2api_password: str,
    sub2api_session_id: str,
    sub2api_state: str,
    sub2api_group_id: int,
    code: str,
    icloud_email: str,
) -> Dict[str, Any]:
```

The helper should contain the exact existing logic from `resp = req_lib.post(.../auth/login...)` through `return {"ok": True, "code": code, "sub2api_account_id": account_id}`.

- [ ] **Step 5: Extend `run_second_half(...)` signature**

Add parameters after `outlook_pool: str = "",`:

```python
    upload_target: str = "sub2api",
    cpa_api_url: str = "",
    cpa_management_key: str = "",
    cpa_upload_mode: str = "auto",
    cpa_oauth_state: str = "",
    session_token: str = "",
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
    expires_at: Any = 0,
```

- [ ] **Step 6: Replace final `[11]` branch with target-aware logic**

Replace the existing `# ---- [11] code → exchange-code → SUB2API 账号 ----` block with:

```python
        upload_target = str(upload_target or "sub2api").lower()

        if upload_target == "cpa":
            log("[11] CPA native OAuth import ...")
            from phase2_codex import complete_cpa_oauth_callback, upload_cpa_auth_file
            from failed_uploads import save_failed_upload

            callback_state = cpa_oauth_state or sub2api_state or parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
            native_result = complete_cpa_oauth_callback(cpa_api_url, cpa_management_key, code, callback_state)
            if native_result.get("ok"):
                return {
                    "ok": True,
                    "code": code,
                    "uploaded": True,
                    "upload_target": "cpa",
                    "upload_method": "cpa_oauth_callback",
                    "cpa_result": native_result,
                }

            log(f"[11] CPA native import failed, fallback to auth-files: {native_result}")
            auth_payload = _build_cpa_auth_payload(
                email=icloud_email,
                access_token=access_token,
                refresh_token=refresh_token,
                id_token=id_token,
                account_id=account_id,
                expires_at=expires_at,
            )
            fallback_result = upload_cpa_auth_file(
                cpa_api_url,
                cpa_management_key,
                auth_payload,
                _stable_codex_filename(icloud_email, phone),
            )
            if fallback_result.get("ok"):
                return {
                    "ok": True,
                    "code": code,
                    "uploaded": True,
                    "upload_target": "cpa",
                    "upload_method": "cpa_auth_file",
                    "cpa_result": fallback_result,
                }

            error = f"CPA upload failed: native={native_result} fallback={fallback_result}"
            failed_file = save_failed_upload(
                {
                    "upload_target": "cpa",
                    "upload_mode": cpa_upload_mode or "auto",
                    "phone": phone,
                    "email": icloud_email,
                    "session_token": session_token,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "id_token": id_token,
                    "account_id": account_id,
                    "expires_at": expires_at,
                    "oauth_state": callback_state,
                    "last_error": error,
                    "attempts": 1,
                }
            )
            return {
                "ok": True,
                "code": code,
                "uploaded": False,
                "upload_target": "cpa",
                "upload_error": error,
                "failed_upload_file": failed_file,
            }

        if sub2api_url and sub2api_email and sub2api_session_id:
            import requests as req_lib
            sub_result = _upload_sub2api_from_code(
                req_lib=req_lib,
                log=log,
                sub2api_url=sub2api_url,
                sub2api_email=sub2api_email,
                sub2api_password=sub2api_password,
                sub2api_session_id=sub2api_session_id,
                sub2api_state=sub2api_state,
                sub2api_group_id=sub2api_group_id,
                code=code,
                icloud_email=icloud_email,
            )
            if sub_result.get("ok"):
                sub_result["uploaded"] = True
                sub_result["upload_target"] = "sub2api"
            return sub_result

        log("[11] 无上传配置, 仅返回 code")
        return {"ok": True, "code": code, "uploaded": False}
```

If the helper uses `_time.sleep`, import `time as _time` inside `_upload_sub2api_from_code(...)`.

- [ ] **Step 7: Run target branching tests**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_phase2_codex_codex_login_forwards_upload_target_and_cpa_config test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_callback_success_returns_uploaded test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_callback_failure_falls_back_to_auth_file test_icloud_phase2.Phase2CodexTests.test_run_second_half_cpa_final_failure_saves_failed_upload test_icloud_phase2.Phase2CodexTests.test_phase2_codex_login_forwards_session_and_state -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add openai_bind_email.py phase2_codex.py test_icloud_phase2.py
git commit -m "feat: branch phase2 uploads by target"
```

---

## Task 5: Add WebUI config fields and round-trip behavior

**Files:**
- Modify: `web_gui.py:214-271`
- Modify: `web_gui.py:2648-2680`
- Modify: `web_gui.py:3247-3265`
- Modify: `web_gui.py:3374-3412`
- Test: `test_web_gui_stats.py`

- [ ] **Step 1: Write failing WebUI config round-trip test**

Add this test to `WebGuiStatsTests` in `test_web_gui_stats.py`:

```python
    def test_api_config_roundtrips_upload_target_and_cpa_settings(self):
        payload = {
            "upload_target": "cpa",
            "cpa_management_url": "http://47.89.129.103:18317",
            "cpa_api_url": "http://47.89.129.103:8317",
            "cpa_management_key": "mgmt-key",
            "cpa_upload_mode": "auto",
        }

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json=payload)
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])

            resp = client.get("/api/config")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        cfg = data["config"]
        self.assertEqual(cfg["upload_target"], "cpa")
        self.assertEqual(cfg["cpa"]["management_url"], "http://47.89.129.103:18317")
        self.assertEqual(cfg["cpa"]["api_url"], "http://47.89.129.103:8317")
        self.assertEqual(cfg["cpa"]["management_key"], "mgmt-key")
        self.assertEqual(cfg["cpa"]["upload_mode"], "auto")
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_api_config_roundtrips_upload_target_and_cpa_settings -v
```

Expected: FAIL because `/api/config` ignores CPA fields.

- [ ] **Step 3: Update `/api/config` POST field list and assignments**

In `web_gui.py`, extend the `for k in [...]` list around `api_config()` with:

```python
                "upload_target", "cpa_management_url", "cpa_api_url", "cpa_management_key", "cpa_upload_mode",
```

Add assignment branches after the SUB2API branches:

```python
                elif k == "upload_target":
                    target = str(d[k] or "sub2api").lower()
                    cfg["upload_target"] = target if target in ("sub2api", "cpa") else "sub2api"
                elif k == "cpa_management_url":
                    cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["management_url"] = d[k]
                elif k == "cpa_api_url":
                    cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["api_url"] = d[k]
                elif k == "cpa_management_key":
                    cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["management_key"] = d[k]
                elif k == "cpa_upload_mode":
                    cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["upload_mode"] = d[k] or "auto"
```

Before `_state["config"] = cfg`, add normalization:

```python
        cfg["upload_target"] = str(cfg.get("upload_target") or "sub2api").lower()
        if cfg["upload_target"] not in ("sub2api", "cpa"):
            cfg["upload_target"] = "sub2api"
        cpa = dict(cfg.get("cpa") or {})
        cfg["cpa"] = {
            "management_url": cpa.get("management_url", ""),
            "api_url": cpa.get("api_url", ""),
            "management_key": cpa.get("management_key", ""),
            "upload_mode": cpa.get("upload_mode", "auto") or "auto",
        }
```

- [ ] **Step 4: Update HTML Phase2 card**

In `_HTML`, change card title from:

```html
<div class="card"><h2>Phase 2: 邮箱 &amp; SUB2API</h2>
```

to:

```html
<div class="card"><h2>Phase 2: 邮箱 &amp; 上传</h2>
```

Insert this block before the existing `SUB2API 地址` label:

```html
          <label>上传平台</label>
          <select id="upload_target" onchange="toggleUploadTargetFields()">
            <option value="sub2api">SUB2API</option>
            <option value="cpa">CPA</option>
          </select>
          <div id="sub2api-group">
```

Add `</div>` after the existing `目标分组` input:

```html
          <label>目标分组</label><input id="sub2api_group" value="CHATGPT">
          </div>
          <div id="cpa-group" style="display:none">
            <label>CPA 管理地址</label><input id="cpa_management_url" placeholder="http://47.89.129.103:18317">
            <label>CPA API 地址</label><input id="cpa_api_url" placeholder="http://47.89.129.103:8317">
            <label>CPA 管理密钥</label><input id="cpa_management_key" type="password">
            <label>CPA 上传模式</label><input id="cpa_upload_mode" value="auto" readonly>
          </div>
```

- [ ] **Step 5: Update WebUI JavaScript save/load**

Add this function near `toggleEmailProviderFields()`:

```javascript
function toggleUploadTargetFields(){
  var v=G('upload_target').value||'sub2api';
  G('sub2api-group').style.display=(v=='sub2api'?'':'none');
  G('cpa-group').style.display=(v=='cpa'?'':'none');
}
```

In `saveConfig()`, add fields:

```javascript
    upload_target:G('upload_target').value,
    cpa_management_url:G('cpa_management_url').value,
    cpa_api_url:G('cpa_api_url').value,
    cpa_management_key:G('cpa_management_key').value,
    cpa_upload_mode:G('cpa_upload_mode').value,
```

In `loadConfig()`, after SUB2API loading, add:

```javascript
    G('upload_target').value=c.upload_target||'sub2api';
    if(c.cpa){G('cpa_management_url').value=c.cpa.management_url||'';G('cpa_api_url').value=c.cpa.api_url||'';G('cpa_management_key').value=c.cpa.management_key||'';G('cpa_upload_mode').value=c.cpa.upload_mode||'auto';}
    toggleUploadTargetFields();
```

- [ ] **Step 6: Run tests and verify pass**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_api_config_roundtrips_upload_target_and_cpa_settings -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web_gui.py test_web_gui_stats.py
git commit -m "feat: add cpa settings to web ui config"
```

---

## Task 6: Use target-aware OAuth URL generation in WebUI Phase2

**Files:**
- Modify: `web_gui.py:991-1074`
- Modify: `web_gui.py:1130-1250`
- Modify: `web_gui.py:2155-2445`
- Test: `test_web_gui_stats.py`

- [ ] **Step 1: Write failing test for CPA Phase2 preparation**

Add this test to `WebGuiStatsTests`:

```python
    def test_phase2_for_result_uses_cpa_oauth_url_and_passes_cpa_config(self):
        config = {
            "upload_target": "cpa",
            "bind_email": "target@outlook.com",
            "cpa": {
                "management_url": "http://mgmt.example.com",
                "api_url": "https://cpa.example.com",
                "management_key": "mgmt-key",
                "upload_mode": "auto",
            },
            "proxy": "",
            "outlook_pool": "",
        }
        account = {
            "phone": "+15551234567",
            "password": "pw",
            "session_token": "session-token",
            "access_token": "access-token",
        }
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "uploaded": True, "upload_target": "cpa"}

        with mock.patch("phase2_codex.get_cpa_oauth_url", return_value={"auth_url": "https://auth.openai.com/oauth/authorize?state=cpa-state", "state": "cpa-state"}):
            with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
                result = web_gui._phase2_for_result(account, config)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["upload_target"], "cpa")
        self.assertEqual(captured["cpa_api_url"], "https://cpa.example.com")
        self.assertEqual(captured["cpa_management_key"], "mgmt-key")
        self.assertEqual(captured["cpa_oauth_state"], "cpa-state")
        self.assertEqual(captured["session_token"], "session-token")
        self.assertEqual(captured["access_token"], "access-token")
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_phase2_for_result_uses_cpa_oauth_url_and_passes_cpa_config -v
```

Expected: FAIL because `_phase2_for_result` always logs into SUB2API.

- [ ] **Step 3: Add a shared WebUI Phase2 helper**

In `web_gui.py`, replace the final duplicate `_phase2_for_result(...)` implementation with a target-aware implementation and delete the earlier duplicate if both definitions remain. Use this full function:

```python
def _phase2_for_result(result: dict, config: dict, thread_tag: str = "", thread_id=None) -> dict:
    """Run Phase 2 for one registered account and upload to selected target."""
    upload_target = str(config.get("upload_target") or "sub2api").lower()
    bind_email = (config.get("bind_email") or "").strip()
    if not bind_email:
        raise RuntimeError("bind_email is not configured")

    tlog = lambda msg, tag="info": _log(msg, tag, thread_id=thread_id)
    icloud_cookies = _load_phase2_icloud_cookies(config)

    if upload_target == "cpa":
        from phase2_codex import get_cpa_oauth_url
        from openai_bind_email import run_second_half

        cpa = config.get("cpa", {})
        if not cpa.get("api_url"):
            raise RuntimeError("CPA API 地址未配置")
        if not cpa.get("management_key"):
            raise RuntimeError("CPA 管理密钥未配置")
        tlog(f"{thread_tag} [1/4] 获取 CPA OAuth URL ...".strip(), "info")
        oauth_info = get_cpa_oauth_url(cpa.get("api_url", ""), cpa.get("management_key", ""))
        state_preview = (oauth_info.get("state") or "")[:8]
        tlog(f"{thread_tag} [2/4] CPA state={state_preview}...".strip(), "info")
        return run_second_half(
            oauth_url=oauth_info["auth_url"],
            phone=result["phone"],
            password=result["password"],
            icloud_email=bind_email,
            icloud_cookies=icloud_cookies,
            proxy=config.get("proxy", ""),
            verbose=True,
            outlook_pool=config.get("outlook_pool", ""),
            upload_target="cpa",
            cpa_api_url=cpa.get("api_url", ""),
            cpa_management_key=cpa.get("management_key", ""),
            cpa_upload_mode=cpa.get("upload_mode", "auto"),
            cpa_oauth_state=oauth_info.get("state", ""),
            session_token=result.get("session_token", ""),
            access_token=result.get("access_token", ""),
            refresh_token=result.get("refresh_token", ""),
            id_token=result.get("id_token", ""),
            account_id=result.get("account_id", ""),
            expires_at=result.get("expires_at", 0),
        )

    import requests as _r
    import urllib.parse as _up
    from openai_bind_email import run_second_half

    sub = config.get("sub2api", {})
    tlog(f"{thread_tag} [1/4] 登录 SUB2API ...".strip(), "info")
    login_resp = _r.post(
        f"{sub['url']}/api/v1/auth/login",
        json={"email": sub["email"], "password": sub.get("pwd", "")},
        timeout=15,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API登录失败: {login_data.get('message', '?')}")
    admin_token = login_data["data"]["access_token"]

    tlog(f"{thread_tag} [2/4] 获取 OAuth URL ...".strip(), "info")
    body = {"redirect_uri": "http://localhost:1455/auth/callback"}
    proxy_id = int(config.get("sub2api", {}).get("proxy_id", 0) or 0)
    if proxy_id:
        body["proxy_id"] = proxy_id
    auth_resp = _r.post(
        f"{sub['url']}/api/v1/admin/openai/generate-auth-url",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    auth_data = auth_resp.json()
    if auth_data.get("code") != 0:
        raise RuntimeError(f"获取OAuth URL失败: {auth_data.get('message', '?')}")
    oauth_url = auth_data["data"]["auth_url"]
    session_id = auth_data["data"]["session_id"]
    oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]

    group_id = 1
    group_name = config.get("sub2api", {}).get("group", "CHATGPT")
    try:
        group_resp = _r.get(
            f"{sub['url']}/api/v1/admin/groups",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=15,
        )
        groups = group_resp.json().get("data", {}).get("items", [])
        for g in groups:
            if g.get("name") == group_name:
                group_id = g.get("id", 1)
                break
    except Exception as e:
        tlog(f"{thread_tag} [2/4] 查询分组失败: {e}".strip(), "warn")

    tlog(f"{thread_tag} [3/4] OAuth 流程: 登录 -> 绑邮箱 -> 验证 -> 同意 -> code ...".strip(), "info")
    return run_second_half(
        oauth_url=oauth_url,
        phone=result["phone"],
        password=result["password"],
        icloud_email=bind_email,
        icloud_cookies=icloud_cookies,
        sub2api_url=sub["url"],
        sub2api_email=sub["email"],
        sub2api_password=sub.get("pwd", ""),
        proxy=config.get("proxy", ""),
        verbose=True,
        sub2api_session_id=session_id,
        sub2api_state=oauth_state,
        outlook_pool=config.get("outlook_pool", ""),
        sub2api_proxy_id=proxy_id,
        sub2api_group_id=group_id,
        upload_target="sub2api",
        session_token=result.get("session_token", ""),
        access_token=result.get("access_token", ""),
    )
```

- [ ] **Step 4: Replace duplicated inline Phase2 code in `_run` and batch workers**

In each worker path currently defining `_do_phase2_once()` with direct SUB2API login, replace the body with:

```python
                    def _do_phase2_once():
                        return _phase2_for_result(result, thread_cfg, thread_tag=tag, thread_id=thread_id)
```

For batch/retry worker paths that call `_phase2_for_result(...)` already, ensure they pass `thread_cfg` and do not pre-check only SUB2API when `upload_target == "cpa"`.

- [ ] **Step 5: Update target pre-checks**

Replace checks like:

```python
    sub = run_config.get("sub2api", {})
    if not run_config.get("no_phase2") and sub.get("url") and sub.get("email"):
```

with:

```python
    upload_target = str(run_config.get("upload_target") or "sub2api").lower()
    sub = run_config.get("sub2api", {})
    cpa = run_config.get("cpa", {})
    phase2_enabled = not run_config.get("no_phase2")
    phase2_configured = (
        upload_target == "cpa" and cpa.get("api_url") and cpa.get("management_key")
    ) or (
        upload_target == "sub2api" and sub.get("url") and sub.get("email")
    )
```

Use `phase2_enabled and phase2_configured` for email pool pre-checks. Add startup errors:

```python
    if phase2_enabled and upload_target == "cpa" and not cpa.get("management_key"):
        _log("CPA 管理密钥未配置", "error")
        with _STATE_LOCK:
            _state["running"] = False
        return
    if phase2_enabled and upload_target == "cpa" and not cpa.get("api_url"):
        _log("CPA API 地址未配置", "error")
        with _STATE_LOCK:
            _state["running"] = False
        return
```

- [ ] **Step 6: Run WebUI Phase2 test**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_phase2_for_result_uses_cpa_oauth_url_and_passes_cpa_config -v
```

Expected: PASS.

- [ ] **Step 7: Run WebUI stats tests**

Run:

```bash
python -m unittest test_web_gui_stats -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add web_gui.py test_web_gui_stats.py
git commit -m "feat: run web phase2 with selected upload target"
```

---

## Task 7: Preserve upload failure state in WebUI results

**Files:**
- Modify: `web_gui.py:1217-1250`
- Modify: `web_gui.py:1658-1688`
- Modify: `web_gui.py:2037-2085`
- Modify: `web_gui.py:2434-2445`
- Modify: `web_gui.py:1739-1743`
- Test: `test_web_gui_stats.py`

- [ ] **Step 1: Write failing result-mapping test**

Add this test to `WebGuiStatsTests`:

```python
    def test_sanitize_result_preserves_upload_failure_fields(self):
        result = web_gui._sanitize_result(
            {
                "ok": True,
                "phone": "+15551234567",
                "session_token": "s" * 60,
                "access_token": "a" * 60,
                "uploaded": False,
                "upload_error": "CPA upload failed",
                "failed_upload_file": "failed_uploads/fail.json",
                "upload_target": "cpa",
            }
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["upload_error"], "CPA upload failed")
        self.assertEqual(result["failed_upload_file"], "failed_uploads/fail.json")
        self.assertEqual(result["upload_target"], "cpa")
        self.assertTrue(result["access_token"].endswith("..."))
```

- [ ] **Step 2: Run test and verify current behavior**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_sanitize_result_preserves_upload_failure_fields -v
```

Expected: PASS if `_sanitize_result` already keeps unknown fields, or FAIL if fields are dropped. Continue with the implementation either way because worker result mapping still needs updating.

- [ ] **Step 3: Update successful Phase2 result mapping**

For each place where worker code currently does:

```python
result["uploaded"] = True
result["final_ok"] = True
result["ok"] = True
result["status"] = "final_ok"
```

replace it with target-aware mapping:

```python
result["bind_email"] = thread_cfg.get("bind_email", "")
result["email_bound"] = True
result["uploaded"] = bool(oauth_result.get("uploaded", True))
result["upload_target"] = oauth_result.get("upload_target", thread_cfg.get("upload_target", "sub2api"))
if oauth_result.get("sub2api_account_id"):
    result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
if oauth_result.get("upload_error"):
    result["upload_error"] = oauth_result.get("upload_error", "")
if oauth_result.get("failed_upload_file"):
    result["failed_upload_file"] = oauth_result.get("failed_upload_file", "")
result["final_ok"] = bool(result["uploaded"])
result["ok"] = True
result["status"] = "final_ok" if result["uploaded"] else "upload_failed"
```

Important: if `oauth_result.get("ok")` is `True` but `uploaded` is `False`, do not retry Phase2 as an OpenAI failure. Treat the account generation/binding as successful and store it with `status="upload_failed"`.

- [ ] **Step 4: Update logs for upload failure**

When `oauth_result.get("ok")` is true but `uploaded` is false, log:

```python
_log(f"{tag}   [4/4] 账号生成成功，但上传失败: {result.get('upload_error', '?')} 文件={result.get('failed_upload_file', '-')}", "warn")
```

When uploaded true with CPA:

```python
_log(f"{tag}   [4/4] CPA 上传成功 ({oauth_result.get('upload_method', 'unknown')})", "success")
```

Keep SUB2API success log for SUB2API.

- [ ] **Step 5: Ensure `_save_result` persists fields**

`_save_result(...)` already writes a full copy of `result`. Confirm it does not filter `uploaded`, `upload_error`, or `failed_upload_file`. No code change is needed if it still writes `safe = dict(result)`.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m unittest test_web_gui_stats -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web_gui.py test_web_gui_stats.py
git commit -m "feat: surface upload failures in web results"
```

---

## Task 8: Add SUB2API final-failure local save

**Files:**
- Modify: `openai_bind_email.py:869-960`
- Test: `test_icloud_phase2.py`

- [ ] **Step 1: Write failing SUB2API failed-save test**

Add this test to `Phase2CodexTests`:

```python
    def test_run_second_half_sub2api_upload_failure_saves_failed_upload(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"

            login_resp = mock.Mock(status_code=200)
            login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}
            exchange_resp = mock.Mock(status_code=500, text="server error")

            with mock.patch("requests.post", side_effect=[login_resp, exchange_resp]):
                with mock.patch("failed_uploads.save_failed_upload", return_value="failed_uploads/sub.json") as save_mock:
                    result = openai_bind_email.run_second_half(
                        oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=sub-state",
                        phone="+15551234567",
                        password="pw",
                        icloud_email="user@example.com",
                        icloud_cookies={},
                        sub2api_url="https://sub2api.example.com",
                        sub2api_email="admin@example.com",
                        sub2api_password="secret",
                        sub2api_session_id="sid-123",
                        sub2api_state="sub-state",
                        session_token="session-token",
                        access_token="access-token",
                        verbose=False,
                    )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["failed_upload_file"], "failed_uploads/sub.json")
        saved_record = save_mock.call_args.args[0]
        self.assertEqual(saved_record["upload_target"], "sub2api")
        self.assertEqual(saved_record["session_token"], "session-token")
        self.assertEqual(saved_record["access_token"], "access-token")
        self.assertNotIn("sub2api_password", saved_record)
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_run_second_half_sub2api_upload_failure_saves_failed_upload -v
```

Expected: FAIL because SUB2API upload failures return `ok=False` and do not save a failed-upload file.

- [ ] **Step 3: Wrap SUB2API upload branch final failures**

In `run_second_half(...)`, inside the SUB2API branch after `_upload_sub2api_from_code(...)`, replace direct return with:

```python
            try:
                sub_result = _upload_sub2api_from_code(...)
            except Exception as upload_exc:
                sub_result = {"ok": False, "error": str(upload_exc)}

            if sub_result.get("ok"):
                sub_result["uploaded"] = True
                sub_result["upload_target"] = "sub2api"
                return sub_result

            from failed_uploads import save_failed_upload
            error = sub_result.get("error") or str(sub_result)
            failed_file = save_failed_upload(
                {
                    "upload_target": "sub2api",
                    "upload_mode": "auto",
                    "phone": phone,
                    "email": icloud_email,
                    "session_token": session_token,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "id_token": id_token,
                    "account_id": account_id,
                    "expires_at": expires_at,
                    "oauth_state": sub2api_state,
                    "last_error": error,
                    "attempts": 1,
                }
            )
            return {
                "ok": True,
                "code": code,
                "uploaded": False,
                "upload_target": "sub2api",
                "upload_error": error,
                "failed_upload_file": failed_file,
            }
```

Use the actual `_upload_sub2api_from_code(...)` call arguments from Task 4.

- [ ] **Step 4: Ensure OpenAI/bind failures still return `ok=False`**

Do not wrap earlier failures (`initiate_oauth`, `submit_phone`, `verify_password`, `send_bind_email`, binding OTP, no auth code). Only upload-step failures after `code` is obtained should become `ok=True, uploaded=False`.

- [ ] **Step 5: Run SUB2API failed-save test and existing SUB2API tests**

Run:

```bash
python -m unittest test_icloud_phase2.Phase2CodexTests.test_run_second_half_sub2api_upload_failure_saves_failed_upload test_icloud_phase2.Phase2CodexTests.test_phase2_codex_login_forwards_session_and_state test_icloud_phase2.Phase2CodexTests.test_phase2_codex_get_oauth_url_returns_auth_url_session_and_state -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add openai_bind_email.py test_icloud_phase2.py
git commit -m "feat: save sub2api upload failures locally"
```

---

## Task 9: Add minimal retry API boundary for failed upload files

**Files:**
- Modify: `web_gui.py`
- Test: `test_web_gui_stats.py`

- [ ] **Step 1: Write failing API test**

Add this test to `WebGuiStatsTests`:

```python
    def test_api_retry_failed_upload_calls_retry_function(self):
        web_gui._state["config"] = {
            "upload_target": "cpa",
            "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
        }

        with mock.patch("failed_uploads.retry_failed_upload", return_value={"ok": True}) as retry_mock:
            with web_gui.app.test_client() as client:
                resp = client.post("/api/failed-uploads/retry", json={"path": "failed_uploads/fail.json"})

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        retry_mock.assert_called_once_with(Path("failed_uploads/fail.json"), web_gui._state["config"])
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_api_retry_failed_upload_calls_retry_function -v
```

Expected: FAIL with 404.

- [ ] **Step 3: Add retry endpoint**

Add this route near other API routes in `web_gui.py`:

```python
@app.route("/api/failed-uploads/retry", methods=["POST"])
def api_failed_upload_retry():
    d = request.json or {}
    path = d.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        from failed_uploads import retry_failed_upload
        result = retry_failed_upload(Path(path), _state.get("config", {}))
        return jsonify({"ok": bool(result.get("ok")), "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```

This endpoint is intentionally minimal; the first release only needs a callable boundary for future UI, while result rows already show `failed_upload_file`.

- [ ] **Step 4: Run retry API test**

Run:

```bash
python -m unittest test_web_gui_stats.WebGuiStatsTests.test_api_retry_failed_upload_calls_retry_function -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_gui.py test_web_gui_stats.py
git commit -m "feat: add failed upload retry api"
```

---

## Task 10: Full regression and manual acceptance

**Files:**
- No code files expected.
- May update plan notes only if a test exposes a mismatch.

- [ ] **Step 1: Run unit regression suite**

Run:

```bash
python -m unittest test_auto_register_retry test_web_gui_stats test_icloud_phase2 test_failed_uploads -v
```

Expected: PASS.

- [ ] **Step 2: Run syntax check for modified Python files**

Run:

```bash
python -m py_compile auto_register.py phase2_codex.py openai_bind_email.py failed_uploads.py web_gui.py
```

Expected: command exits with code 0 and prints nothing.

- [ ] **Step 3: Start WebUI locally**

Run:

```bash
python auto_register.py --gui
```

Expected: WebUI starts on `http://127.0.0.1:7777`.

- [ ] **Step 4: Manual config acceptance for SUB2API compatibility**

In the WebUI:

1. Select upload platform `SUB2API`.
2. Fill existing SUB2API fields.
3. Save config.
4. Reload page.
5. Confirm SUB2API fields retain values and CPA fields are hidden.

Expected: old SUB2API UI behavior remains available.

- [ ] **Step 5: Manual config acceptance for CPA**

In the WebUI:

1. Select upload platform `CPA`.
2. Fill:
   - CPA 管理地址: `http://47.89.129.103:18317`
   - CPA API 地址: `http://47.89.129.103:8317`
   - CPA 管理密钥: use the user's current key from local config input, not committed files.
   - CPA 上传模式: `auto`
3. Save config.
4. Reload page.

Expected: CPA fields retain values; `config.json` contains `upload_target: "cpa"` and `cpa` config.

- [ ] **Step 6: Manual CPA API smoke test without registering an account**

Run this Python one-liner from repo root after saving CPA config:

```bash
python - <<'PY'
import auto_register
from phase2_codex import get_cpa_oauth_url
cfg = auto_register.load_config()
cpa = cfg['cpa']
info = get_cpa_oauth_url(cpa['api_url'], cpa['management_key'])
print(info['state'][:8], info['auth_url'][:80])
PY
```

Expected: prints an 8-character state prefix and an OpenAI auth URL prefix.

- [ ] **Step 7: Manual failed-upload persistence smoke test**

Run:

```bash
python - <<'PY'
from pathlib import Path
from failed_uploads import save_failed_upload, load_failed_upload
path = save_failed_upload({'upload_target':'cpa','phone':'+1000','email':'smoke@example.com','access_token':'at','management_key':'must-not-save','last_error':'smoke'})
print(path)
record = load_failed_upload(path)
print('management_key' in record, record['upload_target'], record['email'])
PY
```

Expected:

```text
failed_uploads/<timestamp>_codex_smoke@example.com_<id>.json
False cpa smoke@example.com
```

- [ ] **Step 8: Confirm git status**

Run:

```bash
git status --short
```

Expected: clean working tree after all commits, or only expected local runtime files like `config.json`, `results/`, `failed_uploads/`, and `logs/` remain untracked/modified.

---

## Self-Review

**Spec coverage:**

- WebUI supports choosing `SUB2API` or `CPA`: Tasks 5 and 6.
- SUB2API behavior remains compatible: Tasks 1, 3, 4, 6, 8, 10.
- CPA native OAuth import first: Tasks 3, 4, 6.
- CPA auth-files fallback: Tasks 3 and 4.
- Multi-thread independent OAuth state: Task 6 calls `get_cpa_oauth_url(...)` per `_phase2_for_result(...)`, and each worker passes its own `thread_cfg`/result.
- Generated account but upload failed saves local JSON: Tasks 2, 4, 7, 8.
- No management secrets in failed files: Task 2 tests and `_SECRET_KEYS`.
- Retry boundary exists: Tasks 2 and 9.
- WebUI displays upload status data through result fields: Task 7.

**Placeholder scan:**

No `TBD`, `TODO`, “implement later”, or “similar to” placeholders are present. Every code-changing step includes exact code or exact transformation text.

**Type/signature consistency:**

- `upload_target`, `cpa_api_url`, `cpa_management_key`, `cpa_upload_mode`, and `cpa_oauth_state` are consistently named across `phase2_codex.codex_login(...)`, `openai_bind_email.run_second_half(...)`, and `web_gui._phase2_for_result(...)`.
- Result fields are consistently named: `uploaded`, `upload_target`, `upload_method`, `upload_error`, `failed_upload_file`, `sub2api_account_id`.
- Failed upload records use the spec’s schema keys and exclude management secrets.
