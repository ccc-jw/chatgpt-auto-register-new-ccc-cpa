"""
Phase 2 wrapper: OAuth login + bind email + upload to SUB2API.
"""

import base64
import json
import sys
from typing import Dict
from urllib.parse import parse_qs, quote, urlparse


_DOCS = r"D:\qingfeng\Documents\逆向包"
if _DOCS not in sys.path:
    sys.path.insert(0, _DOCS)


def codex_login(
    session_token: str,
    phone: str,
    password: str,
    bind_email: str,
    oauth_url,
    icloud_cookies: dict = None,
    proxy: str = "",
    sub2api_url: str = "",
    sub2api_email: str = "",
    sub2api_pwd: str = "",
    sub2api_proxy_id: int = 0,
    sub2api_session_id: str = "",
    sub2api_state: str = "",
    upload_target: str = "sub2api",
    cpa_api_url: str = "",
    cpa_management_key: str = "",
    cpa_upload_mode: str = "auto",
    cpa_oauth_state: str = "",
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
    verbose: bool = True,
) -> Dict:
    """
    Run the Phase 2 flow and, when session/state are available, finish the
    selected upload target step.
    """
    from openai_bind_email import run_second_half

    # session_token is forwarded to retry/failure persistence paths.

    if isinstance(oauth_url, dict):
        oauth_info = oauth_url
        oauth_url = oauth_info.get("auth_url") or oauth_info.get("oauth_url") or ""
        sub2api_session_id = sub2api_session_id or oauth_info.get("session_id", "")
        sub2api_state = sub2api_state or oauth_info.get("state", "")
        cpa_oauth_state = cpa_oauth_state or oauth_info.get("state", "")

    if not sub2api_state and oauth_url:
        sub2api_state = parse_qs(urlparse(oauth_url).query).get("state", [""])[0]

    return run_second_half(
        oauth_url=oauth_url,
        phone=phone,
        password=password,
        icloud_email=bind_email,
        icloud_cookies=icloud_cookies or {},
        sub2api_url=sub2api_url,
        sub2api_email=sub2api_email,
        sub2api_password=sub2api_pwd,
        sub2api_proxy_id=sub2api_proxy_id,
        sub2api_session_id=sub2api_session_id,
        sub2api_state=sub2api_state,
        upload_target=upload_target,
        cpa_api_url=cpa_api_url,
        cpa_management_key=cpa_management_key,
        cpa_upload_mode=cpa_upload_mode,
        cpa_oauth_state=cpa_oauth_state,
        session_token=session_token,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=account_id,
        proxy=proxy,
        verbose=verbose,
    )


def get_oauth_url(
    sub2api_url: str,
    sub2api_email: str,
    sub2api_pwd: str,
    sub2api_proxy_id: int = 0,
) -> Dict[str, str]:
    """Generate OAuth URL metadata from SUB2API."""
    import requests as req

    login_resp = req.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub2api_email, "password": sub2api_pwd},
        timeout=30,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API login failed: {login_data}")

    token = login_data["data"]["access_token"]
    body = {"redirect_uri": "http://localhost:1455/auth/callback"}
    if sub2api_proxy_id:
        body["proxy_id"] = sub2api_proxy_id

    oauth_resp = req.post(
        f"{sub2api_url}/api/v1/admin/openai/generate-auth-url",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    oauth_data = oauth_resp.json()
    if oauth_data.get("code") != 0:
        raise RuntimeError(f"Generate OAuth URL failed: {oauth_data}")

    payload = oauth_data["data"]
    auth_url = payload["auth_url"]
    state = payload.get("state", "") or parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    return {
        "auth_url": auth_url,
        "oauth_url": auth_url,
        "session_id": payload.get("session_id", ""),
        "state": state,
    }


def _join_cpa_management_url(cpa_api_url: str, path: str) -> str:
    return f"{str(cpa_api_url or '').rstrip('/')}/v0/management/{path.lstrip('/')}"


def _cpa_headers(management_key: str) -> Dict[str, str]:
    return {"X-Management-Key": management_key}


def _cpa_ok(payload: dict, status_code: int) -> bool:
    status = str(payload.get("status") or payload.get("code") or "").lower()
    return 200 <= status_code < 300 and status in {"ok", "0", "success", ""}


def _decode_jwt_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _inject_chatgpt_account_id_into_id_token(id_token: str, account_id: str) -> str:
    account_id = str(account_id or "").strip()
    if not account_id:
        return id_token
    parts = str(id_token or "").split(".")
    if len(parts) != 3:
        return id_token
    claims = _decode_jwt_payload(id_token)
    if not claims:
        return id_token
    auth_info = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_info, dict):
        auth_info = {}
    auth_info["chatgpt_account_id"] = account_id
    claims["https://api.openai.com/auth"] = auth_info
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return ".".join([parts[0], encoded, parts[2]])


