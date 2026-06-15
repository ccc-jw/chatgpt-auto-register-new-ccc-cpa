import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import requests

try:
    from phase2_codex import upload_cpa_auth_file, upload_session
except (ImportError, AttributeError):
    upload_cpa_auth_file = None  # type: ignore
    upload_session = None  # type: ignore

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
    "upload_method",
    "phone",
    "email",
    "bind_email",
    "session_token",
    "access_token",
    "refresh_token",
    "id_token",
    "account_id",
    "sub2api_account_id",
    "expires_at",
    "expired",
    "oauth_state",
    "group_id",
    "group_ids",
    "last_error",
    "verify_error",
    "upload_verified",
    "needs_retry",
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


def _sub2api_group_ids(record: Dict[str, Any], config: Dict[str, Any], admin_token: str = "") -> list:
    if isinstance(record.get("group_ids"), list) and record.get("group_ids"):
        return [int(g) for g in record["group_ids"]]
    if record.get("group_id"):
        return [int(record["group_id"])]
    sub = config.get("sub2api", {})
    group_name = str(sub.get("group") or "").strip()
    if group_name and admin_token:
        try:
            resp = requests.get(
                f"{sub.get('url', '')}/api/v1/admin/groups",
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            groups = resp.json().get("data", {}).get("items", [])
            for group in groups:
                if str(group.get("name") or "") == group_name:
                    return [int(group.get("id", 1) or 1)]
        except Exception:
            pass
    return [int(sub.get("group_id", 1) or 1)]


def _validate_sub2api_retry_record(record: Dict[str, Any]) -> None:
    for key in ("email", "access_token", "refresh_token"):
        if not str(record.get(key) or "").strip():
            raise RuntimeError(f"failed_upload missing {key}; run Phase 2 retry to regenerate OAuth credentials")
    try:
        expires_at = int(record.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= 0:
        raise RuntimeError("failed_upload invalid expires_at; run Phase 2 retry to regenerate OAuth credentials")


def _retry_sub2api_oauth_upload(record: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    _validate_sub2api_retry_record(record)
    sub = config.get("sub2api", {})
    sub2api_url = sub.get("url", "")
    login_resp = requests.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub.get("email", ""), "password": sub.get("pwd", "")},
        timeout=30,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        return {"ok": False, "error": f"SUB2API login failed: {login_data}"}
    admin_token = login_data["data"]["access_token"]
    credentials = {
        "access_token": record.get("access_token", ""),
        "refresh_token": record.get("refresh_token", ""),
        "expires_at": record.get("expires_at", 0),
        "email": record.get("email", ""),
    }
    body = {
        "name": record.get("email") or record.get("phone") or "codex-account",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "group_ids": _sub2api_group_ids(record, config, admin_token),
        "priority": 1,
        "concurrency": 10,
        "auto_pause_on_expired": True,
    }
    proxy_id = int(sub.get("proxy_id", 0) or 0)
    if proxy_id:
        body["proxy_id"] = proxy_id
    resp = requests.post(
        f"{sub2api_url}/api/v1/admin/accounts",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    data = resp.json()
    ok = data.get("code") == 0
    result = {"ok": ok, "uploaded": ok, "upload_verified": ok, "upload_target": "sub2api", "_raw": data}
    if ok:
        account_data = data.get("data", {})
        result["sub2api_account_id"] = account_data.get("id") or account_data.get("account_id") or account_data.get("created")
        credentials_status = account_data.get("credentials_status") or {}
        if credentials_status and not credentials_status.get("has_refresh_token", False):
            result["ok"] = False
            result["uploaded"] = False
            result["upload_verified"] = False
            result["needs_retry"] = True
            result["error"] = "SUB2API account missing refresh_token after upload"
    else:
        result["error"] = str(data)
    return result


def retry_failed_upload(path, config: Dict[str, Any]) -> Dict[str, Any]:
    failed_path = Path(path)
    record = load_failed_upload(failed_path)
    target = str(record.get("upload_target") or config.get("upload_target") or "sub2api").lower()

    if target == "cpa":
        if upload_cpa_auth_file is None:
            raise RuntimeError("CPA auth-files uploader is not available")
        cpa = config.get("cpa", {})
        filename = f"codex-{_safe_label(record.get('email') or record.get('phone') or 'account')}.json"
        result = upload_cpa_auth_file(
            cpa.get("api_url", ""),
            cpa.get("management_key", ""),
            _auth_payload_from_record(record),
            filename,
        )
    else:
        result = _retry_sub2api_oauth_upload(record, config)

    if result.get("ok"):
        done_dir = failed_path.parent / "done"
        done_dir.mkdir(exist_ok=True)
        shutil.move(str(failed_path), str(done_dir / failed_path.name))
    return result
