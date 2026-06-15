#!/usr/bin/env python3
"""
OpenAI 后半段 — 纯协议版（基于真实抓包端点）

真实流程:
  [1] POST /oauth/authorize          Cloudflare 挑战 → 302
  [2] POST sentinel/req              flow=authorize_continue → oai-sc
  [3] POST /api/accounts/authorize/continue  {"username":{"kind":"phone_number","value":"+56..."}}
  [4] POST sentinel/req              flow=password_verify
  [5] POST /api/accounts/password/verify     {"password":"xxx"}
  [6] POST /api/accounts/add-email/send      {"email":"...@icloud.com"}
  [7] iCloud 收绑定验证码
  [8] POST /api/accounts/email-otp/validate  {"code":"796880"}
  [9] POST /api/accounts/workspace/select    {"workspace_id":"xxx"}
  [10] GET  /api/oauth/oauth2/auth?login_verifier=xxx  → 302 → code
  [11] code → token 交换 + SUB2API 上传
"""

import base64
import re
import json
import time
import uuid
import urllib3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, Callable
from urllib.parse import urlparse, parse_qs, urljoin

from curl_cffi import requests as curl_requests

# 错误分类常量
PERMANENT_ERRORS = {
    "invalid_username_or_password",
    "account_locked",
    "account_disabled",
    "invalid_grant",
    "user_not_found",
    "password_incorrect",
}

TEMPORARY_ERRORS = {
    "connection_reset",
    "connection_aborted",
    "max_retries_exceeded",
    "timeout",
    "network_error",
    "proxy_error",
    "remote_disconnected",
    "eof",
}


def classify_error(error_msg: str) -> str:
    """分类错误为 'permanent'（永久失败）、'temporary'（临时失败）或 'unknown'"""
    if not error_msg:
        return "unknown"
    error_lower = str(error_msg).lower()
    for err in PERMANENT_ERRORS:
        if err in error_lower:
            return "permanent"
    for err in TEMPORARY_ERRORS:
        if err in error_lower:
            return "temporary"
    return "unknown"

urllib3.disable_warnings()

AUTH = "https://auth.openai.com"
SENTINEL = "https://sentinel.openai.com/backend-api/sentinel/req"
CHATGPT = "https://chatgpt.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

JSON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": AUTH,
    "user-agent": UA,
    "sec-ch-ua": '"Google Chrome";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
}


def _log(msg: str):
    print(f"  [AUTH] {msg}")


# ============================================================
# Sentinel PoW (简化版，内联用)
# ============================================================

def _find_sub2api_account_id(
    req_lib,
    sub2api_url: str,
    admin_token: str,
    email: str,
    timeout: int = 30,
) -> str:
    """Best-effort lookup for an account that may have been created despite a 500 response."""
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
        fields = [
            item.get("email"),
            item.get("name"),
            item.get("username"),
            item.get("account"),
        ]
        credentials = item.get("credentials")
        if isinstance(credentials, dict):
            fields.append(credentials.get("email"))
        return any(str(v or "").strip().lower() == target for v in fields)

    for params in candidates:
        try:
            r = req_lib.get(
                f"{sub2api_url}/api/v1/admin/accounts",
                params=params,
                headers=headers,
                timeout=timeout,
            )
            if r.status_code != 200:
                continue
            payload = r.json()
            for item in iter_items(payload):
                if isinstance(item, dict) and item_matches(item):
                    account_id = item.get("id") or item.get("account_id") or item.get("uid")
                    if account_id:
                        return str(account_id)
        except Exception:
            continue
    return ""


def _stable_codex_filename(email: str, phone: str = "") -> str:
    label = re.sub(r"[^A-Za-z0-9_.@-]+", "-", (email or phone or "account")).strip(".-_") or "account"
    return f"codex-{label[:80]}.json"


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _extract_chatgpt_account_id_from_id_token(id_token: str) -> str:
    claims = _decode_jwt_payload(id_token)
    auth_info = claims.get("https://api.openai.com/auth")
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return ""


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