def _cpa_auth_file_has_account_id(entry: dict, account_id: str = "") -> bool:
    target = str(account_id or "").strip()
    candidates = [
        entry.get("chatgpt_account_id"),
        entry.get("chatgptAccountId"),
        entry.get("account_id"),
        entry.get("accountId"),
    ]
    for container_key in ("metadata", "attributes", "id_token"):
        container = entry.get(container_key)
        if isinstance(container, dict):
            candidates.extend([
                container.get("chatgpt_account_id"),
                container.get("chatgptAccountId"),
                container.get("account_id"),
                container.get("accountId"),
            ])
    for value in candidates:
        text = str(value or "").strip()
        if text and (not target or text == target):
            return True
    return False


def _find_cpa_codex_filename(files: list, email: str, fallback: str = "") -> str:
    target = str(email or "").strip().lower()
    matches = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("filename") or entry.get("id") or "").strip()
        provider = str(entry.get("type") or entry.get("provider") or "").lower()
        entry_email = str(entry.get("email") or entry.get("account") or entry.get("label") or "").strip().lower()
        if provider != "codex" and "codex" not in name.lower():
            continue
        if target and target not in {entry_email, str(entry.get("account") or "").strip().lower()} and target not in name.lower():
            continue
        matches.append(entry)
    if not matches:
        return fallback
    matches.sort(key=lambda e: str(e.get("updated_at") or e.get("modtime") or e.get("created_at") or ""), reverse=True)
    return str(matches[0].get("name") or matches[0].get("filename") or matches[0].get("id") or fallback)


def verify_cpa_auth_file_account_id(cpa_api_url: str, management_key: str, email: str, account_id: str = "") -> Dict[str, object]:
    listed = list_cpa_auth_files(cpa_api_url, management_key)
    if not listed.get("ok"):
        return {"ok": False, "error": "CPA auth-files list failed", "data": listed}
    files = (listed.get("data") or {}).get("files") or []
    filename = _find_cpa_codex_filename(files, email)
    for entry in files:
        if str(entry.get("name") or entry.get("id") or "") == filename and _cpa_auth_file_has_account_id(entry, account_id):
            return {"ok": True, "filename": filename, "entry": entry}
    return {"ok": False, "filename": filename, "error": "CPA auth-file account_id not visible in list"}


def ensure_cpa_auth_file_account_id(cpa_api_url: str, management_key: str, filename: str, account_id: str, email: str = "") -> Dict[str, object]:
    """Patch a CPA Codex auth-file so quota UI can read ChatGPT account_id."""
    import requests as _req

    account_id = str(account_id or "").strip()
    filename = str(filename or "").strip()
    if not cpa_api_url or not management_key or not account_id:
        return {"ok": False, "error": "missing cpa_api_url, management_key, or account_id"}
    if not filename:
        listed = list_cpa_auth_files(cpa_api_url, management_key)
        if not listed.get("ok"):
            return {"ok": False, "error": "CPA auth-files list failed", "data": listed}
        filename = _find_cpa_codex_filename((listed.get("data") or {}).get("files") or [], email)
    if not filename:
        return {"ok": False, "error": "CPA Codex auth-file not found"}

    download = _req.get(
        _join_cpa_management_url(cpa_api_url, "auth-files/download") + f"?name={quote(filename)}",
        headers=_cpa_headers(management_key),
        timeout=30,
    )
    try:
        payload = download.json()
    except Exception:
        return {"ok": False, "status_code": download.status_code, "error": download.text[:300]}
    if download.status_code < 200 or download.status_code >= 300 or not isinstance(payload, dict):
        return {"ok": False, "status_code": download.status_code, "data": payload}

    payload["account_id"] = account_id
    payload["chatgpt_account_id"] = account_id
    if payload.get("id_token"):
        payload["id_token"] = _inject_chatgpt_account_id_into_id_token(str(payload.get("id_token") or ""), account_id)

    upload = _req.post(
        _join_cpa_management_url(cpa_api_url, "auth-files") + f"?name={quote(filename)}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**_cpa_headers(management_key), "Content-Type": "application/json"},
        timeout=60,
    )
    try:
        data = upload.json()
    except Exception:
        data = {"error": upload.text[:300]}
    ok = _cpa_ok(data, upload.status_code)
    if not ok:
        return {"ok": False, "status_code": upload.status_code, "data": data, "filename": filename}
    verified = verify_cpa_auth_file_account_id(cpa_api_url, management_key, email or payload.get("email", ""), account_id)
    return {
        "ok": bool(verified.get("ok")),
        "status_code": upload.status_code,
        "data": data,
        "filename": filename,
        "verified": bool(verified.get("ok")),
        "verify_result": verified,
    }


