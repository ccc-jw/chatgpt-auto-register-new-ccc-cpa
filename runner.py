"""Registration engine - thread-safe, multi-user, with SSE streaming"""

import json
import threading
import time
import queue
from typing import Optional
from pathlib import Path

import auto_register as ar
from phone_sms_adapter import UnifiedSMS, parse_countries

import db

# ── Global locks ──
icloud_lock = threading.Lock()
mailmanage_lock = threading.Lock()

# ── Active runners per user ──
active_runners: dict = {}  # user_id → {"thread": Thread, "stop": threading.Event}


def get_email_for_user(user_id: int, sse_q: queue.Queue) -> str:
    """Get email via iCloud (paid) or MailManage (free). Returns email or raises."""
    icloud = db.check_icloud_access(user_id)
    if icloud and icloud.get("remaining_uses", 0) > 0:
        sse_q.put({"msg": "Using iCloud alias (paid)...", "tag": "info", "time": _ts()})
        with icloud_lock:
            try:
                from icloud_hme import ICloudHME
                cookies_raw = db.get_admin_asset("icloud_cookies")
                if not cookies_raw:
                    raise RuntimeError("Admin iCloud cookies not configured")
                c = json.loads(cookies_raw)
                ic = ICloudHME(c, verbose=False)
                alias = ic.create_alias()
                db.consume_icloud_use(icloud["id"])
                return alias
            except Exception as e:
                db.consume_icloud_use(icloud["id"])
                raise RuntimeError(f"iCloud failed: {e}")
    else:
        sse_q.put({"msg": "Using MailManage email (free)...", "tag": "info", "time": _ts()})
        with mailmanage_lock:
            from mailmanage_client import MailManageClient
            mm_key = db.get_admin_asset("mailmanage_key") or ""
            if not mm_key:
                raise RuntimeError("MailManage key not configured")
            mm = MailManageClient(api_key=mm_key, verbose=False)
            email = mm.get_available_email(category="free")
            if not email:
                raise RuntimeError("No MailManage email available")
            return email


def start(user_id: int, count: int) -> str:
    """Start registration for a user. Returns 'ok' or error string."""
    if user_id in active_runners:
        return "Already running"

    sse_q = get_sse_queue(user_id)
    stop_ev = threading.Event()

    thr = threading.Thread(target=_run, args=(user_id, count, sse_q, stop_ev), daemon=True)
    active_runners[user_id] = {"thread": thr, "stop": stop_ev}
    thr.start()
    return "ok"


def stop(user_id: int):
    if user_id in active_runners:
        active_runners[user_id]["stop"].set()


def is_running(user_id: int) -> bool:
    r = active_runners.get(user_id)
    return r is not None and r["thread"].is_alive()


# ── SSE queues ──
_sse_queues: dict = {}

def get_sse_queue(user_id: int) -> queue.Queue:
    if user_id not in _sse_queues:
        _sse_queues[user_id] = queue.Queue()
    return _sse_queues[user_id]


# ── Internal runner ──
def _ts():
    return time.strftime("%H:%M:%S")