def _build_cpa_auth_payload(
    email: str,
    access_token: str = "",
    refresh_token: str = "",
    id_token: str = "",
    account_id: str = "",
    expires_at: Any = 0,
) -> Dict[str, Any]:
    payload = {"type": "codex", "email": email or ""}
    account_id = (account_id or _extract_chatgpt_account_id_from_id_token(id_token)).strip()
    if id_token and account_id and not _extract_chatgpt_account_id_from_id_token(id_token):
        id_token = _inject_chatgpt_account_id_into_id_token(id_token, account_id)
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
    import time as _time

    resp = req_lib.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub2api_email, "password": sub2api_password},
        timeout=30,
    )
    d = resp.json()
    if d.get("code") != 0:
        log(f"[11] SUB2API 登录失败: {d}")
        return {"ok": False, "error": f"SUB2API login failed: {d}"}
    admin_token = d["data"]["access_token"]

    exchange_data = None
    for attempt in range(10):
        log(f"[11] exchange-code 尝试 {attempt+1}/10 ...")
        try:
            r = req_lib.post(
                f"{sub2api_url}/api/v1/admin/openai/exchange-code",
                json={"session_id": sub2api_session_id, "code": code, "state": sub2api_state},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=300,
            )
            log(f"[11] response: {r.status_code}")
            if r.status_code == 200:
                try:
                    exchange_data = r.json()
                except Exception:
                    exchange_data = r.json()
                break
            elif r.status_code == 502:
                # 502 错误使用指数退避重试: 1s, 2s, 4s, 8s, ...
                wait_time = 2 ** attempt
                log(f"[11] 502 错误，{wait_time}s 后重试 ...")
                _time.sleep(wait_time)
                continue
            else:
                log(f"[11] exchange-code 失败: {r.status_code} {r.text[:200]}")
                return {"ok": False, "error": f"exchange-code: {r.status_code}"}
        except Exception as e:
            if attempt < 9:
                wait_time = 2 ** attempt
                log(f"[11] 网络错误: {e}，{wait_time}s 后重试 ...")
                _time.sleep(wait_time)
                continue
            exchange_data = {"error": str(e)}
            break

    if not exchange_data:
        return {"ok": False, "error": "exchange-code 502 after 3 retries"}

    creds = exchange_data.get("data", exchange_data)
    email_from_creds = creds.get("email", "") or icloud_email

    body = {
        "name": email_from_creds,
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": creds.get("access_token", ""),
            "refresh_token": creds.get("refresh_token", ""),
            "expires_at": creds.get("expires_at", 0),
            "email": email_from_creds,
        },
        "group_ids": [sub2api_group_id],
        "priority": 1,
        "concurrency": 10,
        "auto_pause_on_expired": True,
    }
    r = req_lib.post(
        f"{sub2api_url}/api/v1/admin/accounts",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    d = r.json()
    account_id = str(d.get("data", {}).get("id", ""))
    log(f"[11] 账号创建: code={d.get('code')} id={account_id or '?'}")
    if d.get("code") != 0 or not account_id:
        log("[11] 创建接口返回异常，开始反查 SUB2API 账号列表 ...")
        account_id = _find_sub2api_account_id(
            req_lib=req_lib,
            sub2api_url=sub2api_url,
            admin_token=admin_token,
            email=email_from_creds,
        )
        if account_id:
            log(f"[11] 反查到已创建账号: id={account_id}")
            return {"ok": True, "code": code, "sub2api_account_id": account_id, "uploaded": True, "upload_verified": True, "upload_method": "sub2api_exchange_code"}
        return {"ok": False, "uploaded": False, "upload_verified": False, "needs_retry": True, "error": f"SUB2API account create failed: code={d.get('code')} message={d.get('message', '?')}", "credentials": body["credentials"], "group_ids": [sub2api_group_id]}
    return {"ok": True, "code": code, "sub2api_account_id": account_id, "uploaded": True, "upload_verified": True, "upload_method": "sub2api_exchange_code", "credentials": body["credentials"], "group_ids": [sub2api_group_id]}


class _Sentinel:
    """内联 Sentinel，避免额外依赖"""

    MAX_ATTEMPTS = 500000

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.sid = str(uuid.uuid4())
        self.user_agent = UA

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _config(self) -> list:
        import random
        perf = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime()),
            4294705152, random.random(), self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, "en-US", random.random(),
            random.choice(["plugins-undefined", "mimeTypes-undefined"]),
            random.choice(["location", "documentURI"]),
            random.choice(["Object", "parseFloat"]),
            perf, self.sid, "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf,
        ]

    def _b64(self, data) -> str:
        import base64
        return base64.b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode()

    def _requirements(self) -> str:
        d = self._config()
        d[3] = 1
        d[9] = 5
        return "gAAAAAC" + self._b64(d)

    def _pow(self, seed: str, difficulty: str) -> str:
        import random
        diff = str(difficulty or "0")
        t0 = time.time()
        for i in range(self.MAX_ATTEMPTS):
            d = self._config()
            d[3] = i
            d[9] = round((time.time() - t0) * 1000)
            p = self._b64(d)
            if self._fnv1a_32(seed + p)[:len(diff)] <= diff:
                return "gAAAAAB" + p + "~S"
        return "gAAAAAB" + "wQ8Lk5F" * 10 + self._b64(str(None))

    def get(self, session, flow: str) -> str:
        r = session.post(
            SENTINEL,
            data=json.dumps({"p": self._requirements(), "id": self.device_id, "flow": flow}),
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://sentinel.openai.com",
                "User-Agent": self.user_agent,
            },
            verify=False, timeout=30,
        )
        if not r.ok:
            return ""
        data = r.json()
        token = str(data.get("token") or "")
        if not token:
            return ""
        pw = data.get("proofofwork") or {}
        if pw.get("required") and pw.get("seed"):
            p = self._pow(str(pw["seed"]), str(pw.get("difficulty", "0")))
        else:
            p = self._requirements()
        return json.dumps({"p": p, "t": "", "c": token, "id": self.device_id, "flow": flow})


# ============================================================
# HTML form 解析（consent 页面回退分支）
# ============================================================