def get_cpa_oauth_url(cpa_api_url: str, management_key: str) -> Dict[str, str]:
    """Generate OAuth URL metadata from CLIProxyAPI management API."""
    import requests as _req

    if not cpa_api_url:
        raise RuntimeError("CPA api_url is required")
    if not management_key:
        raise RuntimeError("CPA management key is required")

    resp = _req.get(
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
    import requests as _req

    resp = _req.post(
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
    import requests as _req

    resp = _req.get(
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
    import requests as _req

    resp = _req.post(
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


def _find_sub2api_account_id(req_lib, sub2api_url: str, admin_token: str, email: str, timeout: int = 30) -> str:
    target = (email or "").strip().lower()
    if not target:
        return ""
    headers = {"Authorization": f"Bearer {admin_token}"}
    candidates = [
        {"keyword": target},
        {"search": target},
        {"email": target},
        {"page": 1, "page_size": 50},
        {"page": 1, "per_page": 50},
    ]

    def iter_items(payload):
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "list", "records", "accounts", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def item_matches(item: dict) -> bool:
        fields = [item.get("email"), item.get("name"), item.get("username"), item.get("account")]
        credentials = item.get("credentials")
        if isinstance(credentials, dict):
            fields.append(credentials.get("email"))
        return any(str(v or "").strip().lower() == target for v in fields)

    for params in candidates:
        try:
            resp = req_lib.get(f"{sub2api_url}/api/v1/admin/accounts", params=params, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                continue
            for item in iter_items(resp.json()):
                if isinstance(item, dict) and item_matches(item):
                    account_id = item.get("id") or item.get("account_id") or item.get("uid")
                    if account_id:
                        return str(account_id)
        except Exception:
            continue
    return ""


def upload_session(
    session_token: str,
    icloud_email: str,
    sub2api_url: str,
    sub2api_email: str,
    sub2api_pwd: str,
    sub2api_proxy_id: int = 0,
    group_ids: list = None,
    access_token: str = "",
) -> dict:
    """Upload session_token + access_token directly to SUB2API."""
    import requests as req

    if group_ids is None:
        group_ids = [1]

    login_resp = req.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub2api_email, "password": sub2api_pwd},
        timeout=30,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API login failed: {login_data}")

    admin_token = login_data["data"]["access_token"]
    body = {
        "content": json.dumps(
            {
                "session_token": session_token,
                "access_token": access_token,
                "email": icloud_email,
            }
        ),
        "group_ids": group_ids,
        "priority": 1,
        "auto_pause_on_expired": True,
        "update_existing": True,
    }
    if sub2api_proxy_id:
        body["proxy_id"] = sub2api_proxy_id

    upload_resp = req.post(
        f"{sub2api_url}/api/v1/admin/accounts/import/codex-session",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    upload_data = upload_resp.json()
    result = {"ok": upload_data.get("code") == 0, "uploaded": upload_data.get("code") == 0, "upload_verified": False, "_raw": upload_data}
    if result["ok"]:
        items = upload_data.get("data", {}).get("items", [])
        if items:
            result["account_id"] = items[0].get("account_id") or items[0].get("id")
            result["action"] = items[0].get("action", "unknown")
        else:
            result["account_id"] = upload_data.get("data", {}).get("created") or upload_data.get("data", {}).get("updated")
        if not result.get("account_id"):
            result["account_id"] = _find_sub2api_account_id(req, sub2api_url, admin_token, icloud_email)
        result["warnings"] = [str(w) for w in (upload_data.get("data", {}).get("warnings", []) or [])]
        result["upload_verified"] = bool(result.get("account_id"))
        if not result["upload_verified"]:
            result["ok"] = False
            result["needs_retry"] = True
            result["error"] = "SUB2API import succeeded but account id was not found"
    return result