def _run(user_id: int, target_count: int, sse_q: queue.Queue, stop_ev: threading.Event):
    config_data = db.get_user_config(user_id)
    proxy = config_data.get("proxy", "") or "socks5h://127.0.0.1:10808"
    country = config_data.get("country", "") or "151"
    max_price = config_data.get("max_price", "") or ""
    sms_timeout = config_data.get("sms_timeout", 30) or 30
    sms_provider = config_data.get("sms_provider", "smsbower") or "smsbower"
    sms_api_key = config_data.get("sms_api_key", "") or config_data.get("smsbower_key", "") or ""
    sms_countries = parse_countries(config_data.get("sms_countries", country))

    sse_q.put({"msg": f"开始注册任务 user_id={user_id} count={target_count} provider={sms_provider} countries={sms_countries}", "tag": "info", "time": _ts()})

    if not sms_api_key:
        sse_q.put({"msg": "Please configure SMS API key first", "tag": "error", "time": _ts()})
        return

    sms = UnifiedSMS(provider=sms_provider, api_key=sms_api_key)
    reg_config = {
        "sms": {
            "provider": sms_provider,
            "api_key": sms_api_key,
            "countries": sms_countries,
            "service": "dr",
            "operator": config_data.get("sms_operator", "any") or "any",
            "max_price": max_price,
        },
        "register": {"password": "TempPass123!", "name": "A", "birthdate": "2000-01-01"},
        "proxy": proxy,
        "code_timeout": sms_timeout,
    }

    try:
        bal = sms.balance()
        sse_q.put({"msg": f"短信余额: {bal}", "tag": "info", "time": _ts()})
    except Exception as e:
        sse_q.put({"msg": f"余额检查失败: {e}", "tag": "error", "time": _ts()})

    ok_count = 0
    attempt = 0
    max_attempts = target_count * 15

    while ok_count < target_count and attempt < max_attempts and not stop_ev.is_set():
        attempt += 1
        sse_q.put({"msg": f"[{attempt}] {ok_count}/{target_count}", "tag": "info", "time": _ts()})

        # Check quota
        user = db.get_user(user_id=user_id)
        if user.get("quota", 0) <= 0:
            sse_q.put({"msg": "Out of quota", "tag": "error", "time": _ts()})
            break

        # Get email
        try:
            email = get_email_for_user(user_id, sse_q)
            sse_q.put({"msg": f"Email: {email}", "tag": "success", "time": _ts()})
        except Exception as e:
            sse_q.put({"msg": f"Email failed: {e}", "tag": "error", "time": _ts()})
            break

        # Run registration
        try:
            # Redirect print output to SSE
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = ar.register_one(
                    sms,
                    reg_config,
                    verbose=True,
                    step_retries=2,
                    max_price=max_price,
                    stop_requested=stop_ev.is_set,
                )
            for line in buf.getvalue().split("\n"):
                if line.strip():
                    sse_q.put({"msg": line.strip(), "tag": "info", "time": _ts()})

        except ar.StopRequested:
            sse_q.put({"msg": "Stopped while waiting for phone number", "tag": "warn", "time": _ts()})
            break
        except Exception as e:
            sse_q.put({"msg": f"Error: {e}", "tag": "error", "time": _ts()})
            continue

        try:
            phone = result.get("phone", "?")
            final_ok = result.get("final_ok", False)
            phone_ok = result.get("phone_ok", False)
            email_bound = result.get("email_bound", False)
            uploaded = result.get("uploaded", False)
            status = result.get("status", "register_failed")
            failure_stage = result.get("failure_stage", "")
            retryable = result.get("retryable", False)
            sms_prov = result.get("sms_provider", "")
            sms_country = result.get("country", "")

            # SSE structured stage event
            sse_q.put({
                "stage": "final_ok" if final_ok else ("phone_ok" if phone_ok else "register_failed"),
                "phone_ok": phone_ok,
                "email_selected": result.get("email_selected", False),
                "email_bound": email_bound,
                "uploaded": uploaded,
                "final_ok": final_ok,
                "status": status,
                "failure_stage": failure_stage,
                "retryable": retryable,
                "sms_provider": sms_prov,
                "country": sms_country,
                "phone": phone,
                "email": email,
                "tag": "success" if final_ok else ("warn" if phone_ok else "error"),
                "time": _ts(),
            })

            # Human-readable message
            if final_ok:
                sse_q.put({"msg": f"OK: {phone} -> {email}", "tag": "success", "time": _ts()})
            elif phone_ok:
                sse_q.put({"msg": f"PHONE_OK: {phone} ({status})", "tag": "warn", "time": _ts()})
                if retryable:
                    sse_q.put({"msg": f"可补跑 Phase2 (phone_ok=true, final_ok=false)", "tag": "info", "time": _ts()})
            else:
                sse_q.put({"msg": f"FAIL: {phone} - {result.get('error','')} ({failure_stage})", "tag": "error", "time": _ts()})

            # Quota deduction: only when phone_ok succeeds
            if phone_ok:
                db.consume_quota(user_id)

            db.log_reg(user_id, phone, status, email, result.get("error", ""))

            if final_ok:
                ok_count += 1
        except Exception as e:
            sse_q.put({"msg": f"Error: {e}", "tag": "error", "time": _ts()})

    sse_q.put({"msg": f"Done: {ok_count}/{target_count}", "tag": "success", "time": _ts()})

    # Cleanup
    if user_id in active_runners:
        del active_runners[user_id]