def _extract_form(page_url: str, html: str):
    """从 HTML 中提取第一个 <form> 的 action 和 input 字段"""
    from urllib.parse import urljoin
    import re as _re
    form_match = _re.search(r"<form[^>]*action=[\"']([^\"']+)[\"'][^>]*>", html, _re.IGNORECASE)
    if not form_match:
        return None, {}
    action = urljoin(page_url, form_match.group(1))
    fields = {}
    for m in _re.finditer(r"<input[^>]*name=[\"']([^\"']+)[\"'][^>]*value=[\"']([^\"']*)[\"'][^>]*>", html, _re.IGNORECASE):
        fields[m.group(1)] = m.group(2)
    return action, fields


_OUTLOOK_DOMAIN_PREFIXES = ("outlook.", "hotmail.", "live.", "msn.")


def _is_outlook_email(email: str) -> bool:
    domain = (email or "").strip().lower().partition("@")[2]
    return any(domain.startswith(prefix) for prefix in _OUTLOOK_DOMAIN_PREFIXES)


def _poll_bind_code(
    bind_email: str,
    icloud_cookies: Dict[str, str],
    verbose: bool,
    timeout: int,
    imap_user: str,
    imap_password: str,
    start_after: float,
    proxy: str = "",
    outlook_pool: str = "",
) -> str:
    sender_filters = ["openai", "noreply", "verification", "no-reply"]
    if _is_outlook_email(bind_email):
        from outlook_mail import get_outlook_account, poll_outlook_for_code

        account = get_outlook_account(bind_email, outlook_pool or "outlook.txt")
        code = poll_outlook_for_code(
            account,
            sender_filters=sender_filters,
            timeout=timeout,
            verbose=verbose,
            proxy=proxy,
            start_after=start_after,
        ) or ""
        if code or not proxy:
            return code
        return poll_outlook_for_code(
            account,
            sender_filters=sender_filters,
            timeout=timeout,
            verbose=verbose,
            proxy="",
            start_after=start_after,
        ) or ""

    from icloud_hme import ICloudHME

    icloud = ICloudHME(icloud_cookies or {}, verbose=verbose)
    return icloud.poll_mail_for_code(
        target_email=bind_email,
        sender_filters=sender_filters,
        timeout=timeout,
        imap_user=imap_user,
        imap_password=imap_password,
        start_after=start_after,
    ) or ""


def _prompt_bind_code(bind_email: str) -> str:
    print(f"\n  [!] 自动轮询超时, 目标邮箱: {bind_email}")
    return input("  [?] 输入6位验证码: ").strip()


# ============================================================
# 后半段引擎 (真实端点)
# ============================================================

class OAuthSecondHalf:
    """OpenAI OAuth 后半段 — 真实端点版"""

    def __init__(self, proxy: str = "", verbose: bool = True, device_id: str = ""):
        self.verbose = verbose
        self.device_id = device_id or str(uuid.uuid4())
        self._default_timeout = 30

        if proxy:
            import requests as r
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            self.session = r.Session()
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.verify = False

            # 添加重试策略：连接错误和 502/503/504 自动重试
            retry_strategy = Retry(
                total=3,
                backoff_factor=2,  # 重试间隔: 0s, 2s, 4s
                status_forcelist=[502, 503, 504],
                allowed_methods=["POST", "GET"],
                raise_on_status=False
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

            # Inject default timeout so proxy hangs don't block forever
            _orig = self.session.request
            def _req(method, url, **kw):
                kw.setdefault("timeout", self._default_timeout)
                return _orig(method, url, **kw)
            self.session.request = _req
        else:
            self.session = curl_requests.Session(impersonate="chrome", verify=False)

        self.sentinel = _Sentinel(self.device_id)
        self._sentinel_cache: Dict[str, str] = {}
        self.selected_workspace_id = ""

    def _l(self, msg): 
        if self.verbose: _log(msg)

    def _post_form(self, action: str, fields: dict) -> "requests.Response":
        """POST 提交 HTML form（用于 consent 回退分支）"""
        return self.session.post(action, data=fields, allow_redirects=False)

    def _sentinel_token(self, flow: str) -> str:
        if flow not in self._sentinel_cache:
            try:
                self._sentinel_cache[flow] = self.sentinel.get(self.session, flow)
            except Exception as e:
                self._l(f"Sentinel 跳过 ({flow}): {e}")
                self._sentinel_cache[flow] = ""
        return self._sentinel_cache[flow]

    # ---------- 解析 OAuth URL ----------

    @staticmethod
    def parse_oauth_url(oauth_url: str) -> Dict[str, str]:
        parsed = urlparse(oauth_url)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    # ---------- [1] 发起 OAuth + Cloudflare ----------

    def initiate_oauth(self, oauth_url: str):
        """
        ① GET oauth_url → 跟重定向到登录页
        """
        self._l("[1] 发起 OAuth (GET) ...")
        r = self.session.get(
            oauth_url,
            headers={
                **PAGE_HEADERS,
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
            },
            allow_redirects=True,
        )
        url = r.url
        html = r.text
        is_error = "/error" in url
        self._l(f"[1] 当前 URL: {url[:120]}")
        if is_error:
            self._l(f"[1] 重定向到了错误页!")
        return not is_error, url, html

    # ---------- [2] Sentinel authorize_continue ----------

    def sentinel_authorize(self) -> str:
        self._l("[2] Sentinel (authorize_continue) ...")
        return self._sentinel_token("authorize_continue")

    # ---------- [3] 提交手机号 ----------

    def submit_phone(self, phone: str) -> Dict:
        """
        [3] POST /api/accounts/authorize/continue
           {"username":{"kind":"phone_number","value":"+15550000000"}}
        返回: {continue_url, page:{type, payload}}
        设置: oai-client-auth-session cookie
        """
        self._l(f"[3] 提交手机号: {phone}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("authorize_continue")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/authorize/continue",
            json={"username": {"kind": "phone_number", "value": phone}},
            headers=h,
        )
        self._l(f"[3] 响应: {r.status_code}")
        return r.json() if r.ok else {"error": r.text}

    # ---------- [4] Sentinel password_verify ----------

    def sentinel_password(self) -> str:
        self._l("[4] Sentinel (password_verify) ...")
        return self._sentinel_token("password_verify")

    # ---------- [5] 验证密码 ----------

    def verify_password(self, password: str) -> Dict:
        """
        [5] POST /api/accounts/password/verify
           {"password":"xxx"}
        返回: {continue_url:"/add-email", page:{type:"add_email"}}
        """
        self._l("[5] 验证密码 ...")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/password/verify",
            json={"password": password},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[5] 响应: page={pt}")
        return data

    # ---------- [6] 发送绑定邮箱 ----------

    def send_bind_email(self, email: str) -> Dict:
        """
        [6] POST /api/accounts/add-email/send
           {"email":"alias@icloud.com"}
        返回: {continue_url:"/email-verification", page:{type:"email_otp_verification"}}
        """
        self._l(f"[6] 发送绑定邮箱: {email}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/add-email/send",
            json={"email": email},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[6] 响应: page={pt}")
        return data

    def select_phone_otp_channel(self, channel: str = "sms") -> Dict:
        """
        POST /api/accounts/phone-otp/select-channel
        {"channel": "sms" or "voice"}
        """
        self._l(f"[6] 选择手机验证码渠道: {channel}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/phone-otp/select-channel",
            json={"channel": channel},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[6] 响应: page={pt}")
        return data

    def send_phone_otp(self) -> Dict:
        """
        POST /api/accounts/phone-otp/send
        """
        self._l("[6] 发送手机验证码 ...")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/phone-otp/send",
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[6] 响应: page={pt}")
        return data

    def verify_phone_otp(self, code: str) -> Dict:
        """
        POST /api/accounts/phone-otp/validate
        {"code": "123456"}
        """
        self._l(f"[7] 验证手机验证码: {code}")
        h = dict(JSON_HEADERS)
        st = self._sentinel_token("password_verify")
        if st:
            h["OpenAI-Sentinel-Token"] = st
        r = self.session.post(
            f"{AUTH}/api/accounts/phone-otp/validate",
            json={"code": code},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[7] 响应: page={pt}")
        return data

    # ---------- [7] 验证邮箱 OTP ----------

    def verify_email_otp(self, code: str) -> Dict:
        """
        [7] POST /api/accounts/email-otp/validate
           {"code":"796880"}
        返回: {continue_url:"/sign-in-with-chatgpt/codex/consent", page:{type:"consent"}}
        email 标记为 verified
        """
        self._l(f"[7] 验证邮箱 OTP: {code}")
        h = dict(JSON_HEADERS)
        r = self.session.post(
            f"{AUTH}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=h,
        )
        data = r.json() if r.ok else {"error": r.text}
        pt = (data.get("page") or {}).get("type", "")
        self._l(f"[7] 响应: page={pt}")
        return data

    # ---------- [8] 查询 session 状态 ----------

    def get_session_dump(self) -> Dict:
        """
        GET /api/accounts/client_auth_session_dump
        返回: {client_auth_session:{session_id, username, email, workspaces, ...}}
        """
        r = self.session.get(
            f"{AUTH}/api/accounts/client_auth_session_dump",
            headers=JSON_HEADERS,
        )
        return r.json() if r.ok else {}

    # ---------- [9] 选择工作区 ----------

    def select_workspace(self, workspace_id: str) -> Dict:
        """
        [9] POST /api/accounts/workspace/select
           {"workspace_id":"74461035-..."}
        返回: {continue_url:"...login_verifier...", page:{...}}
        """
        self._l(f"[9] 选择工作区: {workspace_id}")
        self.selected_workspace_id = str(workspace_id or "")
        r = self.session.post(
            f"{AUTH}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=JSON_HEADERS,
        )
        data = r.json() if r.ok else {"error": r.text}
        return data

    # ---------- [10] 最终 OAuth → 获取 code ----------

    def follow_continue_until_code(self, continue_url: str, max_hops: int = 8) -> Optional[str]:
        """
        跟随 continue_url 链，直到捕获 redirect_uri 中的 code
        会自动处理 consent 页（获取 session_dump → 选 workspace → 再跟）
        """
        url = continue_url
        for hop in range(max_hops):
            self._l(f"[10] hop {hop+1}/{max_hops}: {url[:100]}...")
            r = self.session.get(
                url if url.startswith("http") else urljoin(AUTH, url),
                headers={**PAGE_HEADERS, "referer": AUTH, "sec-fetch-site": "same-origin"},
                allow_redirects=False,
            )
            location = r.headers.get("Location", "")
            ct = r.headers.get("content-type", "")
            self._l(f"[10]   -> {r.status_code} ct={ct[:30]} loc={location if location else 'none'}")

            # 检查 Location / URL 中的 code
            if location:
                parsed = urlparse(location)
                code = parse_qs(parsed.query).get("code", [None])[0]
                if code:
                    self._l(f"[10] code: {code[:30]}...")
                    return code
                url = location if location.startswith("http") else urljoin(AUTH, location)
                continue

            # 当前 URL 中的 code
            parsed = urlparse(r.url)
            code = parse_qs(parsed.query).get("code", [None])[0]
            if code:
                self._l(f"[10] code (url): {code[:30]}...")
                return code

            # HTML consent 页 → 需要选 workspace
            if "text/html" in ct and ("consent" in url.lower() or "consent" in r.text.lower()[:500]):
                self._l("[10] consent 页 → 选 workspace ...")
                dump = self.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    self._l(f"[10] workspace: {ws_id}")
                    ws_r = self.select_workspace(ws_id)
                    next_url = ws_r.get("continue_url", "")
                    if next_url:
                        url = next_url if next_url.startswith("http") else urljoin(AUTH, next_url)
                        continue
                # 回退：尝试从 HTML 提取 form
                action, fields = _extract_form(r.url, r.text)
                if action and fields:
                    self._l(f"[10] POST consent form: {action}")
                    fr = self._post_form(action, fields)
                    loc = fr.headers.get("Location", "")
                    if loc:
                        url = loc if loc.startswith("http") else urljoin(AUTH, loc)
                        continue

            # JSON → 提取 continue_url
            if "json" in ct:
                try:
                    data = r.json()
                    next_url = data.get("continue_url", "")
                    if next_url:
                        url = next_url if next_url.startswith("http") else urljoin(AUTH, next_url)
                        continue
                except Exception:
                    pass

            break

        return None

    def final_oauth(self, oauth_params: Dict[str, str]) -> Optional[str]:
        """
        [10] GET /api/oauth/oauth2/auth?client_id=...&login_verifier=...&...
           → 302 → redirect_uri?code=xxx&state=yyy
        返回: authorization code
        """
        self._l("[10] 最终 OAuth → 获取 code ...")

        # 构建完整参数
        params = dict(oauth_params)
        # 从 URL 拼接
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{AUTH}/api/oauth/oauth2/auth?{qs}"

        r = self.session.get(
            url,
            headers={**PAGE_HEADERS, "referer": AUTH},
            allow_redirects=False,
        )

        # 从 Location header 提取 code
        location = r.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            code = parse_qs(parsed.query).get("code", [None])[0]
            if code:
                self._l(f"[10] code: {code[:30]}...")
                return code

        # 跟随重定向后从 URL 提取
        r2 = self.session.get(
            url,
            headers={**PAGE_HEADERS, "referer": AUTH},
            allow_redirects=True,
        )
        parsed = urlparse(r2.url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if code:
            self._l(f"[10] code: {code[:30]}...")
            return code

        self._l("[10] 未捕获到 code")
        return None


# ============================================================
# 完整后半段入口
# ============================================================

def run_second_half(
    oauth_url: str,
    phone: str,
    password: str,
    icloud_email: str,
    icloud_cookies: Dict[str, str],
    sub2api_url: str = "",
    sub2api_email: str = "",
    sub2api_password: str = "",
    sub2api_proxy_id: int = 0,
    sub2api_group_id: int = 1,
    proxy: str = "",
    verbose: bool = True,
    bind_code: str = "",
    imap_user: str = "",
    imap_password: str = "",
    sub2api_session_id: str = "",
    sub2api_state: str = "",
    outlook_pool: str = "",
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
) -> Dict:
    """
    完整后半段 (基于真实端点):

    [1] POST /oauth/authorize             发起OAuth
    [2] sentinel/req (authorize_continue) 安全检测
    [3] /api/accounts/authorize/continue  提交手机号
    [4] sentinel/req (password_verify)    刷新安全token
    [5] /api/accounts/password/verify     验证密码
    [6] /api/accounts/add-email/send      绑定iCloud邮箱
    [7] iCloud收验证码
    [8] /api/accounts/email-otp/validate  验证邮箱OTP
    [9] /api/accounts/workspace/select    选择工作区
    [10] /api/oauth/oauth2/auth            → code
    [11] code→token + SUB2API上传
    """

    def log(msg):
        if verbose: _log(msg)

    flow = OAuthSecondHalf(proxy=proxy, verbose=verbose)
    mail_label = "Outlook" if _is_outlook_email(icloud_email) else "iCloud"

    try:
        # 解析 OAuth URL 参数
        oauth_params = OAuthSecondHalf.parse_oauth_url(oauth_url)
        log(f"OAuth params: client_id={oauth_params.get('client_id','?')[:20]}...")

        # ---- [1] 发起 OAuth ----
        log("=" * 40)
        ok, current_url, html = flow.initiate_oauth(oauth_url)
        if not ok:
            log(f"[1] OAuth 发起失败, URL: {current_url[:120]}")
            return {"ok": False, "error": f"initiate_oauth failed: {current_url[:120]}"}

        # ---- [2] Sentinel ----
        flow.sentinel_authorize()

        # ---- [3] 提交手机号 ----
        log("[3] 提交手机号 ...")
        r = flow.submit_phone(phone)
        if r.get("error"):
            log(f"[3] 失败: {r.get('error')}")
            return {"ok": False, "error": f"submit_phone: {r.get('error')}"}
        log(f"[3] page: {(r.get('page') or {}).get('type', '?')}")

        # ---- [4] Sentinel ----
        flow.sentinel_password()

        # ---- [5] 验证密码 ----
        log("[5] 验证密码 ...")
        r = flow.verify_password(password)
        if r.get("error"):
            log(f"[5] 失败: {r.get('error')}")
            return {"ok": False, "error": f"verify_password: {r.get('error')}"}
        page_type = (r.get("page") or {}).get("type", "")
        log(f"[5] page: {page_type}")

        # 分支判断
        if "phone_otp_select_channel" in page_type:
            # 需要先选手机验证码渠道（SMS/语音），验证手机 OTP，然后再绑邮箱
            log("[5] phone_otp_select_channel 页, 先验证手机 OTP ...")
            # 选择 SMS 渠道
            sel = flow.select_phone_otp_channel("sms")
            if sel.get("error"):
                log(f"[5] 选择渠道失败: {sel.get('error')}")
                return {"ok": False, "error": f"select_phone_channel: {sel.get('error')}"}

            # 发送手机验证码
            send_r = flow.send_phone_otp()
            if send_r.get("error"):
                log(f"[5] 发送手机验证码失败: {send_r.get('error')}")
                return {"ok": False, "error": f"send_phone_otp: {send_r.get('error')}"}

            # 等待手机验证码
            log("[6] 等待手机验证码 ...")
            if bind_code:
                phone_code = bind_code
            else:
                print(f"\n  [!] 请输入手机验证码:")
                phone_code = input("  [?] 输入6位验证码: ").strip()
            if not phone_code:
                return {"ok": False, "error": "phone code timeout"}

            # 验证手机 OTP
            vr = flow.verify_phone_otp(phone_code)
            if vr.get("error"):
                log(f"[7] 手机验证码验证失败: {vr.get('error')}")
                return {"ok": False, "error": f"verify_phone_otp: {vr.get('error')}"}
            # 验证成功后，页面应该变为需要绑邮箱的状态
            page_type = (vr.get("page") or {}).get("type", "")
            log(f"[7] 手机验证后 page: {page_type}")
            # 继续到绑邮箱流程（fall through to else branch）

        if "about_you" in page_type:
            # 新号没填资料 → 先填资料
            log("[5] about_you 页, 先填资料 ...")
            h = dict(JSON_HEADERS)
            h["referer"] = f"{AUTH}/about-you"
            r = flow.session.post(
                f"{AUTH}/api/accounts/create_account",
                json={"name": "A", "birthdate": "2000-01-01"},
                headers=h,
                allow_redirects=False,
            )
            data = r.json() if r.ok else {"error": r.text}
            continue_url = data.get("continue_url", "")
            # 检查重定向
            location = r.headers.get("Location", "")
            if location:
                continue_url = location
            page_type = (data.get("page") or {}).get("type", "")
            log(f"[5] create_account page: {page_type}")
            # 资料填完后可能到 add_email
            if "consent" in page_type or "add_email" in page_type or "email_otp" in page_type:
                pass  # 继续走下面的分支
            else:
                code = flow.follow_continue_until_code(data.get("continue_url", "")) if data.get("continue_url") else None
                if not code:
                    code = flow.final_oauth(oauth_params)
                if not code:
                    return {"ok": False, "error": "no authorization code after about_you"}

        if "consent" in page_type:
            # 已到同意页 → 选工作区 → 拿 code
            log("[5] 已到 consent 页，跳过绑邮箱")
            dump = flow.get_session_dump()
            workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
            if workspaces:
                ws_id = workspaces[0].get("id", "")
                log(f"[9] 工作区: {ws_id}")
                ws_r = flow.select_workspace(ws_id)
                log(f"[9] page: {(ws_r.get('page') or {}).get('type', '?')}")
                continue_url = ws_r.get("continue_url", "")
            else:
                continue_url = ""
            code = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        elif "email_otp_verification" in page_type:
            # 检查账号是否已有邮箱
            log("[5] email_otp_verification, 检查账号状态 ...")
            dump = flow.get_session_dump()
            existing_email = ((dump.get("client_auth_session") or {}).get("email") or "").strip()

            if existing_email:
                # 账号已有邮箱，OTP 已发送到现有邮箱
                log(f"[5] 账号已有邮箱: {existing_email}，直接收取验证码 ...")
                poll_start_after = time.time()
                # 使用现有邮箱收取验证码，并更新 icloud_email 为实际邮箱
                target_email = existing_email
                icloud_email = existing_email  # 更新为实际邮箱，后续保存结果时使用
            else:
                # 账号无邮箱，需要绑定新邮箱
                log("[5] 账号无邮箱，发送新邮箱验证码 ...")
                poll_start_after = time.time()
                if icloud_email:
                    r_send = flow.send_bind_email(icloud_email)
                    send_err = r_send.get("error", "")
                    send_page = (r_send.get("page") or {}).get("type", "")
                    log(f"[6] send result: error={send_err} page={send_page}")
                    if not send_err and "otp_verification" in send_page:
                        log("[6] 新验证码已发送,等待IMAP...")
                target_email = icloud_email

            code_bind = bind_code
            if not code_bind:
                log(f"[7] iCloud 收验证码 (目标: {target_email}) ...")
                try:
                    code_bind = _poll_bind_code(
                        bind_email=target_email,
                        icloud_cookies=icloud_cookies,
                        verbose=verbose,
                        timeout=60,
                        imap_user=imap_user,
                        imap_password=imap_password,
                        start_after=poll_start_after,
                        proxy=proxy,
                        outlook_pool=outlook_pool,
                    )
                except RuntimeError as poll_err:
                    err_str = str(poll_err)
                    # 如果是 Outlook 账户找不到或已使用，返回明确错误
                    if "Outlook account not found" in err_str or "was already used" in err_str:
                        log(f"[7] 无法自动收取验证码：{poll_err}")
                        return {"ok": False, "error": f"outlook_unavailable: {poll_err}"}
                    else:
                        raise

                if not code_bind:
                    print(f"\n  [!] 自动轮询超时或失败, 目标邮箱: {target_email}")
                    code_bind = input("  [?] 输入6位验证码: ").strip()
            if not code_bind:
                return {"ok": False, "error": "binding code timeout"}
            log(f"[7] 验证码: {code_bind}")

            # 验证 + workspace + 取 code
            r = flow.verify_email_otp(code_bind)
            if r.get("error"):
                log(f"[8] 失败: {r.get('error')}")
                return {"ok": False, "error": f"verify_email_otp: {r.get('error')}"}
            log(f"[8] page: {(r.get('page') or {}).get('type', '?')}")
            continue_url = r.get("continue_url", "")

            if not continue_url:
                dump = flow.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    flow.selected_workspace_id = str(ws_id or "")
                    ws_r = flow.select_workspace(ws_id)
                    continue_url = ws_r.get("continue_url", "")

            code = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        else:
            # 需要绑定新邮箱 (add_email)
            log(f"[6] 绑定邮箱: {icloud_email} ...")
            poll_start_after = time.time()
            r = flow.send_bind_email(icloud_email)
            if r.get("error"):
                log(f"[6] 失败: {r.get('error')}")
                return {"ok": False, "error": f"send_bind_email: {r.get('error')}"}
            log(f"[6] page: {(r.get('page') or {}).get('type', '?')}")

            # ---- [7] iCloud 收码 ----
            log("[7] iCloud 收验证码 ...")
            if bind_code:
                code_bind = bind_code
                log(f"[7] 使用手动验证码: {code_bind}")
            else:
                code_bind = _poll_bind_code(
                    bind_email=icloud_email,
                    icloud_cookies=icloud_cookies,
                    verbose=verbose,
                    timeout=60,
                    imap_user=imap_user,
                    imap_password=imap_password,
                    start_after=poll_start_after,
                    proxy=proxy,
                    outlook_pool=outlook_pool,
                )
                if not code_bind:
                    print(f"\n  [!] 自动轮询超时, 目标邮箱: {icloud_email}")
                    code_bind = input("  [?] 输入6位验证码: ").strip()
                if not code_bind:
                    return {"ok": False, "error": "binding code timeout"}
            log(f"[7] 绑定验证码: {code_bind}")

            # ---- [8] 验证 + workspace + 取 code ----
            r = flow.verify_email_otp(code_bind)
            if r.get("error"):
                log(f"[8] 失败: {r.get('error')}")
                return {"ok": False, "error": f"verify_email_otp: {r.get('error')}"}
            log(f"[8] page: {(r.get('page') or {}).get('type', '?')}")
            continue_url = r.get("continue_url", "")

            if not continue_url:
                dump = flow.get_session_dump()
                workspaces = ((dump.get("client_auth_session") or {}).get("workspaces") or [])
                if workspaces:
                    ws_id = workspaces[0].get("id", "")
                    flow.selected_workspace_id = str(ws_id or "")
                    ws_r = flow.select_workspace(ws_id)
                    continue_url = ws_r.get("continue_url", "")

            code = flow.follow_continue_until_code(continue_url) if continue_url else None
            if not code:
                code = flow.final_oauth(oauth_params)
            if not code:
                return {"ok": False, "error": "no authorization code"}

        log(f"[10] code 获取成功: {code[:30]}...")

        # ---- [11] code → upload ----
        upload_target = str(upload_target or "sub2api").lower()

        if upload_target == "cpa":
            log("[11] CPA native OAuth import ...")
            from phase2_codex import complete_cpa_oauth_callback, ensure_cpa_auth_file_account_id, upload_cpa_auth_file
            from failed_uploads import save_failed_upload

            callback_state = cpa_oauth_state or sub2api_state or parse_qs(urlparse(oauth_url).query).get("state", [""])[0]
            account_id = (account_id or "").strip()
            if not account_id and id_token:
                account_id = _extract_chatgpt_account_id_from_id_token(id_token)
            native_result = complete_cpa_oauth_callback(cpa_api_url, cpa_management_key, code, callback_state)
            if native_result.get("ok"):
                filename = _stable_codex_filename(icloud_email, phone)
                patch_result = ensure_cpa_auth_file_account_id(cpa_api_url, cpa_management_key, filename, account_id, icloud_email)
                if not patch_result.get("ok"):
                    log(f"[11] CPA account_id patch failed: {patch_result}")
                    error = f"CPA account_id patch failed: {patch_result}"
                    failed_file = save_failed_upload(
                        {
                            "upload_target": "cpa",
                            "upload_mode": cpa_upload_mode or "auto",
                            "upload_method": "cpa_oauth_callback",
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
                            "verify_error": str(patch_result),
                            "upload_verified": False,
                            "needs_retry": True,
                            "attempts": 1,
                        }
                    )
                    return {
                        "ok": True,
                        "code": code,
                        "uploaded": False,
                        "upload_verified": False,
                        "needs_retry": True,
                        "upload_target": "cpa",
                        "upload_method": "cpa_oauth_callback",
                        "upload_error": error,
                        "failed_upload_file": failed_file,
                        "cpa_result": native_result,
                        "cpa_account_id_patch": patch_result,
                        "account_id": account_id,
                    }
                return {
                    "ok": True,
                    "code": code,
                    "uploaded": True,
                    "upload_verified": True,
                    "needs_retry": False,
                    "upload_target": "cpa",
                    "upload_method": "cpa_oauth_callback",
                    "cpa_result": native_result,
                    "cpa_account_id_patch": patch_result,
                    "account_id": account_id,
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
            account_id = auth_payload.get("account_id", account_id)
            if not account_id:
                error = "CPA auth-file fallback missing ChatGPT account_id"
                failed_file = save_failed_upload(
                    {
                        "upload_target": "cpa",
                        "upload_mode": cpa_upload_mode or "auto",
                        "upload_method": "cpa_auth_file",
                        "phone": phone,
                        "email": icloud_email,
                        "session_token": session_token,
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "expires_at": expires_at,
                        "oauth_state": callback_state,
                        "last_error": error,
                        "upload_verified": False,
                        "needs_retry": True,
                        "attempts": 1,
                    }
                )
                return {
                    "ok": True,
                    "code": code,
                    "uploaded": False,
                    "upload_verified": False,
                    "needs_retry": True,
                    "upload_target": "cpa",
                    "upload_method": "cpa_auth_file",
                    "upload_error": error,
                    "failed_upload_file": failed_file,
                }
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
                    "upload_verified": True,
                    "needs_retry": False,
                    "upload_target": "cpa",
                    "upload_method": "cpa_auth_file",
                    "cpa_result": fallback_result,
                    "account_id": account_id,
                }

            error = f"CPA upload failed: native={native_result} fallback={fallback_result}"
            failed_file = save_failed_upload(
                {
                    "upload_target": "cpa",
                    "upload_mode": cpa_upload_mode or "auto",
                    "upload_method": "cpa_auth_file",
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
                    "upload_verified": False,
                    "needs_retry": True,
                    "attempts": 1,
                }
            )
            return {
                "ok": True,
                "code": code,
                "uploaded": False,
                "upload_verified": False,
                "needs_retry": True,
                "upload_target": "cpa",
                "upload_method": "cpa_auth_file",
                "upload_error": error,
                "failed_upload_file": failed_file,
            }

        if sub2api_url and sub2api_email and sub2api_session_id:
            log("[11] SUB2API exchange-code ...")
            import requests as req_lib

            try:
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
            except Exception as upload_exc:
                sub_result = {"ok": False, "error": str(upload_exc)}
            if sub_result.get("ok"):
                sub_result["uploaded"] = True
                sub_result["upload_verified"] = bool(sub_result.get("upload_verified", True))
                sub_result["needs_retry"] = not sub_result["upload_verified"]
                sub_result["upload_target"] = "sub2api"
                return sub_result

            from failed_uploads import save_failed_upload
            error = sub_result.get("error") or str(sub_result)
            failed_file = save_failed_upload(
                {
                    "upload_target": "sub2api",
                    "upload_mode": "auto",
                    "upload_method": "sub2api_exchange_code",
                    "phone": phone,
                    "email": icloud_email,
                    "session_token": session_token,
                    "access_token": sub_result.get("credentials", {}).get("access_token", access_token),
                    "refresh_token": sub_result.get("credentials", {}).get("refresh_token", refresh_token),
                    "id_token": id_token,
                    "account_id": account_id,
                    "expires_at": sub_result.get("credentials", {}).get("expires_at", expires_at),
                    "oauth_state": sub2api_state,
                    "group_ids": sub_result.get("group_ids", [sub2api_group_id]),
                    "last_error": error,
                    "upload_verified": False,
                    "needs_retry": True,
                    "attempts": 1,
                }
            )
            return {
                "ok": True,
                "code": code,
                "uploaded": False,
                "upload_verified": False,
                "needs_retry": True,
                "upload_target": "sub2api",
                "upload_method": "sub2api_exchange_code",
                "upload_error": error,
                "failed_upload_file": failed_file,
            }

        log("[11] 无上传配置, 仅返回 code")
        return {"ok": True, "code": code, "uploaded": False}

    except Exception as e:
        log(f"异常: {e}")
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print("OpenAI 后半段 — 真实端点版")
    print()
    print("流程: OAuth → sentinel → 手机号 → 密码 → 绑邮箱 → OTP验证 → workspace → code")
    print()
    print("使用: from openai_bind_email import run_second_half")
