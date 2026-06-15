#!/usr/bin/env python3
"""ChatGPT Auto Register - Web GUI (Flask + SSE)"""

import copy, json, os, queue, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)
sys.path.insert(0, str(Path(__file__).parent))
from smsbower import SmsBower
from phone_sms_adapter import UnifiedSMS, parse_countries
import auto_register as ar
from outlook_mail import (
    OutlookMailClient,
    get_outlook_account,
    load_outlook_accounts,
    mark_outlook_status,
)
from outlook_manager import _read_used as _read_outlook_used

_STATE_LOCK = threading.RLock()

# ── File logging: one file per server start ──
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)
_log_file = _log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_log_file_lock = threading.Lock()


def _bounded_int(value, default=1, minimum=1, maximum=99):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(v, maximum))


def _stop_requested():
    with _STATE_LOCK:
        return _state["stop"]


def _empty_stats():
    return {
        "current_success": 0,
        "current_fail": 0,
        "total_success": 0,
        "total_fail": 0,
    }


_state = {
    "running": False, "stop": False, "results": [],
    "stats": _empty_stats(),
    "config": ar.load_config(), "log_queue": queue.Queue(),
    "log_lines": [], "log_cursor": 0,
    "code_queue": queue.Queue(),
    "pause_queue": queue.Queue(),
    "_paused": False, "_need_code": False,
    "_code_queues": {},
    "_code_waiting": {},
}

# ── 邮箱去重 ──
def _ensure_stats():
    stats = _state.get("stats")
    if not isinstance(stats, dict):
        stats = _empty_stats()
        _state["stats"] = stats
        return stats
    for key, default in _empty_stats().items():
        try:
            stats[key] = int(stats.get(key, default))
        except (TypeError, ValueError):
            stats[key] = default
    return stats


def _reset_current_stats():
    with _STATE_LOCK:
        stats = _ensure_stats()
        stats["current_success"] = 0
        stats["current_fail"] = 0


def _record_stat(success: bool):
    with _STATE_LOCK:
        stats = _ensure_stats()
        if success:
            stats["current_success"] += 1
            stats["total_success"] += 1
        else:
            stats["current_fail"] += 1
            stats["total_fail"] += 1


def _record_result(result: dict):
    with _STATE_LOCK:
        _state["results"].append(result)


def _status_payload():
    with _STATE_LOCK:
        results = [_sanitize_result(r) for r in _state["results"]]
        stats = dict(_ensure_stats())
        running = _state["running"]
    return {"running": running, "results": results, "stats": stats}


_email_blacklist = set()
_claimed_emails = set()
_bl_file = Path(__file__).parent / "email_blacklist.json"
_bl_lock = threading.Lock()
_cl_lock = threading.Lock()

_OUTLOOK_STATUS_LABELS = {
    "success": "已注册成功",
    "bad": "坏号",
    "verify_failed": "验证失败",
    "reserved": "已预留",
    "register_failed": "注册失败",
    "unused": "未使用",
}
_OUTLOOK_SUMMARY_KEYS = [
    "unused",
    "reserved",
    "success",
    "register_failed",
    "verify_failed",
    "bad",
]

def _load_email_blacklist():
    global _email_blacklist
    if _bl_file.exists():
        try:
            _email_blacklist = set(json.loads(_bl_file.read_text(encoding="utf-8")))
        except Exception:
            pass

def _save_email_blacklist():
    with _bl_lock:
        try:
            _bl_file.write_text(json.dumps(sorted(_email_blacklist), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

_load_email_blacklist()

# 鈹€鈹€ iCloud cookies 鏈湴鍌ㄥ瓨 鈹€鈹€
COOKIES_FILE = Path(__file__).parent / "icloud_cookies.json"

def _load_icloud_cookies():
    if COOKIES_FILE.exists():
        try:
            return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _iter_phase2_icloud_cookie_paths(config: dict):
    raw_path = (config or {}).get("icloud_cookies", "")
    if raw_path:
        path = Path(raw_path)
        yield path
        if not path.is_absolute():
            yield Path(__file__).parent / path
    yield COOKIES_FILE
    yield Path(__file__).parent / "cookies.json"


def _load_phase2_icloud_cookies(config: dict):
    for path in _iter_phase2_icloud_cookie_paths(config):
        if not path or not Path(path).exists():
            continue
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
    try:
        from icloud_hme import extract_chrome_cookies

        cookies = extract_chrome_cookies()
        if cookies:
            return cookies
    except Exception:
        pass
    return {}

def _log(msg, tag="info", thread_id=None):
    ts = time.strftime("%H:%M:%S")
    item = {"msg": str(msg), "tag": tag, "time": ts}
    if thread_id is not None:
        item["thread"] = int(thread_id)
    _state["log_queue"].put(item)
    with _STATE_LOCK:
        _state["log_lines"].append(item)
        if len(_state["log_lines"]) > 2000:
            _state["log_lines"] = _state["log_lines"][-1500:]
    # Write to log file
    tid = f"[T{thread_id}] " if thread_id is not None else ""
    line = f"{ts} {tid}{msg}"
    with _log_file_lock:
        try:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
@app.route("/")
def index():
    return Response(_HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        d = request.json or {}
        cfg = ar.load_config()
        for k in ["sms_provider", "api_key", "countries", "service", "password", "max_price", "code_timeout",
                   "sms_timeout", "imap_user", "imap_pass", "sub2api_url", "sub2api_email",
                   "sub2api_pwd", "sub2api_group", "sub2api_proxy_id", "bind_email", "icloud_cookies",
                "email_provider", "mailmanage_key", "mailmanage_category", "mailmanage_keyword", "outlook_pool",
                "debug_mode", "no_phase2", "phase2_auto_skip", "sms_sort_by_price",
                "plus_method", "plus_email", "plus_phone", "plus_pin",
                "plus_country", "plus_currency", "proxy",
                "upload_target", "cpa_management_url", "cpa_api_url", "cpa_management_key", "cpa_upload_mode"]:
            if k in d:
                if k == "sms_provider": cfg["sms"]["provider"] = d[k]
                elif k == "api_key": cfg["sms"]["api_key"] = d[k]
                elif k == "countries": cfg["sms"]["countries"] = [c.strip() for c in d[k].split(",") if c.strip()]
                elif k in ("code_timeout", "sms_timeout"): cfg[k] = int(d[k]) if d[k] else 30
                elif k == "password": cfg["register"]["password"] = d[k]
                elif k == "service": cfg["sms"][k] = d[k]
                elif k == "max_price": cfg["sms"]["max_price"] = str(d[k]).strip() or ar.DEFAULT_SMS_MAX_PRICE
                elif k == "proxy": cfg["proxy"] = d[k]
                elif k == "imap_user": cfg["icloud"] = cfg.get("icloud", {}); cfg["icloud"]["user"] = d[k]
                elif k == "imap_pass": cfg["icloud"] = cfg.get("icloud", {}); cfg["icloud"]["pass"] = d[k]
                elif k == "sub2api_url": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["url"] = d[k]
                elif k == "sub2api_email": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["email"] = d[k]
                elif k == "sub2api_pwd": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["pwd"] = d[k]
                elif k == "bind_email": cfg["bind_email"] = d[k]
                elif k == "sub2api_group": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["group"] = d[k]
                elif k == "sub2api_proxy_id": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["proxy_id"] = int(d[k]) if d[k] else 0
                elif k == "icloud_cookies": cfg["icloud_cookies"] = d[k]
                elif k == "mailmanage_key": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["api_key"] = d[k]
                elif k == "mailmanage_category": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["category"] = d[k]
                elif k == "mailmanage_keyword": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["keyword"] = d[k]
                elif k == "email_provider": cfg["email_provider"] = d[k]
                elif k == "outlook_pool": cfg[k] = d[k]
                elif k in ("plus_method", "plus_email", "plus_phone", "plus_pin", "plus_country", "plus_currency"):
                    cfg[k] = d[k]
                elif k == "debug_mode": cfg["debug_mode"] = d[k] == "1" or d[k] is True
                elif k == "no_phase2": cfg["no_phase2"] = d[k] == "1" or d[k] is True
                elif k == "phase2_auto_skip": cfg["phase2_auto_skip"] = d[k] == "1" or d[k] is True
                elif k == "sms_sort_by_price": cfg["sms_sort_by_price"] = d[k] == "1" or d[k] is True
                elif k == "upload_target":
                    target = str(d[k] or "sub2api").lower()
                    cfg["upload_target"] = target if target in ("sub2api", "cpa") else "sub2api"
                elif k == "cpa_management_url": cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["management_url"] = d[k]
                elif k == "cpa_api_url": cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["api_url"] = d[k]
                elif k == "cpa_management_key": cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["management_key"] = d[k]
                elif k == "cpa_upload_mode": cfg["cpa"] = cfg.get("cpa", {}); cfg["cpa"]["upload_mode"] = d[k] or "auto"
        if not str(cfg["sms"].get("max_price", "")).strip():
            cfg["sms"]["max_price"] = ar.DEFAULT_SMS_MAX_PRICE
        # Also write to legacy fields for runner.py backward compatibility
        cfg["sms_provider"] = cfg["sms"].get("provider", "smsbower")
        cfg["sms_api_key"] = cfg["sms"].get("api_key", "")
        cfg["sms_countries"] = ",".join(cfg["sms"].get("countries", []))
        cfg["sms_service"] = cfg["sms"].get("service", "dr")
        cfg["sms_max_price"] = cfg["sms"].get("max_price", "")
        # Also sync proxy from top-level config for runner.py compatibility
        cfg["proxy"] = cfg.get("proxy", "")
        cfg.pop("phase2", None)
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
        _state["config"] = cfg
        _save_config_file(cfg)
        return jsonify({"ok": True, "config": _sanitize_config(cfg)})
    cfg = ar.load_config()
    _state["config"] = cfg
    return jsonify({"ok": True, "config": _sanitize_config(cfg)})

@app.route("/api/balance")
def api_balance():
    sms_cfg = _state.get("config", {}).get("sms", {})
    key = sms_cfg.get("api_key", "")
    provider = sms_cfg.get("provider", "smsbower")
    if not key: return jsonify({"ok": False, "error": "No API key"})
    try:
        return jsonify({"ok": True, "balance": UnifiedSMS(provider=provider, api_key=key).balance()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/icloud-cookies", methods=["GET", "POST"])
def api_icloud_cookies():
    if request.method == "POST":
        d = request.json or {}
        raw = d.get("cookies", "")
        if not raw.strip():
            return jsonify({"ok": False, "error": "cookies 涓虹┖"})
        try:
            cookies = json.loads(raw)
        except json.JSONDecodeError as e:
            return jsonify({"ok": False, "error": f"JSON 解析失败: {e}"})
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _log(f"iCloud cookies 宸蹭繚瀛?({len(str(cookies))} bytes)", "success")
        return jsonify({"ok": True, "size": len(str(cookies))})
    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "loaded": True, "size": len(str(cookies)),
                            "preview": str(cookies)[:200]})
        except Exception:
            return jsonify({"ok": True, "loaded": False, "error": "cookies file exists but parse failed"})
    return jsonify({"ok": True, "loaded": False})


@app.route("/api/plus-upgrade", methods=["POST"])
def api_plus_upgrade():
    """瑙﹀彂 ChatGPT Plus 鍗囩骇"""
    d = request.json or {}
    access_token = d.get("access_token", "")
    session_token = d.get("session_token", "")
    if not access_token and not session_token:
        return jsonify({"ok": False, "error": "闇€瑕?access_token 鎴?session_token"})

    cfg = _state["config"]
    plus_cfg = cfg.get("plus", {})

    def _run_upgrade():
        from plus_payment import generate_plus_link, grab_midtrans_url
        from gopay_pay import GoPayPayment
        import gopay_register

        _log("=== Phase 3: Plus 鍗囩骇 ===", "success")

        _log("[Plus] 鐢熸垚鏀粯閾炬帴...", "info")
        try:
            cashier_url = generate_plus_link(
                access_token=access_token,
                cookies=cfg.get("cookies", ""),
                country=plus_cfg.get("country", "ID"),
                currency=plus_cfg.get("currency", "IDR"),
                proxy=cfg.get("proxy", ""),
            )
            _log(f"[Plus] Cashier URL: {cashier_url[:60]}...", "success")
        except Exception as e:
            _log(f"[Plus] 生成支付链接失败: {e}", "error")
            return

        _log("[Plus] 娴忚鍣ㄦ姄鍙?Midtrans URL...", "info")
        try:
            midtrans_url = grab_midtrans_url(
                cashier_url,
                proxy=cfg.get("proxy", ""),
                headless=plus_cfg.get("headless", True),
            )
            _log(f"[Plus] Midtrans: {midtrans_url[:60]}...", "success")
        except Exception as e:
            _log(f"[Plus] 娴忚鍣ㄦ姄鍙栧け璐? {e}", "error")
            return

        payment_method = d.get("plus_method", "gopay")

        if payment_method == "paypal":
            _log("[Plus] PayPal 鍗忚璺嚎...", "info")
            try:
                from plus_payment import complete_paypal_checkout_protocol
                result = complete_paypal_checkout_protocol(
                    checkout_url=cashier_url,
                    cookies_str=cfg.get("cookies", ""),
                    proxy=cfg.get("proxy", ""),
                    email=d.get("plus_email", ""),
                    log_fn=lambda m: _log(f"[Plus] {m}", "info"),
                )
                if result.get("ok"):
                    _log(f"[Plus] PayPal 付款成功!", "success")
                else:
                    _log(f"[Plus] PayPal 失败: {result.get('error')}", "error")
            except Exception as e:
                _log(f"[Plus] PayPal 寮傚父: {e}", "error")
            return

        gopay_phone = plus_cfg.get("gopay_phone", "")
        gopay_pin = plus_cfg.get("gopay_pin", "")
        if not gopay_phone or not gopay_pin:
            _log("[Plus] 闇€瑕侀厤缃?GoPay 鎵嬫満鍙峰拰 PIN", "error")
            return

        _log(f"[Plus] GoPay 浠樻 {gopay_phone}...", "info")

        def wait_otp(phone, timeout):
            _log(f"[Plus] 等待 OTP ({phone}, {timeout}s)...", "warn")
            try:
                sms_api_key = cfg.get("sms", {}).get("api_key", "") or cfg.get("smsbower", {}).get("api_key", "")
                sms_provider = cfg.get("sms", {}).get("provider", "smsbower")
                if not sms_api_key:
                    return None
                sms = UnifiedSMS(provider=sms_provider, api_key=sms_api_key)
                code = sms.wait_code(timeout=timeout, interval=3)
                return code
            except Exception:
                return None

        try:
            payment = GoPayPayment(proxy=cfg.get("proxy", ""))
            result = payment.pay(
                midtrans_url=midtrans_url,
                phone=gopay_phone.lstrip("+").lstrip("62"),
                country_code="62",
                pin=gopay_pin,
                wait_otp=wait_otp,
            )
            if result.get("success"):
                _log(f"[Plus] 付款成功! status={result.get('transaction_status')}", "success")
            else:
                _log(f"[Plus] 付款失败: {result.get('detail')}", "error")
        except Exception as e:
            _log(f"[Plus] 浠樻寮傚父: {e}", "error")

    threading.Thread(target=_run_upgrade, daemon=True).start()
    return jsonify({"ok": True, "message": "Plus upgrade started"})


@app.route("/api/start", methods=["POST"])
def api_start():
    if _state["running"]: return jsonify({"ok": False, "error": "Already running"})
    d = request.json or {}
    with _STATE_LOCK:
        _state["running"] = True
        _state["stop"] = False
        _state["results"] = []
    _reset_current_stats()
    cfg = _state["config"]
    concurrency = max(1, min(int(d.get("concurrency", 1)), 10))
    threading.Thread(target=_run, args=(cfg, int(d.get("count", 1)), int(d.get("retries", 2)), concurrency), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    _state["stop"] = True
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify(_status_payload())

@app.route("/api/download")
def api_download():
    safe = []
    for r in _state["results"]:
        if not r.get("ok"):
            continue
        item = dict(r)
        # Remove sensitive fields
        for k in ["password", "chatgpt_password", "session_token", "access_token", "refresh_token", "id_token"]:
            item.pop(k, None)
        safe.append(item)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = Path(__file__).parent / f"results_{ts}.json"
    path.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(path, as_attachment=True, download_name=path.name)

@app.route("/api/submit-code", methods=["POST"])
def api_submit_code():
    d = request.json or {}
    code = d.get("code", "").strip()
    tid = d.get("thread_id", "")
    if code and len(code) >= 4:
        if tid and tid in _state.get("_code_queues", {}):
            _state["_code_queues"][tid].put(code)
        else:
            _state["code_queue"].put(code)
            for k, q in _state.get("_code_queues", {}).items():
                q.put(code)
                break
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "too short"})

@app.route("/api/waiting-code")
def api_waiting_code():
    waiting = _state.get("_code_waiting", {})
    if waiting:
        tid = list(waiting.keys())[0]
        return jsonify({"waiting": True, "thread_id": tid, "hint": waiting[tid]})
    return jsonify({"waiting": not _state["code_queue"].empty() or _state.get("_need_code", False)})

@app.route("/api/proxies")
def api_proxies():
    sub = _state["config"].get("sub2api", {})
    if not sub.get("url") or not sub.get("email"):
        return jsonify({"ok": False, "items": []})
    try:
        import requests as _r
        r = _r.post(f"{sub['url']}/api/v1/auth/login",
            json={"email": sub["email"], "password": sub.get("pwd", "")}, timeout=15)
        data = r.json()
        if data.get("code") != 0:
            return jsonify({"ok": False, "items": []})
        token = data["data"]["access_token"]
        r = _r.get(f"{sub['url']}/api/v1/admin/proxies",
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        pdata = r.json()
        items = pdata.get("data", {}).get("items", [])
        result = [{"id": p.get("id"), "name": p.get("name") or f"{p.get('host','')}:{p.get('port','')}"} for p in items]
        return jsonify({"ok": True, "items": result})
    except Exception as e:
        return jsonify({"ok": False, "items": [], "error": str(e)})

@app.route("/api/waiting-pause")
def api_waiting_pause():
    return jsonify({
        "paused": _state.get("_paused", False),
        "phase2_retry": _state.get("_phase2_retry", False),
    })

@app.route("/api/continue", methods=["POST"])
def api_continue():
    _state["pause_queue"].put("continue")
    _state["_paused"] = False
    return jsonify({"ok": True})

@app.route("/api/skip-phase2", methods=["POST"])
def api_skip_phase2():
    _state["_phase2_retry"] = False
    _state["_paused"] = False
    _state["pause_queue"].put("skip")
    return jsonify({"ok": True})

@app.route("/api/log-since/<int:cursor>")
def api_log_since(cursor):
    lines = _state["log_lines"][cursor:]
    return jsonify({"lines": lines, "cursor": len(_state["log_lines"])})

def _result_upload_complete(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    target = str(data.get("upload_target") or "sub2api").lower()
    if data.get("final_ok") or (data.get("uploaded") and data.get("upload_verified", False)):
        if target == "cpa":
            return True
        if data.get("sub2api_id") or data.get("sub2api_account_id"):
            return True
    if data.get("needs_retry"):
        return False
    if target == "cpa":
        return bool(data.get("uploaded") and data.get("upload_verified", False))
    return bool((data.get("sub2api_id") or data.get("sub2api_account_id")) and data.get("upload_verified", False))


# ---- Results list API ----
@app.route("/api/results-list")
def api_results_list():
    source = request.args.get("source", "files")
    results_dir = Path(__file__).parent / "results"
    if not results_dir.exists():
        return jsonify({"ok": True, "items": []})
    items = []

    if source == "all":
        all_path = results_dir / "_all.json"
        if not all_path.exists():
            return jsonify({"ok": True, "items": []})
        try:
            all_data = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"ok": True, "items": []})
        for idx, data in enumerate(all_data):
            # Include both final_ok and phone_ok results
            if not data.get("ok") and not data.get("phone_ok"):
                continue
            if _result_upload_complete(data):
                continue
            items.append({
                "index": idx,
                "phone": data.get("phone", "?"),
                "name": data.get("name", ""),
                "has_phase2": _result_upload_complete(data),
                "sub2api_id": data.get("sub2api_id", ""),
            })
    else:
        for f in sorted(results_dir.iterdir(), key=lambda x: x.name):
            if f.suffix != ".json" or f.name == "_all.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            # Include both final_ok and phone_ok results
            if not data.get("ok") and not data.get("phone_ok"):
                continue
            if _result_upload_complete(data):
                continue
            items.append({
                "filename": f.name,
                "phone": data.get("phone", "?"),
                "name": data.get("name", ""),
                "has_phase2": _result_upload_complete(data),
                "sub2api_id": data.get("sub2api_id", ""),
            })
    return jsonify({"ok": True, "items": items})


def _current_config() -> dict:
    cfg = _state.get("config")
    if isinstance(cfg, dict):
        return cfg
    cfg = ar.load_config()
    _state["config"] = cfg
    return cfg


def _outlook_results_dir() -> Path:
    return Path(__file__).parent / "results"


def _outlook_pool_source(config: dict) -> str:
    return (config or {}).get("outlook_pool") or "outlook.txt"


def _outlook_used_source(config: dict) -> str:
    return (config or {}).get("outlook_used") or "outlook_used.txt"


def _parse_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _parse_result_file_time(path: Path) -> tuple[float, str]:
    stem_parts = path.stem.rsplit("_", 2)
    if len(stem_parts) >= 3:
        label = f"{stem_parts[-2]}_{stem_parts[-1]}"
        try:
            ts = datetime.strptime(label, "%Y%m%d_%H%M%S")
            return ts.timestamp(), ts.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    try:
        ts = path.stat().st_mtime
        return ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0.0, ""


def _extract_result_time(data: dict, fallback_ts: float = 0.0) -> tuple[float, str]:
    for key in ("last_result_time", "created_at", "updated_at", "saved_at", "time", "timestamp"):
        value = data.get(key, "")
        if isinstance(value, (int, float)) and value:
            ts = float(value)
            return ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        ts = _parse_timestamp(value)
        if ts:
            return ts, str(value)
    if fallback_ts:
        return fallback_ts, datetime.fromtimestamp(fallback_ts).strftime("%Y-%m-%d %H:%M:%S")
    return 0.0, ""


def _load_outlook_result_records() -> list[dict]:
    results_dir = _outlook_results_dir()
    if not results_dir.exists():
        return []

    records = []
    all_path = results_dir / "_all.json"
    if all_path.exists():
        try:
            all_data = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            all_data = []
        if isinstance(all_data, list):
            for idx, row in enumerate(all_data, 1):
                if not isinstance(row, dict):
                    continue
                fallback_ts = float(idx)
                recorded_at, time_label = _extract_result_time(row, fallback_ts)
                records.append(
                    {
                        "ok": bool(row.get("ok")),
                        "phone": str(row.get("phone", "") or ""),
                        "sub2api_id": str(row.get("sub2api_id", "") or ""),
                        "bind_email": str(row.get("bind_email", "") or "").strip(),
                        "recorded_at": recorded_at,
                        "time_label": time_label,
                        "data": row,
                    }
                )

    for path in sorted(results_dir.glob("*.json")):
        if path.name == "_all.json":
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        fallback_ts, file_label = _parse_result_file_time(path)
        recorded_at, time_label = _extract_result_time(row, fallback_ts)
        records.append(
            {
                "ok": bool(row.get("ok")),
                "phone": str(row.get("phone", "") or ""),
                "sub2api_id": str(row.get("sub2api_id", "") or ""),
                "bind_email": str(row.get("bind_email", "") or "").strip(),
                "recorded_at": recorded_at,
                "time_label": time_label or file_label,
                "data": row,
            }
        )
    return records


def _classify_outlook_status(has_success_result: bool, last_event_status: str, has_result: bool) -> str:
    status = (last_event_status or "").strip().lower()
    if has_success_result:
        return "success"
    if status == "bad":
        return "bad"
    if status == "verify_failed":
        return "verify_failed"
    # Once a result exists, do not keep showing the earlier reserved event.
    # Successful Phase2 is handled above; remaining result records mean registration/Phase2 failed.
    if has_result:
        return "register_failed"
    if status == "reserved":
        return "reserved"
    return "unused"


def _build_outlook_pool_entries(config: dict | None = None) -> list[dict]:
    config = config or _current_config()
    accounts = load_outlook_accounts(_outlook_pool_source(config))
    latest_statuses, events = _read_outlook_used(_outlook_used_source(config))
    latest_events = {}
    for event_time, email, status in events:
        latest_events[email.lower()] = {
            "time": event_time or "",
            "status": (status or "").strip().lower(),
        }

    latest_results = {}
    has_results = set()
    success_results = set()
    for record in _load_outlook_result_records():
        bind_email = record["bind_email"].lower()
        if not bind_email:
            continue
        has_results.add(bind_email)
        target = str(record.get("data", {}).get("upload_target") or "sub2api").lower()
        upload_complete = bool(record.get("data", {}).get("uploaded") and record.get("data", {}).get("upload_verified", False))
        if target == "cpa":
            is_success = upload_complete
        else:
            is_success = bool((record["sub2api_id"] or record.get("data", {}).get("sub2api_account_id")) and record.get("data", {}).get("upload_verified", False))
        if is_success:
            success_results.add(bind_email)
        current = latest_results.get(bind_email)
        if current is None or record["recorded_at"] >= current["recorded_at"]:
            latest_results[bind_email] = record

    current_bind = (config.get("bind_email") or "").strip().lower()
    entries = []
    for account in accounts:
        email = account.email
        key = email.lower()
        event = latest_events.get(key, {})
        result = latest_results.get(key)
        status = _classify_outlook_status(
            has_success_result=key in success_results,
            last_event_status=event.get("status", latest_statuses.get(key, "")),
            has_result=key in has_results,
        )
        sort_ts = _parse_timestamp(event.get("time", "")) or float((result or {}).get("recorded_at", 0.0))
        entries.append(
            {
                "email": email,
                "status": status,
                "status_label": _OUTLOOK_STATUS_LABELS[status],
                "last_event_time": event.get("time", ""),
                "last_event_status": event.get("status", latest_statuses.get(key, "")),
                "has_result": key in has_results,
                "result_ok": bool((result or {}).get("ok")),
                "phone": (result or {}).get("phone", ""),
                "sub2api_id": (result or {}).get("sub2api_id", ""),
                "bind_email": (result or {}).get("bind_email", ""),
                "last_result_time": (result or {}).get("time_label", ""),
                "can_assign": status not in ("bad", "success"),
                "can_mark_bad": True,
                "can_mark_verify_failed": True,
                "can_mark_reserved": True,
                "is_current_bind": key == current_bind,
                "_sort_ts": sort_ts,
            }
        )
    return entries


def _sanitize_outlook_entry(entry: dict) -> dict:
    clean = dict(entry or {})
    clean.pop("_sort_ts", None)
    return clean


def _outlook_list_sort_key(entry: dict):
    status = entry.get("status", "")
    bucket = 2
    if status == "unused":
        bucket = 0
    elif status == "reserved":
        bucket = 1
    return (
        bucket,
        -float(entry.get("_sort_ts", 0.0) or 0.0),
        str(entry.get("email", "")).lower(),
    )


def _find_outlook_pool_entry(email: str, entries: list[dict]) -> dict | None:
    target = (email or "").strip().lower()
    if not target:
        return None
    for entry in entries:
        if entry.get("email", "").lower() == target:
            return entry
    return None


@app.route("/api/outlook-pool/summary")
def api_outlook_pool_summary():
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    counts = {key: 0 for key in _OUTLOOK_SUMMARY_KEYS}
    for entry in entries:
        counts[entry["status"]] += 1
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "total": len(entries),
            "counts": counts,
            "current_bind_email": cfg.get("bind_email", ""),
            "email_provider": cfg.get("email_provider", ""),
        }
    )


@app.route("/api/outlook-pool/list")
def api_outlook_pool_list():
    status = (request.args.get("status", "all") or "all").strip().lower()
    query = (request.args.get("q", "") or "").strip().lower()
    page = _bounded_int(request.args.get("page"), default=1, minimum=1, maximum=9999)
    page_size = _bounded_int(request.args.get("page_size"), default=20, minimum=1, maximum=100)
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    rows = []
    for entry in entries:
        if status != "all" and entry["status"] != status:
            continue
        if query:
            haystack = "\n".join(
                [
                    entry.get("email", ""),
                    entry.get("phone", ""),
                    entry.get("bind_email", ""),
                    entry.get("sub2api_id", ""),
                ]
            ).lower()
            if query not in haystack:
                continue
        rows.append(entry)

    rows.sort(key=_outlook_list_sort_key)
    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "items": [_sanitize_outlook_entry(entry) for entry in rows[start:end]],
            "total": total,
            "page": page,
            "page_size": page_size,
            "current_bind_email": cfg.get("bind_email", ""),
        }
    )


@app.route("/api/outlook-pool/detail")
def api_outlook_pool_detail():
    email = (request.args.get("email", "") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email is required"}), 400
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    entry = _find_outlook_pool_entry(email, entries)
    if not entry:
        return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "entry": _sanitize_outlook_entry(entry),
            "current_bind_email": cfg.get("bind_email", ""),
        }
    )


@app.route("/api/outlook-pool/messages")
def api_outlook_pool_messages():
    email = (request.args.get("email", "") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email is required"}), 400
    cfg = _current_config()
    limit = _bounded_int(request.args.get("limit"), default=20, minimum=1, maximum=50)
    include_body = str(request.args.get("include_body", "1")).lower() not in ("0", "false", "no")
    try:
        account = get_outlook_account(email, _outlook_pool_source(cfg))
        client = OutlookMailClient(
            account,
            verbose=False,
            proxy=cfg.get("proxy", ""),
            prefer_imap=True,
        )
        items = client.list_recent_messages(limit=limit, include_body=include_body)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "email": account.email, "items": items})


@app.route("/api/outlook-pool/action", methods=["POST"])
def api_outlook_pool_action():
    data = request.json or {}
    action = (data.get("action", "") or "").strip()
    cfg = dict(_current_config())
    try:
        entries = _build_outlook_pool_entries(cfg)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if action == "mark_status":
        email = (data.get("email", "") or "").strip()
        status = (data.get("status", "") or "").strip().lower()
        if status not in ("reserved", "verify_failed", "bad"):
            return jsonify({"ok": False, "error": "invalid status"}), 400
        entry = _find_outlook_pool_entry(email, entries)
        if not entry:
            return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
        mark_outlook_status(entry["email"], status, _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(entry["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": entry["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    if action == "assign_for_run":
        email = (data.get("email", "") or "").strip()
        entry = _find_outlook_pool_entry(email, entries)
        if not entry:
            return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
        if not entry.get("can_assign"):
            return jsonify({"ok": False, "error": "selected email cannot be assigned"}), 400
        cfg["bind_email"] = entry["email"]
        cfg["email_provider"] = "outlook"
        _state["config"] = cfg
        _save_config_file(cfg)
        mark_outlook_status(entry["email"], "reserved", _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(entry["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": entry["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    if action == "reserve_next_unused":
        target = None
        for entry in sorted(entries, key=_outlook_list_sort_key):
            if entry["status"] == "unused":
                target = entry
                break
        if not target:
            return jsonify({"ok": False, "error": "no unused outlook account available"}), 400
        cfg["bind_email"] = target["email"]
        cfg["email_provider"] = "outlook"
        _state["config"] = cfg
        _save_config_file(cfg)
        mark_outlook_status(target["email"], "reserved", _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(target["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": target["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    return jsonify({"ok": False, "error": "unsupported action"}), 400


@app.route("/api/failed-uploads/list")
def api_failed_uploads_list():
    try:
        from failed_uploads import BASE_DIR

        base_dir = BASE_DIR.resolve()
        items = []
        if base_dir.exists():
            for path in sorted(base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                items.append(
                    {
                        "filename": path.name,
                        "path": f"failed_uploads/{path.name}",
                        "phone": data.get("phone", ""),
                        "email": data.get("email") or data.get("bind_email", ""),
                        "upload_target": data.get("upload_target", "sub2api"),
                        "created_at": data.get("created_at", ""),
                        "last_error": data.get("last_error", ""),
                    }
                )
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _update_result_after_failed_upload_retry(failed_path: Path, retry_result: dict):
    if not retry_result.get("ok"):
        return
    results_dir = Path(__file__).parent / "results"
    if not results_dir.exists():
        return
    failed_resolved = str(Path(failed_path).resolve())
    failed_name = Path(failed_path).name
    for path in results_dir.glob("*.json"):
        if path.name == "_all.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        marker = str(data.get("failed_upload_file") or "")
        if marker not in {failed_resolved, str(failed_path), f"failed_uploads/{failed_name}"} and Path(marker).name != failed_name:
            continue
        data["uploaded"] = True
        data["upload_verified"] = True
        data["needs_retry"] = False
        data["final_ok"] = True
        data["ok"] = True
        data["status"] = "final_ok"
        data["upload_target"] = retry_result.get("upload_target", data.get("upload_target", "sub2api"))
        if retry_result.get("sub2api_account_id"):
            data["sub2api_id"] = retry_result.get("sub2api_account_id")
        data.pop("upload_error", None)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@app.route("/api/failed-uploads/retry", methods=["POST"])
def api_failed_upload_retry():
    d = request.json or {}
    raw_path = str(d.get("path", "")).strip()
    if not raw_path:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        from failed_uploads import BASE_DIR, retry_failed_upload

        base_dir = BASE_DIR.resolve()
        requested = Path(raw_path)
        candidate = requested if requested.is_absolute() else Path(__file__).parent / requested
        resolved = candidate.resolve()
        if resolved == base_dir or base_dir not in resolved.parents or resolved.suffix != ".json":
            return jsonify({"ok": False, "error": "path must be a failed_uploads json file"}), 400
        _log(f"[失败上传补传] 开始: {resolved.name}", "info")
        result = retry_failed_upload(resolved, _state.get("config", {}))
        _update_result_after_failed_upload_retry(resolved, result)
        _record_stat(bool(result.get("ok")))
        if result.get("ok"):
            _log(f"[失败上传补传] 成功: {resolved.name}", "success")
        else:
            _log(f"[失败上传补传] 失败: {resolved.name} {result.get('error', result)}", "error")
        return jsonify({"ok": bool(result.get("ok")), "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/batch-phase2-delete", methods=["POST"])
def api_batch_phase2_delete():
    d = request.json or {}
    source = d.get("source", "files")
    if source != "files":
        return jsonify({"ok": False, "error": "只能删除 results目录 的文件记录"}), 400
    files = d.get("files", [])
    if not files:
        return jsonify({"ok": False, "error": "未选择文件"}), 400

    results_dir = (Path(__file__).parent / "results").resolve()
    deleted = 0
    for name in files:
        candidate = (results_dir / str(name)).resolve()
        if candidate == results_dir or results_dir not in candidate.parents or candidate.suffix != ".json" or candidate.name == "_all.json":
            return jsonify({"ok": False, "error": "只能删除 results 目录下的 JSON 结果文件"}), 400
        if candidate.exists():
            candidate.unlink()
            deleted += 1
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/batch-phase2", methods=["POST"])
def api_batch_phase2():
    if _state["running"]:
        return jsonify({"ok": False, "error": "task already running"})
    d = request.json or {}
    source = d.get("source", "files")
    files = d.get("files", [])
    if not files:
        return jsonify({"ok": False, "error": "未选择文件"})
    email = d.get("email", "").strip()
    concurrency = max(1, min(int(d.get("concurrency", 1)), 10))
    _state["stop"] = False
    cfg = _state["config"]
    threading.Thread(target=_run_batch_phase2, args=(files, cfg, email, source, concurrency), daemon=True).start()
    return jsonify({"ok": True})


def _country_order_for_attempt(countries, attempt_index: int, concurrency: int = 1):
    """Return country order for an attempt.

    In parallel mode, all workers in the same concurrency wave should start from
    the same cheapest-first order. Otherwise only the first worker gets the
    cheapest country while other simultaneous workers start from later countries.
    """
    if len(countries) <= 1:
        return countries
    wave_size = max(1, concurrency)
    wave_index = max(0, attempt_index) // wave_size
    offset = wave_index % len(countries)
    return countries[offset:] + countries[:offset]



def _save_config_file(cfg: dict):
    path = Path(__file__).parent / "config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def _save_result(results_dir: Path, result: dict, config: dict):
    # Save both final OK and phone_ok (pending Phase2) results
    if not result.get("ok") and not result.get("phone_ok"):
        return
    safe = dict(result)
    safe["bind_email"] = config.get("bind_email", "")
    # Keep explicit credential aliases so future lookups do not depend on UI wording.
    if safe.get("password"):
        safe["chatgpt_password"] = safe.get("password", "")
    if safe.get("bind_email"):
        safe["account_email"] = safe.get("bind_email", "")
    ts = time.strftime("%Y%m%d_%H%M%S")
    phone = result.get("phone", "unknown").replace("+", "")
    path = results_dir / f"{phone}_{ts}.json"
    path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    all_path = results_dir / "_all.json"
    all_results = []
    if all_path.exists():
        try:
            all_results = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    all_results.append(safe)
    all_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    credential_record = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phone": result.get("phone", ""),
        "chatgpt_password": safe.get("chatgpt_password", ""),
        "bind_email": safe.get("bind_email", ""),
        "upload_target": safe.get("upload_target", config.get("upload_target", "")),
        "status": safe.get("status", ""),
        "result_file": str(path),
    }
    credentials_path = results_dir / "credentials.jsonl"
    with credentials_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(credential_record, ensure_ascii=False) + "\n")
    if credential_record.get("chatgpt_password"):
        _log(
            f"[凭证已保存] phone={credential_record['phone']} email={credential_record['bind_email'] or '-'} file={path.name}",
            "success",
        )

def _sanitize_config(cfg):
    return copy.deepcopy(cfg)

def _sanitize_result(r):
    r2 = dict(r)
    for k in ["session_token", "access_token"]:
        if r2.get(k): r2[k] = r2[k][:30] + "..."
    for k in ["password", "chatgpt_password", "refresh_token", "id_token"]:
        if r2.get(k): r2[k] = "***"
    return r2

class _LogWriter:
    def __init__(self, log_fn):
        self._log = log_fn
        self._lock = threading.RLock()
        self._buffers = {}
        self._thread_map = {}

    def bind_thread(self, thread_id):
        ident = threading.get_ident()
        with self._lock:
            self._thread_map[ident] = thread_id
            self._buffers.setdefault(ident, "")

    def unbind_thread(self):
        ident = threading.get_ident()
        self.flush()
        with self._lock:
            self._thread_map.pop(ident, None)
            self._buffers.pop(ident, None)

    def _state_for_current_thread(self):
        ident = threading.get_ident()
        with self._lock:
            self._buffers.setdefault(ident, "")
            return ident, self._buffers[ident], self._thread_map.get(ident)

    def write(self, s):
        if not s:
            return 0
        ident, buf, thread_id = self._state_for_current_thread()
        buf += str(s)
        lines = []
        while "\n" in buf:
            idx = buf.index("\n")
            line = buf[:idx].strip()
            buf = buf[idx + 1:]
            if line:
                lines.append(line)
        with self._lock:
            self._buffers[ident] = buf
        for line in lines:
            self._log(line, "info", thread_id=thread_id)
        return len(str(s))

    def flush(self):
        ident, buf, thread_id = self._state_for_current_thread()
        line = buf.strip()
        if line:
            self._log(line, "info", thread_id=thread_id)
        with self._lock:
            self._buffers[ident] = ""


def _phase2_email_is_bound(result: dict) -> bool:
    if _result_upload_complete(result):
        return True
    if result.get("email_bound") is False:
        return False
    return bool(result.get("bind_email"))


def _phase2_error_status(error: str) -> str:
    text = str(error or "").lower()
    if "email_already_in_use" in text or "this email is already in use" in text:
        return "email_already_in_use"
    return "register_failed"


def _validate_phase2_result(result: dict) -> None:
    for field in ("phone", "password"):
        if not str(result.get(field) or "").strip():
            raise RuntimeError(f"missing_phase2_field:{field}")


def _phase2_for_result(result: dict, config: dict, thread_tag: str = "", thread_id=None) -> dict:
    """Run Phase 2 for one registered account and upload to selected target."""
    _validate_phase2_result(result)
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


def _run_batch_phase2(files: list, config: dict, email: str = "", source: str = "files", concurrency: int = 1):
    old_stdout = sys.stdout
    run_config = copy.deepcopy(config)
    if email:
        run_config["bind_email"] = email
        _log(f"[补跑] 使用指定邮箱: {email}", "info")

    email_provider = run_config.get("email_provider", "")
    mm_config = run_config.get("mailmanage", {})
    mm = None
    ic = None
    provider_lock = threading.Lock()
    use_outlook = run_config.get("mail_provider") == "outlook" or email_provider == "outlook"

    # 统一初始化邮箱提供商
    provider_name, provider_instance, use_outlook = _get_email_provider(run_config, _log)
    mm = provider_instance if provider_name == "mailmanage" else None
    ic = provider_instance if provider_name == "icloud" else None

    # 验证 Outlook 池存在
    if use_outlook and not email:
        outlook_pool_path = run_config.get("outlook_pool") or "outlook.txt"
        from pathlib import Path as _Path
        from outlook_mail import _is_inline_pool_text
        is_inline = _is_inline_pool_text(outlook_pool_path)
        if not is_inline:
            outlook_file = _Path(outlook_pool_path)
            if not outlook_file.is_absolute():
                outlook_file = _Path(__file__).parent / outlook_pool_path
            if not outlook_file.exists():
                _log(f"[补跑] Outlook 池文件不存在: {outlook_file}", "error")
                _log(f"[补跑] 请恢复 outlook.txt 或改用其他邮箱提供商", "error")
                with _STATE_LOCK:
                    _state["running"] = False
                sys.stdout = old_stdout
                return
        # 检查是否有可用邮箱
        try:
            from outlook_mail import load_outlook_accounts, _used_emails
            accounts = load_outlook_accounts(outlook_pool_path if is_inline else str(outlook_file))
            used = _used_emails(_Path(__file__).parent / (run_config.get("outlook_used") or "outlook_used.txt"))
            available = [a for a in accounts if a.email.lower() not in used]
            if not available:
                _log(f"[补跑] Outlook 池中所有邮箱已被使用，无可用邮箱", "error")
                _log(f"[补跑] 请恢复更多邮箱到 outlook.txt", "error")
                with _STATE_LOCK:
                    _state["running"] = False
                sys.stdout = old_stdout
                return
            _log(f"[补跑] Outlook 可用邮箱: {len(available)}/{len(accounts)}", "info")
        except Exception as e:
            _log(f"[补跑] Outlook 池验证失败: {e}", "warn")

    with _STATE_LOCK:
        _state["running"] = True
        _state["_phase2_retry"] = False

    log_writer = _LogWriter(_log)
    sys.stdout = log_writer
    results_dir = Path(__file__).parent / "results"

    sub = run_config.get("sub2api", {})
    upload_target = str(run_config.get("upload_target") or "sub2api").lower()
    cpa = run_config.get("cpa", {})
    if upload_target == "cpa":
        if not cpa.get("api_url"):
            _log("[batch] CPA API 地址未配置", "error")
            with _STATE_LOCK:
                _state["running"] = False
            sys.stdout = old_stdout
            return
        if not cpa.get("management_key"):
            _log("[batch] CPA 管理密钥未配置", "error")
            with _STATE_LOCK:
                _state["running"] = False
            sys.stdout = old_stdout
            return
    elif not sub.get("url") or not sub.get("email"):
        _log("[batch] please configure SUB2API url and email first", "error")
        with _STATE_LOCK:
            _state["running"] = False
        sys.stdout = old_stdout
        return

    is_multi = concurrency > 1
    summary = f"[batch] start Phase 2 for {len(files)} items"
    if is_multi:
        summary += f", 并发 {concurrency} 线程"
    _log(summary, "info")

    all_data = None
    if source == "all":
        all_path = results_dir / "_all.json"
        if all_path.exists():
            try:
                all_data = json.loads(all_path.read_text(encoding="utf-8"))
            except Exception as e:
                _log(f"[补跑] 读取 _all.json 失败: {e}", "error")

    counters = {"ok": 0, "fail": 0, "processed": 0, "total": len(files)}
    counter_lock = threading.Lock()
    file_queue = queue.Queue()
    for item in files:
        file_queue.put(item)

    def _report_progress(tag=""):
        """报告补跑进度"""
        with counter_lock:
            processed = counters["processed"]
            total = counters["total"]
            ok = counters["ok"]
            fail = counters["fail"]
            if total > 0:
                pct = (processed / total) * 100
                _log(f"[补跑] {tag} 进度: {processed}/{total} ({pct:.0f}%) - 成功:{ok} 失败:{fail}", "info")

    def _reserve_email(thread_id, phone, original_email="", require_original=False):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)

        if original_email and require_original:
            if use_outlook:
                try:
                    from outlook_mail import get_outlook_account

                    get_outlook_account(
                        original_email,
                        run_config.get("outlook_pool") or "outlook.txt",
                        run_config.get("outlook_used") or "outlook_used.txt",
                    )
                    tlog(f"[补跑] {tag} [{phone}] 使用原始邮箱: {original_email}", "info")
                    return original_email
                except Exception as e:
                    tlog(f"[补跑] {tag} [{phone}] 原始邮箱不可用，无法补跑已绑定邮箱账号: {e}", "error")
                    return ""
            else:
                tlog(f"[补跑] {tag} [{phone}] 使用原始邮箱: {original_email}", "info")
                return original_email

        if email:
            return email
        if mm is not None:
            try:
                with provider_lock:
                    picked = mm.get_available_email(category=mm_config.get("category", "free"))
                tlog(f"[补跑] {tag} [{phone}] MailManage 取号: {picked}", "info")
                return picked
            except Exception as e:
                tlog(f"[补跑] {tag} [{phone}] MailManage 取号失败: {e}", "error")
                return ""
        if use_outlook:
            try:
                from outlook_mail import reserve_next_outlook

                with provider_lock:
                    outlook_account = reserve_next_outlook(
                        run_config.get("outlook_pool") or "outlook.txt",
                        run_config.get("outlook_used") or "outlook_used.txt",
                    )
                tlog(f"[补跑] {tag} [{phone}] Outlook 取号: {outlook_account.email}", "info")
                return outlook_account.email
            except Exception as e:
                tlog(f"[补跑] {tag} [{phone}] Outlook 閸欐牕褰挎径杈Е: {e}", "error")
                return ""
        if ic is not None:
            try:
                with provider_lock:
                    picked = ic.reuse_or_create_alias()
                tlog(f"[补跑] {tag} [{phone}] iCloud 别名: {picked}", "info")
                return picked
            except Exception as e:
                tlog(f"[补跑] {tag} [{phone}] iCloud 取号失败: {e}", "error")
                return ""
        return ""

    def _batch_worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        log_writer.bind_thread(thread_id)
        try:
            while not _state["stop"]:
                try:
                    fname = file_queue.get_nowait()
                except queue.Empty:
                    return

                fpath = None
                if source == "all":
                    try:
                        idx = int(fname)
                        if all_data is None or idx >= len(all_data):
                            tlog(f"[补跑] {tag} 索引超出范围: {fname}", "error")
                            continue
                        result = dict(all_data[idx])
                    except (ValueError, IndexError, TypeError) as e:
                        tlog(f"[补跑] {tag} 无效索引: {fname} ({e})", "error")
                        continue
                else:
                    fpath = results_dir / fname
                    if not fpath.exists():
                        tlog(f"[补跑] {tag} 文件不存在: {fname}", "error")
                        continue
                    try:
                        result = json.loads(fpath.read_text(encoding="utf-8"))
                    except Exception as e:
                        tlog(f"[补跑] {tag} 读取失败: {fname} ({e})", "error")
                        continue

                # Accept both final OK and phone_ok (pending Phase2) records
                if not result.get("ok") and not result.get("phone_ok"):
                    tlog(f"[补跑] {tag} 跳过失败记录: {result.get('phone', '?')}", "warn")
                    continue
                if _result_upload_complete(result):
                    tlog(f"[补跑] {tag} 跳过已完成记录 {result.get('phone', '?')}", "info")
                    continue
                try:
                    _validate_phase2_result(result)
                except RuntimeError as e:
                    phone = result.get("phone", "?")
                    tlog(f"[补跑] {tag} [{phone}] 跳过无效记录: {e}", "error")
                    with counter_lock:
                        counters["fail"] += 1
                    _record_stat(False)
                    continue

                phone = result.get("phone", "?")
                original_email = result.get("bind_email", "")
                used_email = _reserve_email(thread_id, phone, original_email, require_original=_phase2_email_is_bound(result))
                if not used_email:
                    tlog(f"[补跑] {tag} [{phone}] 没有可用邮箱，跳过", "error")
                    with counter_lock:
                        counters["fail"] += 1
                    _record_stat(False)
                    continue

                thread_cfg = copy.deepcopy(run_config)
                thread_cfg["bind_email"] = used_email
                tlog(f"[补跑] {tag} [{phone}] 开始 Phase 2 (邮箱: {used_email}) ...", "info")

                try:
                    oauth_result = _phase2_for_result(result, thread_cfg, tag, thread_id=thread_id)
                except Exception as e:
                    oauth_result = {"ok": False, "error": str(e)}

                if oauth_result.get("ok"):
                    with counter_lock:
                        counters["ok"] += 1
                        counters["processed"] += 1
                    result["bind_email"] = used_email
                    result["email_bound"] = True
                    result["uploaded"] = bool(oauth_result.get("uploaded", True))
                    result["upload_verified"] = bool(oauth_result.get("upload_verified", result["uploaded"]))
                    result["upload_target"] = oauth_result.get("upload_target", thread_cfg.get("upload_target", "sub2api"))
                    if oauth_result.get("sub2api_account_id"):
                        result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
                    if oauth_result.get("upload_error"):
                        result["upload_error"] = oauth_result.get("upload_error", "")
                    if oauth_result.get("failed_upload_file"):
                        result["failed_upload_file"] = oauth_result.get("failed_upload_file", "")
                    result["final_ok"] = bool(result["uploaded"] and result.get("upload_verified", False))
                    result["ok"] = True
                    result["status"] = "final_ok" if result["final_ok"] else "upload_unverified"
                    _record_stat(result["final_ok"])
                    if run_config.get("mail_provider") == "outlook" or run_config.get("email_provider") == "outlook":
                        try:
                            outlook_status = "success" if result["final_ok"] else "verify_failed"
                            mark_outlook_status(used_email, outlook_status, run_config.get("outlook_used") or "outlook_used.txt")
                        except Exception as e:
                            tlog(f"[补跑] {tag} [{phone}] Outlook 状态更新失败: {e}", "warn")
                    with counter_lock:
                        if source == "all" and all_data is not None:
                            all_data[int(fname)] = result
                            (results_dir / "_all.json").write_text(
                                json.dumps(all_data, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                        elif fpath is not None:
                            fpath.write_text(
                                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                    if result["uploaded"]:
                        if result["upload_target"] == "cpa":
                            tlog(f"[补跑] {tag} [{phone}] CPA 上传成功 ({oauth_result.get('upload_method', 'unknown')})", "success")
                        else:
                            tlog(f"[补跑] {tag} [{phone}] 成功, sub2api_id={result.get('sub2api_id', '?')}", "success")
                    else:
                        tlog(f"[补跑] {tag} [{phone}] 账号生成成功，但上传失败: {result.get('upload_error', '?')} 文件={result.get('failed_upload_file', '-')}", "warn")
                    if mm is not None:
                        try:
                            with provider_lock:
                                mm.mark_used(used_email)
                            tlog(f"[补跑] {tag} [{phone}] MailManage 已标记: {used_email}", "info")
                        except Exception as e:
                            tlog(f"[补跑] {tag} [{phone}] 标记失败: {e}", "warn")
                else:
                    with counter_lock:
                        counters["fail"] += 1
                    _record_stat(False)
                    if run_config.get("mail_provider") == "outlook" or run_config.get("email_provider") == "outlook":
                        try:
                            mark_outlook_status(used_email, _phase2_error_status(oauth_result.get("error", "")), run_config.get("outlook_used") or "outlook_used.txt")
                        except Exception as e:
                            tlog(f"[补跑] {tag} [{phone}] Outlook 状态更新失败: {e}", "warn")
                    tlog(f"[补跑] {tag} [{phone}] 失败: {oauth_result.get('error', '?')}", "error")
        finally:
            log_writer.unbind_thread()

    try:
        threads = []
        for i in range(concurrency):
            thread = threading.Thread(target=_batch_worker, args=(i + 1,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
    finally:
        sys.stdout = old_stdout

    with _STATE_LOCK:
        _state["running"] = False
    _log(
        f"[补跑] 完成: {counters['ok']} 成功 / {counters['fail']} 失败",
        "success" if counters["ok"] > 0 else "warn",
    )


def _get_email_provider(run_config, log_func=None):
    """
    统一邮箱提供商选择和初始化逻辑
    返回: (provider_name, provider_instance, use_outlook)
    """
    email_provider = run_config.get("email_provider", "")
    mm_config = run_config.get("mailmanage", {})
    mm = None
    ic = None
    use_outlook = run_config.get("mail_provider") == "outlook" or email_provider == "outlook"

    def _log(msg, level="info"):
        if log_func:
            log_func(msg, level)

    if email_provider == "mailmanage" and mm_config.get("api_key"):
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
            _log("MailManage client initialized", "info")
        except Exception as e:
            _log(f"MailManage 初始化失败: {e}", "error")
    elif not use_outlook:
        cookies = _load_phase2_icloud_cookies(run_config)
        if cookies:
            try:
                from icloud_hme import ICloudHME
                ic = ICloudHME(cookies, verbose=False)
                _log("iCloud HME initialized", "info")
            except Exception as e:
                _log(f"iCloud 初始化失败: {e}", "error")

    if email_provider == "mailmanage" and mm:
        return "mailmanage", mm, False
    elif use_outlook:
        return "outlook", None, True
    elif ic:
        return "icloud", ic, False
    else:
        return "none", None, False


def _run(config, count, retries, concurrency=1):
    run_config = copy.deepcopy(config)
    old_stdout = sys.stdout
    log_writer = _LogWriter(_log)
    sys.stdout = log_writer
    with _STATE_LOCK:
        _state["_phase2_retry"] = False

    sms_cfg = run_config.get("sms", {"api_key": "", "provider": "smsbower", "countries": [], "service": "dr", "operator": "any", "max_price": ar.DEFAULT_SMS_MAX_PRICE})
    key = sms_cfg.get("api_key", "")
    provider = sms_cfg.get("provider", "smsbower")
    sms = UnifiedSMS(provider=provider, api_key=key)
    try:
        _log(f"短信余额: {sms.balance()}", "info")
    except Exception:
        pass

    proxy = run_config.get("proxy", "")
    if proxy:
        _log(f"代理: {proxy}", "info")
    else:
        _log("代理: 未配置(直连)", "warn")

    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    is_multi = concurrency > 1
    start_msg = f"start registration: target={count} retries={retries}"
    if is_multi:
        start_msg += f", 并发 {concurrency} 线程"
    _log(start_msg, "success")

    sub = run_config.get("sub2api", {})
    upload_target = str(run_config.get("upload_target") or "sub2api").lower()
    cpa = run_config.get("cpa", {})
    phase2_enabled = not run_config.get("no_phase2", False)
    if upload_target == "cpa":
        phase2_configured = bool(cpa.get("api_url") and cpa.get("management_key"))
    else:
        phase2_configured = bool(sub.get("url") and sub.get("email"))
    bind_email = run_config.get("bind_email", "")
    email_provider = run_config.get("email_provider", "")
    mm_config = run_config.get("mailmanage", {})
    mm = None
    provider_lock = threading.Lock()
    debug_mode = run_config.get("debug_mode", False) and not is_multi
    use_outlook = run_config.get("mail_provider") == "outlook" or email_provider == "outlook"

    if email_provider == "mailmanage" and mm_config.get("api_key"):
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
        except Exception as e:
            _log(f"MailManage 初始化失败: {e}", "error")

    # 统一初始化邮箱提供商
    provider_name, provider_instance, use_outlook = _get_email_provider(run_config, _log)
    if provider_name == "mailmanage":
        mm = provider_instance
    elif provider_name == "icloud":
        ic = provider_instance

    # ── Pre-check: verify email pool has enough available accounts ──
    if phase2_enabled and phase2_configured:
        if not bind_email:
            email_check_ok = False
            available_count = 0
            if mm is not None:
                try:
                    mailboxes = mm.list_mailboxes(category=mm_config.get("category", "free"), status="free")
                    available_count = len(mailboxes)
                    if available_count >= count:
                        _log(f"邮箱池检查: MailManage 有 {available_count} 个可用邮箱 >= 目标 {count} ✓", "info")
                        email_check_ok = True
                    elif available_count > 0:
                        _log(f"邮箱池检查: MailManage 仅有 {available_count} 个可用邮箱 < 目标 {count}，可用邮箱不足请补充邮箱", "error")
                    else:
                        _log(f"邮箱池检查: MailManage 没有可用邮箱 (category={mm_config.get('category','free')})", "error")
                except Exception as e:
                    _log(f"邮箱池检查失败: MailManage {e}", "error")
            elif use_outlook:
                outlook_pool = run_config.get("outlook_pool") or "outlook.txt"
                outlook_used = run_config.get("outlook_used") or "outlook_used.txt"
                try:
                    from outlook_mail import load_outlook_accounts, _used_emails, _repo_path
                    accounts = load_outlook_accounts(outlook_pool)
                    used_file = _repo_path(outlook_used)
                    used = _used_emails(used_file)
                    available = [a for a in accounts if a.email.lower() not in used]
                    available_count = len(available)
                    if available_count >= count:
                        _log(f"邮箱池检查: Outlook 有 {available_count}/{len(accounts)} 个未使用 >= 目标 {count} ✓", "info")
                        email_check_ok = True
                    elif available_count > 0:
                        _log(f"邮箱池检查: Outlook 仅有 {available_count} 个可用邮箱 < 目标 {count}，可用邮箱不足请补充邮箱", "error")
                    else:
                        _log(f"邮箱池检查: Outlook 所有 {len(accounts)} 个账号都已使用，可用邮箱不足请补充邮箱", "error")
                except Exception as e:
                    _log(f"邮箱池检查失败: Outlook {e}", "error")
            elif ic is not None:
                # iCloud aliases can be created on the fly, no pool check needed
                email_check_ok = True

            if not email_check_ok:
                _log("可用邮箱不足，请补充邮箱后重新启动程序", "error")
                with _STATE_LOCK:
                    _state["running"] = False
                return

    max_attempts = count * 15
    condition = threading.Condition()
    counters = {"ok": 0, "attempt": 0, "active": 0}
    countries_list = sms_cfg.get("countries", [])
    if not countries_list:
        _log("请先在配置中填写国家/地区 ID（多个用逗号分隔）", "error")
        with _STATE_LOCK:
            _state["running"] = False
        return

    # Sort countries by price if checkbox is enabled
    sort_by_price = config.get("sms_sort_by_price", False) or config.get("sms_sort_by_price") == "1"
    if sort_by_price and len(countries_list) > 1:
        try:
            sorted_info = sms.get_sorted_countries_by_price(countries_list, service=sms_cfg.get("service", "dr"))
            countries_list = [item["country"] for item in sorted_info]
            price_str = " → ".join(f"{item['country']}(${item['price']:.4f})" for item in sorted_info)
            _log(f"[拿手机号] 按价格排序: {price_str}", "info")
        except Exception as e:
            _log(f"[拿手机号] 价格排序失败，使用原始顺序: {e}", "warn")


    def claim_attempt():
        with condition:
            while True:
                if _state["stop"] or counters["ok"] >= count or counters["attempt"] >= max_attempts:
                    return None
                if counters["ok"] + counters["active"] < count:
                    counters["attempt"] += 1
                    counters["active"] += 1
                    return counters["attempt"], counters["ok"]
                condition.wait(timeout=0.5)

    def finish_registration(success):
        with condition:
            counters["active"] = max(0, counters["active"] - 1)
            if success:
                counters["ok"] += 1
            condition.notify_all()

    def _get_next_country(attempt_num):
        return _country_order_for_attempt(countries_list, attempt_num - 1, concurrency)

    def reserve_phase2_email(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        if mm is not None:
            try:
                with provider_lock:
                    email_value = mm.get_available_email(category=mm_config.get("category", "free"))
                tlog(f"{tag} MailManage 閫夊畾: {email_value}", "success")
                return email_value
            except Exception as e:
                tlog(f"{tag} MailManage 获取邮箱失败: {e}", "error")
                return ""
        if use_outlook:
            try:
                from outlook_mail import reserve_next_outlook
                with provider_lock:
                    outlook_account = reserve_next_outlook(
                        run_config.get("outlook_pool") or "outlook.txt",
                        run_config.get("outlook_used") or "outlook_used.txt",
                    )
                tlog(f"{tag} Outlook 邮箱: {outlook_account.email}", "success")
                return outlook_account.email
            except Exception as e:
                tlog(f"{tag} Outlook 获取邮箱失败: {e}", "error")
                return ""
        if ic is not None:
            try:
                with provider_lock:
                    email_value = ic.reuse_or_create_alias()
                tlog(f"{tag} 鏂?iCloud 别名: {email_value}", "success")
                return email_value
            except Exception as e:
                tlog(f"{tag} iCloud 别名失败: {e}", "error")
                return ""
        return ""

    def _worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        thread_sms = UnifiedSMS(provider=provider, api_key=key)
        log_writer.bind_thread(thread_id)
        try:
            while True:
                claimed = claim_attempt()
                if not claimed:
                    return
                attempt_num, ok_so_far = claimed
                tlog(f"{tag} attempt {attempt_num} [{ok_so_far}/{count}]", "info")
                thread_cfg = copy.deepcopy(run_config)
                thread_cfg["sms"]["countries"] = _get_next_country(attempt_num)
                try:
                    result = ar.register_one(
                        thread_sms,
                        thread_cfg,
                        verbose=True,
                        step_retries=retries,
                        create_account_max_retries=20,
                        max_price=sms_cfg.get("max_price", ""),
                        no_phase2=run_config.get("no_phase2", False),
                        stop_requested=_stop_requested,
                    )
                except ar.StopRequested:
                    tlog(f"{tag} 已停止等待手机号", "warn")
                    finish_registration(False)
                    return
                except Exception as e:
                    result = {"ok": False, "phone": "?", "error": str(e)}

                if not result.get("ok") and thread_sms.activation_id:
                    try:
                        thread_sms.cancel()
                    except Exception:
                        pass

                phone_ok = result.get("phone_ok", False)
                final_ok = result.get("final_ok", False)
                status = result.get("status", "register_failed")
                failure_stage = result.get("failure_stage", "")
                retryable = result.get("retryable", False)

                _record_result(result)

                # Log stage status
                if final_ok:
                    tlog(f"{tag} 成功: {result.get('phone','?')}  最终状态: final_ok", "success")
                elif phone_ok:
                    tlog(f"{tag} 手机号成功: {result.get('phone','?')}  状态: {status}  可补跑: {retryable}", "warn")
                else:
                    tlog(f"{tag} 失败: {result.get('phone','?')} {result.get('error','')}  阶段: {failure_stage}", "error")

                # Count based on final_ok (or phone_ok if no_phase2)
                counted = False
                if final_ok:
                    finish_registration(True)
                    _record_stat(True)
                    counted = True
                    ok_num = counters["ok"]
                    sys.stdout.flush()
                    tlog(f"{tag} 注册成功: {result['phone']} [{ok_num}/{count}]", "success")
                elif phone_ok and retryable and not (phase2_enabled and phase2_configured):
                    finish_registration(True)
                    _record_stat(False)
                    counted = True
                    ok_num = counters["ok"]
                    sys.stdout.flush()
                    tlog(f"{tag} 手机号成功 (待Phase2): {result['phone']} [{ok_num}/{count}]", "warn")
                elif not phone_ok:
                    finish_registration(False)
                    _record_stat(False)
                    continue

                if debug_mode:
                    tlog("=" * 40, "warn")
                    tlog("DEBUG - 注册完成，已暂停", "warn")
                    tlog(f"Phone: {result['phone']}", "success")
                    # Do NOT log password or session_token in debug mode
                    tlog(f"Password: ***redacted***", "warn")
                    tlog(f"Session Token: ***redacted***", "warn")
                    tlog(f"阶段状态: phone_ok={result.get('phone_ok',False)} final_ok={result.get('final_ok',False)}", "info")
                    tlog("=" * 40, "warn")
                    _state["_paused"] = True
                    try:
                        _state["pause_queue"].get(timeout=300)
                    except queue.Empty:
                        tlog("DEBUG - timed out, auto continue", "warn")
                    _state["_paused"] = False

                phase2_ok = True
                if (
                    phase2_enabled
                    and phase2_configured
                    and result.get("session_token")
                ):
                    phase2_ok = False
                    phase2_max_retries = 5
                    phase2_retries = 0

                    while not phase2_ok and not _state["stop"] and phase2_retries < phase2_max_retries:
                        phase2_retries += 1
                        if phase2_retries > 1 or not thread_cfg.get("bind_email"):
                            if not bind_email:
                                new_email = reserve_phase2_email(thread_id)
                                if new_email:
                                    thread_cfg["bind_email"] = new_email
                                    tlog(f"{tag} 获取到邮箱: {new_email} (Phase2 重试 #{phase2_retries})", "success")
                                elif not thread_cfg.get("bind_email"):
                                    tlog(f"{tag} 无可用邮箱，跳过 Phase 2，继续使用此手机号", "warn")
                                    phase2_ok = True  # Mark as OK so we don't keep retrying without email
                                    break

                        tlog(
                            f"{tag} === Phase 2: OAuth + 绑邮箱 + 上传 (邮箱: {thread_cfg.get('bind_email', '?')}) 重试 {phase2_retries}/{phase2_max_retries} ===",
                            "info",
                        )
                        phase2_ok = False

                        try:
                            oauth_result = _phase2_for_result(result, thread_cfg, tag, thread_id=thread_id)
                        except Exception as e:
                            oauth_result = {"ok": False, "error": str(e)}

                        if oauth_result.get("ok"):
                            phase2_ok = True
                            result["bind_email"] = thread_cfg.get("bind_email", "")
                            result["email_bound"] = True
                            result["uploaded"] = bool(oauth_result.get("uploaded", True))
                            result["upload_verified"] = bool(oauth_result.get("upload_verified", result["uploaded"]))
                            result["needs_retry"] = bool(oauth_result.get("needs_retry", not result["upload_verified"]))
                            result["upload_target"] = oauth_result.get("upload_target", thread_cfg.get("upload_target", "sub2api"))
                            if oauth_result.get("sub2api_account_id"):
                                result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
                            if oauth_result.get("upload_error"):
                                result["upload_error"] = oauth_result.get("upload_error", "")
                            if oauth_result.get("failed_upload_file"):
                                result["failed_upload_file"] = oauth_result.get("failed_upload_file", "")
                            result["final_ok"] = bool(result["uploaded"] and result.get("upload_verified", False))
                            result["ok"] = True
                            result["status"] = "final_ok" if result["final_ok"] else "upload_unverified"
                            if result["uploaded"]:
                                if result["upload_target"] == "cpa":
                                    tlog(f"{tag}   [4/4] CPA 上传成功 ({oauth_result.get('upload_method', 'unknown')})", "success")
                                else:
                                    aid = oauth_result.get("sub2api_account_id", "?")
                                    tlog(f"{tag}   [4/4] 上传成功: SUB2API id={aid}", "success")
                            else:
                                tlog(f"{tag}   [4/4] 账号生成成功，但上传失败: {result.get('upload_error', '?')} 文件={result.get('failed_upload_file', '-')}", "warn")
                            if run_config.get("mail_provider") == "outlook" or run_config.get("email_provider") == "outlook":
                                try:
                                    outlook_status = "success" if result.get("final_ok") else "verify_failed"
                                    mark_outlook_status(thread_cfg.get("bind_email", ""), outlook_status, run_config.get("outlook_used") or "outlook_used.txt")
                                except Exception as e:
                                    tlog(f"{tag} Outlook 状态更新失败: {e}", "warn")
                        else:
                            if run_config.get("mail_provider") == "outlook" or run_config.get("email_provider") == "outlook":
                                try:
                                    mark_outlook_status(thread_cfg.get("bind_email", ""), _phase2_error_status(oauth_result.get("error", "")), run_config.get("outlook_used") or "outlook_used.txt")
                                except Exception as e:
                                    tlog(f"{tag} Outlook 状态更新失败: {e}", "warn")
                            error_msg = oauth_result.get('error', '?')
                            from openai_bind_email import classify_error
                            error_type = classify_error(error_msg)
                            if error_type == "permanent":
                                tlog(f"{tag}   [4/4] Phase 2 永久失败 (不重试): {error_msg}", "error")
                                break
                            elif error_type == "temporary":
                                tlog(f"{tag}   [4/4] Phase 2 临时失败 (将重试): {error_msg}", "warn")
                            else:
                                tlog(f"{tag}   [4/4] Phase 2 失败: {error_msg} (重试 {phase2_retries}/{phase2_max_retries})", "warn")
                            if phase2_retries >= phase2_max_retries:
                                tlog(f"{tag}   Phase2 已达最大重试次数 {phase2_max_retries}，停止使用此手机号", "error")
                                break
                            tlog(f"{tag}   将获取新邮箱自动重试...", "warn")
                            continue

                _save_result(results_dir, result, thread_cfg)
                if not counted:
                    finish_registration(False)
                    _record_stat(bool(result.get("final_ok")))
                if mm is not None and thread_cfg.get("bind_email") and phase2_ok:
                    try:
                        with provider_lock:
                            mm.mark_used(thread_cfg["bind_email"])
                        tlog(f"{tag} MailManage 已标记: {thread_cfg['bind_email']}", "info")
                    except Exception:
                        pass
        finally:
            log_writer.unbind_thread()

    try:
        threads = []
        for i in range(concurrency):
            thread = threading.Thread(target=_worker, args=(i + 1,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
    finally:
        sys.stdout = old_stdout

    with _STATE_LOCK:
        _state["running"] = False
    _log(f"完成: {counters['ok']}/{count}", "success" if counters["ok"] >= count else "warn")


def _find_pending_phase2_items() -> list:
    """扫描 results/ 目录，返回需要补跑 Phase2 的文件名列表"""
    results_dir = Path(__file__).parent / "results"
    if not results_dir.exists():
        return []
    pending = []
    for f in sorted(results_dir.iterdir(), key=lambda x: x.name):
        if f.suffix != ".json" or f.name == "_all.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("phone_ok") and not _result_upload_complete(data):
            pending.append(f.name)
    return pending


def _auto_retry_phase2_on_startup():
    """启动时自动补跑未完成的 Phase2"""
    cfg = _state.get("config", {})
    if not cfg.get("phase2_auto_skip", False):
        _log("[自动补跑] 已关闭，跳过历史 Phase2 补跑", "info")
        return
    # 检查 Phase2 是否已配置（需要邮箱提供商）
    email_provider = cfg.get("email_provider", "")
    mm_config = cfg.get("mailmanage", {})
    use_outlook = cfg.get("mail_provider") == "outlook" or email_provider == "outlook"
    bind_email = cfg.get("bind_email", "")
    has_provider = (
        bind_email
        or (email_provider == "mailmanage" and mm_config.get("api_key"))
        or use_outlook
    )
    if not has_provider:
        icloud_cookies = _load_phase2_icloud_cookies(cfg)
        if not icloud_cookies:
            return  # 没有邮箱提供商，跳过自动补跑

    pending = _find_pending_phase2_items()
    if not pending:
        return

    _log(f"[自动补跑] 发现 {len(pending)} 个待补跑 Phase2 的历史账号，自动启动补跑 ...", "info")
    threading.Thread(
        target=_run_batch_phase2,
        args=(pending, cfg, "", "files", 1),
        daemon=True,
    ).start()


def start_gui(host="0.0.0.0", port=7778):
    _auto_retry_phase2_on_startup()
    print(f"http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)



_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT Auto Register</title>
<style>
/* 鈹€鈹€ RigsHub Design System 鈹€鈹€ */
:root{color-scheme:light;--paper:#f3efe4;--paper-dim:#e8e2d4;--ink:#0f0e0c;--ink-soft:#5c564e;--ink-faint:#9a938a;--rule:rgba(15,14,12,0.12);--rule-strong:rgba(15,14,12,0.22);--red:#b7392d;--green:#1f8b4c;--mono:"Geist Mono","SFMono-Regular",Consolas,monospace;--serif:Newsreader,"Iowan Old Style",Georgia,serif;--sans:"Noto Sans SC","Geist","PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-family:var(--sans);font-synthesis:none;text-rendering:geometricPrecision;-webkit-font-smoothing:antialiased}
*{box-sizing:border-box;margin:0;padding:0}
html{min-width:900px;height:100%;background:var(--paper)}
body{height:100%;min-height:100vh;overflow:hidden;color:var(--ink);font-size:14px;background:radial-gradient(circle at 20% 14%,rgba(183,57,45,0.035),transparent 28%),radial-gradient(circle at 74% 48%,rgba(15,14,12,0.035),transparent 34%),linear-gradient(90deg,rgba(15,14,12,0.025) 1px,transparent 1px),linear-gradient(rgba(15,14,12,0.025) 1px,transparent 1px),var(--paper);background-size:auto,auto,72px 72px,72px 72px,auto}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:0.28;background-image:radial-gradient(circle,rgba(15,14,12,0.16) 0 0.55px,transparent 0.7px),radial-gradient(circle,rgba(183,57,45,0.12) 0 0.45px,transparent 0.65px);background-size:5px 5px,11px 11px;mix-blend-mode:multiply}
button{cursor:pointer;color:inherit;font:inherit}
input,select,textarea{font:inherit}
.manuscript{position:relative;display:grid;grid-template-rows:auto minmax(0,1fr);height:100vh;min-height:0;overflow:hidden;padding:28px 48px 24px}
.nav{display:flex;align-items:center;gap:24px;width:100%;margin:0 auto;padding-bottom:14px;border-bottom:2px solid var(--red)}
.brand{display:inline-flex;align-items:center;gap:10px;min-width:300px;color:inherit;background:none;border:0;cursor:pointer}
.brand-mark{font-size:22px}
.brand-name{font-family:var(--serif);font-size:26px;letter-spacing:0}
.brand-meta{color:var(--ink-faint);font-family:var(--mono);font-size:11px;letter-spacing:0.28em;text-transform:uppercase}
#status-msg{min-width:52px;letter-spacing:0;text-transform:none;font-family:var(--sans);font-size:13px;color:var(--ink-soft)}
.nav-links{display:flex;align-items:center;justify-content:flex-end;gap:24px;width:100%;color:var(--ink-soft);font-size:13px}
.nav-action{background:none;border:0;padding:4px 8px;font-size:13px;color:var(--ink-soft)}
.nav-action.active{color:var(--ink);border-bottom:1px solid var(--ink)}
.nav-action:hover{color:var(--ink)}
.corner{position:fixed;width:22px;height:22px;pointer-events:none;opacity:0.3}
.corner::before,.corner::after{content:"";position:absolute;background:var(--ink-faint)}
.corner::before{top:10px;left:0;width:22px;height:1px}
.corner::after{top:0;left:10px;width:1px;height:22px}
.corner-tl{top:22px;left:22px}.corner-tr{top:22px;right:22px}
.corner-bl{bottom:22px;left:22px}.corner-br{bottom:22px;right:22px}
.content{overflow-y:auto;overflow-x:hidden;padding:24px 0;max-width:1400px;width:100%;margin:0 auto}
.stats{display:flex;gap:16px;margin-bottom:20px}
.stat{flex:1;padding:20px;background:rgba(255,255,255,0.55);border:1px solid var(--rule);text-align:center}
.stat .num{font-size:32px;font-family:var(--serif);color:var(--ink)}
.stat .lbl{font-size:11px;color:var(--ink-faint);margin-top:4px;text-transform:uppercase;letter-spacing:0.12em}
.row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.col{flex:1;min-width:340px}
.card{background:rgba(255,255,255,0.55);border:1px solid var(--rule);padding:20px;margin-bottom:16px}
.card h2{font-family:var(--serif);font-size:18px;color:var(--ink);margin-bottom:14px;font-weight:500}
label{display:block;font-size:11px;color:var(--ink-faint);margin-top:10px;text-transform:uppercase;letter-spacing:0.08em}
input,select,textarea{width:100%;padding:8px 10px;margin:3px 0;background:rgba(255,255,255,0.7);border:1px solid var(--rule);color:var(--ink);font-size:13px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--ink-faint)}
.btn-primary{padding:8px 20px;background:var(--ink);color:var(--paper);border:none;font-size:13px;cursor:pointer}
.btn-primary:hover:not(:disabled){background:var(--ink-soft)}
.btn-danger{padding:8px 20px;background:var(--red);color:#fff;border:none;font-size:13px;cursor:pointer}
.btn-danger:hover:not(:disabled){opacity:0.85}
.btn-neutral{padding:6px 14px;background:transparent;color:var(--ink-soft);border:1px solid var(--rule);font-size:12px;cursor:pointer}
.btn-neutral:hover:not(:disabled){border-color:var(--ink-faint);color:var(--ink)}
button:disabled{opacity:0.4;cursor:not-allowed}
.log{background:var(--ink);color:#d4d4d4;padding:16px;max-height:450px;overflow-y:auto;font:12px/1.6 var(--mono)}
.log .info{color:#6a9fd8}.log .success{color:#4ec9b0}
.log .error{color:#f44747}.log .warn{color:#ce9178}
.log .time{color:#666;margin-right:8px}
.log-tabs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.log-tab.active{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.log-toolbar{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-faint);margin-top:8px}
.toast{position:fixed;top:16px;right:16px;padding:10px 20px;font-size:13px;z-index:999;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
.toast-ok{background:#c8e6c9;color:var(--green)}
.toast-err{background:#ffcdd2;color:var(--red)}
.floating-panel{display:none;position:fixed;bottom:16px;right:16px;padding:12px 16px;background:rgba(255,255,255,0.95);border:1px solid var(--rule-strong);z-index:99}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--rule);border-top-color:var(--ink);border-radius:50%;animation:s .6s linear infinite;margin-right:4px}
@keyframes s{to{transform:rotate(360deg)}}
.worker-status{font-size:11px;color:var(--ink-faint);margin-top:8px;text-align:center}
.view{display:block}
.outlook-pool-shell{display:grid;grid-template-columns:minmax(340px,420px) minmax(0,1fr);gap:16px;align-items:start}
.outlook-pool-current{font-size:12px;color:var(--ink-soft);margin-bottom:12px}
.outlook-pool-import{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:12px;align-items:start}
.outlook-pool-import textarea{min-height:112px;resize:vertical;font:12px/1.6 var(--mono)}
.outlook-pool-import-actions{display:flex;flex-direction:column;gap:8px}
.outlook-pool-import-hint{margin-bottom:10px;font-size:12px;color:var(--ink-faint)}
.outlook-pool-file-name{font-size:11px;color:var(--ink-faint);word-break:break-all}
.outlook-pool-list{border:1px solid var(--rule);background:rgba(255,255,255,0.35);max-height:640px;overflow-y:auto}
.outlook-pool-row{display:block;width:100%;padding:12px 14px;border:0;border-bottom:1px solid var(--rule);background:none;text-align:left}
.outlook-pool-row:hover{background:rgba(15,14,12,0.04)}
.outlook-pool-row.active{background:rgba(15,14,12,0.08)}
.outlook-pool-row:last-child{border-bottom:0}
.outlook-pool-row .title{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
.outlook-pool-row .meta{margin-top:6px;font-size:11px;color:var(--ink-faint);display:flex;flex-wrap:wrap;gap:10px}
.outlook-pool-pill{display:inline-flex;align-items:center;padding:2px 8px;border:1px solid var(--rule);font-size:11px;color:var(--ink-soft);background:rgba(255,255,255,0.4)}
.outlook-pool-pager{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12px;color:var(--ink-faint)}
.outlook-pool-empty{padding:24px;color:var(--ink-faint);text-align:center}
.outlook-pool-detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.outlook-pool-detail-item{padding:10px 12px;border:1px solid var(--rule);background:rgba(255,255,255,0.35)}
.outlook-pool-detail-item .k{display:block;font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px}
.outlook-pool-detail-item .v{font-size:13px;color:var(--ink);word-break:break-all}
.outlook-pool-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.outlook-pool-messages{display:grid;grid-template-columns:minmax(280px,360px) minmax(0,1fr);gap:12px}
.outlook-pool-mail-list{border:1px solid var(--rule);background:rgba(255,255,255,0.35);max-height:420px;overflow-y:auto}
.outlook-pool-mail-row{display:block;width:100%;padding:10px 12px;border:0;border-bottom:1px solid var(--rule);background:none;text-align:left}
.outlook-pool-mail-row:hover{background:rgba(15,14,12,0.04)}
.outlook-pool-mail-row.active{background:rgba(15,14,12,0.08)}
.outlook-pool-mail-row:last-child{border-bottom:0}
.outlook-pool-mail-row .subj{font-size:13px;color:var(--ink)}
.outlook-pool-mail-row .meta{margin-top:4px;font-size:11px;color:var(--ink-faint)}
.outlook-pool-mail-body{min-height:320px;max-height:420px;overflow:auto;padding:12px;border:1px solid var(--rule);background:rgba(255,255,255,0.35);white-space:pre-wrap;font:12px/1.7 var(--mono);color:var(--ink)}

</style></head><body>
<div class="manuscript">
  <span class="corner corner-tl"></span><span class="corner corner-tr"></span>
  <span class="corner corner-bl"></span><span class="corner corner-br"></span>

  <header class="nav">
    <button class="brand" type="button">
      <span class="brand-mark">&#9881;</span>
      <span class="brand-name">ChatGPT Register</span>
    </button>
    <span class="brand-meta" id="status-msg">就绪</span>
    <nav class="nav-links">
      <button class="nav-action" onclick="downloadResults()">下载结果</button>
      <span class="nav-divider"></span>
      <span class="nav-action" id="balance">-</span>
      <span class="brand-meta">SMSBower</span>
    </nav>
  </header>

  <div class="content">
    <div class="stats">
      <div class="stat"><span class="num" id="ok-count">0</span><span class="lbl">本次成功</span></div>
      <div class="stat"><span class="num" id="fail-count">0</span><span class="lbl">本次失败</span></div>
      <div class="stat"><span class="num" id="total-ok-count">0</span><span class="lbl">累计成功</span></div>
      <div class="stat"><span class="num" id="total-fail-count">0</span><span class="lbl">累计失败</span></div>
    </div>

    <div class="row">
      <div class="col">
        <div class="card"><h2>注册配置</h2>
          <label>短信平台</label>
          <select id="sms_provider" style="width:100%;padding:8px 10px;background:#fdf8f0;border:1px solid #d4b896;border-radius:4px;color:#4a3728;font-size:13px">
            <option value="smsbower">SMSBower</option>
            <option value="hero-sms">Hero-SMS</option>
          </select>
          <label>API Key</label><input id="api_key" placeholder="your-sms-api-key">
          <label>代理</label><input id="proxy" placeholder="socks5h://127.0.0.1:10808">
          <label>国家/地区 ID（按所选短信平台，多个用逗号分隔）</label><input id="countries" placeholder="例如 4,16（从配置读取，不使用默认国家）">
          <label style="display:flex;align-items:center;gap:6px;font-size:13px">
            <input type="checkbox" id="sms_sort_by_price" style="width:auto;margin:0">
            按最低价格自动排序（优先使用便宜的国家）
          </label>
          <label>最高价格</label><input id="max_price" value="0.03" placeholder="默认 0.03">
          <label>密码</label><input id="password" placeholder="留空=随机">
          <div class="row" style="margin-top:10px">
            <div style="flex:1"><label>目标数量</label><input id="count" value="1" type="number" min="1" max="99"></div>
            <div style="flex:1"><label>并发线程</label><input id="concurrency" value="1" type="number" min="1" max="10"></div>
            <div style="flex:1"><label>步骤重试</label><input id="retries" value="2" type="number" min="0" max="10"></div>
          </div>
          <div style="margin-top:12px;display:flex;gap:8px">
            <button class="btn-primary" id="btn-start" onclick="startReg()" style="flex:1">开始注册</button>
            <button class="btn-neutral" id="btn-phase2" onclick="openBatchPanel()" style="flex:1">Phase 2 补跑</button>
            <button class="btn-neutral" onclick="openFailedUploadsPanel()" style="flex:1">失败上传补传</button>
            <button class="btn-danger" id="btn-stop" onclick="stopReg()" disabled>停止</button>
          </div>
          <div class="worker-status" id="worker-status"></div>
        </div>
      </div>
      <div class="col">
        <div class="card"><h2>Phase 2: 邮箱 &amp; 上传</h2>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;text-transform:none;letter-spacing:0;margin-top:0">
            <input type="checkbox" id="no_phase2" style="width:auto;margin:0"> 不跑 Phase 2
          </label>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;text-transform:none;letter-spacing:0">
            <input type="checkbox" id="phase2_auto_skip" style="width:auto;margin:0"> 启动时自动补跑历史 Phase 2
          </label>
          <label>邮箱提供方</label>
          <select id="email_provider" onchange="toggleEmailProviderFields()">
            <option value="">iCloud</option>
            <option value="mailmanage">MailManage</option>
            <option value="outlook">Outlook</option>
          </select>
          <div id="mm-group" style="display:none">
            <label>MailManage Key</label><input id="mailmanage_key">
            <label>分类</label><input id="mailmanage_category" value="safe">
            <label>关键词</label><input id="mailmanage_keyword" value="gpt">
          </div>
          <div id="outlook-group" style="display:none">
            <label>Outlook 账号池</label>
            <textarea id="outlook_pool" rows="6" style="font-family:var(--mono);font-size:11px" placeholder="email----password----client_id----refresh_token"></textarea>
            <div style="font-size:11px;color:var(--ink-faint);line-height:1.5;margin-top:4px">Outlook 池是长期凭据库：已用邮箱不要删除，只追加新邮箱；重复分配由 outlook_used 状态控制。</div>
          </div>
          <div id="icloud-group">
            <label>iCloud 邮箱 (IMAP)</label><input id="imap_user" placeholder="xxx@icloud.com">
            <label>Apple 专用密码</label><input id="imap_pass" type="password">
          </div>
          <label>上传平台</label>
          <select id="upload_target" onchange="toggleUploadTargetFields()">
            <option value="sub2api">SUB2API</option>
            <option value="cpa">CPA</option>
          </select>
          <div id="sub2api-group">
          <label>SUB2API 地址</label><input id="sub2api_url">
          <label>管理邮箱</label><input id="sub2api_email">
          <label>管理密码</label><input id="sub2api_pwd" type="password">
          <label>目标分组</label><input id="sub2api_group" value="CHATGPT">
          </div>
          <div id="cpa-group" style="display:none">
            <label>CPA 管理地址</label><input id="cpa_management_url" placeholder="http://47.89.129.103:18317">
            <label>CPA API 地址</label><input id="cpa_api_url" placeholder="http://47.89.129.103:8317">
            <label>CPA 管理密钥</label><input id="cpa_management_key" type="password">
            <label>CPA 上传模式</label><input id="cpa_upload_mode" value="auto" readonly>
          </div>
          <label>绑定邮箱</label><input id="bind_email">
          <label>iCloud Cookies 路径</label><input id="icloud_cookies" placeholder="cookies.json">
        </div>
      </div>
    </div>

    <div class="card"><h2>Plus 升级</h2>
      <label>支付方式</label>
      <select id="plus_method" onchange="togglePlusFields()">
        <option value="paypal">PayPal 协议线路 (纯协议)</option>
        <option value="gopay">GoPay (印尼手机号 + PIN)</option>
      </select>
      <div id="plus-paypal-group">
        <label>PayPal 邮箱 (用于注册)</label><input id="plus_email" placeholder="your@email.com">
      </div>
      <div id="plus-gopay-group" style="display:none">
        <label>GoPay 手机号</label><input id="plus_phone" placeholder="+6281234567890">
        <label>GoPay PIN</label><input id="plus_pin" type="password" placeholder="6 digits">
      </div>
      <label>国家</label><input id="plus_country" value="ID">
      <label>货币</label><input id="plus_currency" value="IDR">
      <div style="margin-top:10px">
        <button class="btn-primary" onclick="upgradePlus()" style="width:100%">开通 Plus</button>
      </div>
    </div>

    <div class="card"><h2>iCloud Cookies 导入</h2>
      <textarea id="cookies_input" rows="5" style="font-family:var(--mono);font-size:11px" placeholder='[{"name":"X-APPLE-WEB...", ...}]'></textarea>
      <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
        <button class="btn-neutral" onclick="importCookies()">导入 Cookies</button>
        <span id="cookies_status" style="font-size:11px;color:var(--ink-faint)"></span>
      </div>
    </div>

    <div class="card"><h2>运行日志</h2>
      <div class="log-toolbar">
        <label style="display:flex;align-items:center;gap:4px;cursor:pointer;margin:0;text-transform:none;letter-spacing:0;font-size:12px">
          <input type="checkbox" id="auto-scroll" checked style="width:auto;margin:0"> 自动滚动
        </label>
        <span style="flex:1"></span>
        <button class="btn-neutral" onclick="clearLog()">清空</button>
      </div>
      <div class="log-tabs" id="log-tabs">
        <button class="btn-neutral log-tab active" id="log-tab-all" type="button" onclick="setActiveLogTab('all')">全部</button>
      </div>
      <div class="log" id="log" style="margin-top:6px"><span class="info">等待启动...</span></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<div id="code-panel" class="floating-panel" style="align-items:center;gap:8px">
  <span id="code-hint" style="color:var(--red);font-size:13px">验证码</span>
  <input id="bind-code-input" placeholder="6 digits" maxlength="6" style="width:120px;padding:4px 8px;font-size:14px">
  <button class="btn-primary" onclick="submitCode()" style="padding:4px 12px">提交</button>
</div>
<div id="pause-panel" class="floating-panel" style="align-items:center;gap:8px">
  <span id="pause-msg" style="color:var(--green);font-size:13px">暂停中</span>
  <button class="btn-primary" onclick="doContinue()" style="padding:4px 12px">继续</button>
  <button class="btn-danger" id="btn-skip-phase2" onclick="doSkipPhase2()" style="padding:4px 12px;display:none">跳过</button>
</div>
<div id="batch-panel" class="floating-panel" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:480px;max-height:70vh;background:#fff;border:1px solid var(--rule-strong);z-index:1000;flex-direction:column;padding:16px;border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,0.15)">
  <h3 style="margin:0 0 12px;font-size:16px">Phase 2 补跑</h3>
  <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;font-size:12px">
    <label style="margin:0">邮箱</label>
    <input id="batch-email" placeholder="留空=自动获取" style="flex:1;padding:4px 8px;font-size:12px">
    <select id="batch-source" style="padding:4px;font-size:12px"><option value="files">results目录</option></select>
  </div>
  <div id="batch-list" style="flex:1;overflow-y:auto;border:1px solid var(--rule);padding:4px;border-radius:4px;max-height:40vh"></div>
  <div id="batch-summary" style="font-size:11px;color:var(--ink-faint);margin-top:8px">0 个待处理</div>
  <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
    <label style="margin:0;font-size:12px"><input type="checkbox" id="batch-select-all" checked style="width:auto;margin:0" onchange="toggleSelectAll()"> 全选</label>
    <span style="flex:1"></span>
    <button class="btn-danger" id="btn-batch-delete" onclick="deleteSelectedBatchItems()" style="padding:4px 12px">删除所选</button>
    <button class="btn-neutral" onclick="closeBatchPanel()" style="padding:4px 12px">关闭</button>
    <button class="btn-primary" id="btn-batch-start" onclick="startBatchPhase2()" style="padding:4px 12px">开始补跑</button>
    <span id="batch-running" style="display:none;font-size:11px;color:var(--ink-soft)">运行中...</span>
  </div>
</div>
<div id="failed-upload-panel" class="floating-panel" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:520px;max-height:70vh;background:#fff;border:1px solid var(--rule-strong);z-index:1000;flex-direction:column;padding:16px;border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,0.15)">
  <h3 style="margin:0 0 12px;font-size:16px">失败上传补传</h3>
  <div id="failed-upload-list" style="flex:1;overflow-y:auto;border:1px solid var(--rule);padding:4px;border-radius:4px;max-height:40vh"></div>
  <div id="failed-upload-summary" style="font-size:11px;color:var(--ink-faint);margin-top:8px">0 个待补传</div>
  <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
    <label style="margin:0;font-size:12px"><input type="checkbox" id="failed-upload-select-all" checked style="width:auto;margin:0" onchange="toggleFailedUploadSelectAll()"> 全选</label>
    <span style="flex:1"></span>
    <button class="btn-neutral" onclick="closeFailedUploadsPanel()" style="padding:4px 12px">关闭</button>
    <button class="btn-primary" id="btn-failed-upload-retry" onclick="retrySelectedFailedUploads()" style="padding:4px 12px">补传所选</button>
    <span id="failed-upload-running" style="display:none;font-size:11px;color:var(--ink-soft)">补传中...</span>
  </div>
</div>
<script>
function G(id){return document.getElementById(id);}
function toast(msg,ok){var t=G('toast');t.textContent=msg;t.className='toast '+(ok?'toast-ok':'toast-err')+' show';setTimeout(function(){t.className='toast'},2500);}

function escapeHtml(s){
  return String(s||'').replace(/[&<>"]/g,function(ch){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch];
  });
}

var currentView='main';
// bootstrap markers: id="view-main" id="view-outlook-pool" id="outlook-pool-summary" id="outlook-pool-list" id="outlook-pool-detail" id="outlook-pool-messages"
var outlookPoolState={
  loaded:false,
  page:1,
  pageSize:20,
  total:0,
  status:'all',
  q:'',
  selectedEmail:'',
  items:[],
  messages:[],
  selectedMessageIndex:0
};

function bootstrapOutlookPoolView(){
  var content=document.querySelector('.content');
  if(content && !G('view-main')){
    var mainView=document.createElement('div');
    mainView.id='view-main';
    mainView.className='view';
    while(content.firstChild){
      mainView.appendChild(content.firstChild);
    }
    content.appendChild(mainView);
  }
  if(content && !G('view-outlook-pool')){
    var outlookView=document.createElement('div');
    outlookView.id='view-outlook-pool';
    outlookView.className='view';
    outlookView.style.display='none';
    outlookView.innerHTML=[
      '<div class="card">',
      '  <h2>Outlook 池导入</h2>',
      '  <div id="outlook-pool-current-bind" class="outlook-pool-current">当前绑定邮箱: -</div>',
      '  <div class="outlook-pool-import-hint">支持上传 txt 文件、直接粘贴多行 Outlook 账号，或填入账号池文件路径。</div>',
      '  <div class="outlook-pool-import">',
      '    <textarea id="outlook-pool-editor" placeholder="email----password----client_id----refresh_token&#10;email----password----client_id----refresh_token"></textarea>',
      '    <div class="outlook-pool-import-actions">',
      '      <input id="outlook-pool-file" type="file" accept=".txt,text/plain" style="display:none" onchange="importOutlookPoolFile()">',
      '      <button class="btn-neutral" type="button" onclick="chooseOutlookPoolFile()">选择 txt 文件</button>',
      '      <div id="outlook-pool-file-name" class="outlook-pool-file-name">未选择文件</div>',
      '      <button class="btn-primary" id="outlook-pool-save" type="button" onclick="saveOutlookPoolEditor()">保存并刷新</button>',
      '      <button class="btn-neutral" type="button" onclick="syncOutlookPoolEditor()">从当前配置载入</button>',
      '    </div>',
      '  </div>',
      '  <div class="stats" id="outlook-pool-summary"></div>',
      '</div>',
      '<div class="outlook-pool-shell">',
      '  <div class="card">',
      '    <h2>Outlook 池</h2>',
      '    <div class="row" style="margin-bottom:12px">',
      '      <div style="flex:1;min-width:140px">',
      '        <label>状态</label>',
      '        <select id="outlook-pool-filter" onchange="reloadOutlookPoolList(true)">',
      '          <option value="all">全部</option>',
      '          <option value="unused">未使用</option>',
      '          <option value="reserved">已预留</option>',
      '          <option value="success">已注册成功</option>',
      '          <option value="register_failed">注册失败</option>',
      '          <option value="verify_failed">验证失败</option>',
      '          <option value="bad">坏号</option>',
      '        </select>',
      '      </div>',
      '      <div style="flex:1;min-width:180px">',
      '        <label>搜索</label>',
      '        <input id="outlook-pool-query" placeholder="邮箱 / 手机号" onkeydown="if(event.key===&quot;Enter&quot;){reloadOutlookPoolList(true);}">',
      '      </div>',
      '    </div>',
      '    <div style="display:flex;gap:8px;margin-bottom:10px">',
      '      <button class="btn-neutral" type="button" onclick="reloadOutlookPoolList(true)">筛选</button>',
      '      <button class="btn-neutral" type="button" onclick="refreshOutlookPoolPage()">刷新</button>',
      '    </div>',
      '    <div id="outlook-pool-list" class="outlook-pool-list"><div class="outlook-pool-empty">等待加载池子数据...</div></div>',
      '    <div class="outlook-pool-pager">',
      '      <button class="btn-neutral" id="outlook-pool-prev" type="button" onclick="changeOutlookPoolPage(-1)">上一页</button>',
      '      <span id="outlook-pool-page-info">第 1 页</span>',
      '      <button class="btn-neutral" id="outlook-pool-next" type="button" onclick="changeOutlookPoolPage(1)">下一页</button>',
      '    </div>',
      '  </div>',
      '  <div>',
      '    <div class="card">',
      '      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">',
      '        <h2 style="margin-bottom:0;flex:1">详情</h2>',
      '        <button class="btn-neutral" type="button" onclick="refreshSelectedOutlookMessages()">刷新邮件</button>',
      '      </div>',
      '      <div id="outlook-pool-detail"><div class="outlook-pool-empty">请选择一个 Outlook 邮箱。</div></div>',
      '    </div>',
      '    <div class="card">',
      '      <h2>最近邮件</h2>',
      '      <div id="outlook-pool-messages" class="outlook-pool-messages">',
      '        <div id="outlook-pool-message-list" class="outlook-pool-mail-list"><div class="outlook-pool-empty">选择邮箱后加载邮件。</div></div>',
      '        <div id="outlook-pool-message-body" class="outlook-pool-mail-body">暂无邮件内容。</div>',
      '      </div>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('');
    content.appendChild(outlookView);
  }
  var nav=document.querySelector('.nav-links');
  if(nav && !G('nav-main')){
    nav.insertAdjacentHTML('afterbegin','<button class="nav-action active" id="nav-main" type="button" onclick="switchView(&quot;main&quot;)">主页</button><button class="nav-action" id="nav-outlook-pool" type="button" onclick="switchView(&quot;outlook-pool&quot;)">Outlook 池</button>');
  }
  var brand=document.querySelector('.brand');
  if(brand)brand.setAttribute('onclick',"switchView('main')");
}

function switchView(view){
  currentView=(view==='outlook-pool')?'outlook-pool':'main';
  if(G('view-main'))G('view-main').style.display=(currentView==='main'?'':'none');
  if(G('view-outlook-pool'))G('view-outlook-pool').style.display=(currentView==='outlook-pool'?'':'none');
  if(G('nav-main'))G('nav-main').classList.toggle('active',currentView==='main');
  if(G('nav-outlook-pool'))G('nav-outlook-pool').classList.toggle('active',currentView==='outlook-pool');
  if(currentView==='outlook-pool' && !outlookPoolState.loaded){
    loadOutlookPool();
  }
}

function loadOutlookPool(){
  outlookPoolState.loaded=true;
  loadOutlookPoolSummary();
  loadOutlookPoolList();
  if(outlookPoolState.selectedEmail){
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
    loadOutlookPoolMessages(outlookPoolState.selectedEmail);
  }
}

function chooseOutlookPoolFile(){
  if(G('outlook-pool-file'))G('outlook-pool-file').click();
}

function syncOutlookPoolEditor(value){
  var nextValue=value;
  var fromConfig=(nextValue===undefined);
  if(nextValue===undefined){
    nextValue=G('outlook_pool')?G('outlook_pool').value:'';
  }
  nextValue=nextValue||'';
  if(G('outlook_pool'))G('outlook_pool').value=nextValue;
  if(G('outlook-pool-editor'))G('outlook-pool-editor').value=nextValue;
  if(fromConfig && G('outlook-pool-file-name'))G('outlook-pool-file-name').textContent='当前配置';
}

function importOutlookPoolFile(){
  var input=G('outlook-pool-file');
  var file=input && input.files && input.files[0];
  if(!file){
    toast('请选择 txt 文件',false);
    return;
  }
  if(G('outlook-pool-file-name'))G('outlook-pool-file-name').textContent=file.name||'已选择文件';
  var reader=new FileReader();
  reader.onload=function(){
    syncOutlookPoolEditor(typeof reader.result==='string' ? reader.result : '');
    toast('文件内容已载入编辑器',true);
  };
  reader.onerror=function(){
    toast('读取文件失败',false);
  };
  reader.readAsText(file,'utf-8');
}

function saveOutlookPoolEditor(){
  var editor=G('outlook-pool-editor');
  var raw=editor?editor.value:'';
  syncOutlookPoolEditor(raw);
  return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({outlook_pool:raw,email_provider:'outlook'})}).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'save failed');}
    if(G('email_provider'))G('email_provider').value='outlook';
    toggleEmailProviderFields();
    outlookPoolState.loaded=true;
    outlookPoolState.page=1;
    outlookPoolState.selectedEmail='';
    outlookPoolState.messages=[];
    outlookPoolState.selectedMessageIndex=0;
    renderOutlookPoolDetail(null);
    renderOutlookPoolMessages([]);
    loadOutlookPoolSummary();
    loadOutlookPoolList();
    toast('Outlook 池已保存',true);
    return j;
  }).catch(function(err){
    toast(String(err.message||err),false);
    return null;
  });
}

function refreshOutlookPoolPage(){
  loadOutlookPoolSummary();
  loadOutlookPoolList();
  if(outlookPoolState.selectedEmail){
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
  }
}

function renderOutlookPoolSummary(data){
  var counts=data.counts||{};
  var stats=[
    ['总数',data.total||0],
    ['未使用',counts.unused||0],
    ['已预留',counts.reserved||0],
    ['已注册成功',counts.success||0],
    ['注册失败',counts.register_failed||0],
    ['验证失败',counts.verify_failed||0],
    ['坏号',counts.bad||0]
  ];
  G('outlook-pool-summary').innerHTML=stats.map(function(item){
    return '<div class="stat"><span class="num">'+escapeHtml(String(item[1]))+'</span><span class="lbl">'+escapeHtml(item[0])+'</span></div>';
  }).join('');
  var bind=data.current_bind_email||'-';
  G('outlook-pool-current-bind').innerHTML='当前绑定邮箱: <strong>'+escapeHtml(bind)+'</strong>';
  if(G('bind_email'))G('bind_email').value=data.current_bind_email||'';
  if(G('email_provider') && data.email_provider){
    G('email_provider').value=data.email_provider;
    toggleEmailProviderFields();
  }
}

function loadOutlookPoolSummary(){
  return fetch('/api/outlook-pool/summary').then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'summary failed');}
    renderOutlookPoolSummary(j);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-summary'))G('outlook-pool-summary').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    return null;
  });
}

function reloadOutlookPoolList(resetPage){
  if(G('outlook-pool-filter'))outlookPoolState.status=G('outlook-pool-filter').value||'all';
  if(G('outlook-pool-query'))outlookPoolState.q=(G('outlook-pool-query').value||'').trim();
  if(resetPage)outlookPoolState.page=1;
  loadOutlookPoolList();
}

function changeOutlookPoolPage(delta){
  outlookPoolState.page=Math.max(1,(outlookPoolState.page||1)+delta);
  loadOutlookPoolList();
}

function loadOutlookPoolList(){
  var params=new URLSearchParams({
    status:outlookPoolState.status||'all',
    q:outlookPoolState.q||'',
    page:String(outlookPoolState.page||1),
    page_size:String(outlookPoolState.pageSize||20)
  });
  if(G('outlook-pool-list'))G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">加载中...</div>';
  return fetch('/api/outlook-pool/list?'+params.toString()).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'list failed');}
    outlookPoolState.items=j.items||[];
    outlookPoolState.total=j.total||0;
    renderOutlookPoolList();
    return j;
  }).catch(function(err){
    if(G('outlook-pool-list'))G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    if(G('outlook-pool-page-info'))G('outlook-pool-page-info').textContent='加载失败';
    return null;
  });
}

function renderOutlookPoolList(){
  if(!G('outlook-pool-list'))return;
  if(!outlookPoolState.items.length){
    G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">没有匹配的 Outlook 邮箱。</div>';
  }else{
    G('outlook-pool-list').innerHTML=outlookPoolState.items.map(function(item){
      var active=item.email===outlookPoolState.selectedEmail?' active':'';
      var when=item.last_event_time||item.last_result_time||'-';
      var record=item.has_result?'有本地结果':'无本地结果';
      var current=item.is_current_bind?'<span class="outlook-pool-pill">当前绑定</span>':'';
      return '<button class="outlook-pool-row'+active+'" type="button" onclick="selectOutlookPoolEmail('+JSON.stringify(item.email)+')"><div class="title"><span>'+escapeHtml(item.email)+'</span><span class="outlook-pool-pill">'+escapeHtml(item.status_label||item.status||'')+'</span>'+current+'</div><div class="meta"><span>'+escapeHtml(when)+'</span><span>'+escapeHtml(record)+'</span></div></button>';
    }).join('');
  }
  var totalPages=Math.max(1,Math.ceil((outlookPoolState.total||0)/(outlookPoolState.pageSize||20)));
  if(outlookPoolState.page>totalPages){
    outlookPoolState.page=totalPages;
  }
  if(G('outlook-pool-page-info'))G('outlook-pool-page-info').textContent='第 '+outlookPoolState.page+' / '+totalPages+' 页';
  if(G('outlook-pool-prev'))G('outlook-pool-prev').disabled=outlookPoolState.page<=1;
  if(G('outlook-pool-next'))G('outlook-pool-next').disabled=outlookPoolState.page>=totalPages;
}

function selectOutlookPoolEmail(email){
  outlookPoolState.selectedEmail=email||'';
  renderOutlookPoolList();
  loadOutlookPoolDetail(outlookPoolState.selectedEmail);
  loadOutlookPoolMessages(outlookPoolState.selectedEmail);
}

function renderOutlookActionButton(cls,label,action,email,status,disabled){
  return '<button class="'+cls+'" type="button" onclick="actOnOutlookPool('+JSON.stringify(action)+','+JSON.stringify(email||'')+','+JSON.stringify(status||'')+')"'+(disabled?' disabled':'')+'>'+label+'</button>';
}

function renderOutlookPoolDetail(entry){
  if(!G('outlook-pool-detail'))return;
  if(!entry){
    G('outlook-pool-detail').innerHTML='<div class="outlook-pool-empty">请选择一个 Outlook 邮箱。</div>';
    return;
  }
  var eventLabel=entry.last_event_status||'-';
  var resultTime=entry.last_result_time||'-';
  var bindMark=entry.is_current_bind?'<span class="outlook-pool-pill">当前绑定</span>':'';
  G('outlook-pool-detail').innerHTML='<div class="outlook-pool-detail-grid"><div class="outlook-pool-detail-item"><span class="k">邮箱</span><div class="v">'+escapeHtml(entry.email||'')+' '+bindMark+'</div></div><div class="outlook-pool-detail-item"><span class="k">状态</span><div class="v">'+escapeHtml(entry.status_label||entry.status||'')+'</div></div><div class="outlook-pool-detail-item"><span class="k">最后事件</span><div class="v">'+escapeHtml(eventLabel)+'</div></div><div class="outlook-pool-detail-item"><span class="k">事件时间</span><div class="v">'+escapeHtml(entry.last_event_time||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">手机号</span><div class="v">'+escapeHtml(entry.phone||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">Sub2API ID</span><div class="v">'+escapeHtml(entry.sub2api_id||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">绑定邮箱</span><div class="v">'+escapeHtml(entry.bind_email||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">结果时间</span><div class="v">'+escapeHtml(resultTime)+'</div></div></div><div class="outlook-pool-actions">'+renderOutlookActionButton('btn-danger','标记 bad','mark_status',entry.email,'bad',!entry.can_mark_bad)+renderOutlookActionButton('btn-neutral','标记 verify_failed','mark_status',entry.email,'verify_failed',!entry.can_mark_verify_failed)+renderOutlookActionButton('btn-neutral','标记 reserved','mark_status',entry.email,'reserved',!entry.can_mark_reserved)+renderOutlookActionButton('btn-primary','设为本次注册使用','assign_for_run',entry.email,'',!entry.can_assign)+renderOutlookActionButton('btn-primary','取下一个未使用','reserve_next_unused','','',!entry.can_assign)+'</div>';
}

function loadOutlookPoolDetail(email){
  if(!email){
    renderOutlookPoolDetail(null);
    return Promise.resolve(null);
  }
  return fetch('/api/outlook-pool/detail?email='+encodeURIComponent(email)).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'detail failed');}
    renderOutlookPoolDetail(j.entry||null);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-detail'))G('outlook-pool-detail').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    return null;
  });
}

function actOnOutlookPool(action,email,status){
  var payload={action:action};
  if(email)payload.email=email;
  if(status)payload.status=status;
  return fetch('/api/outlook-pool/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(r){return r.json().then(function(j){return {status:r.status,body:j};});}).then(function(resp){
    if(resp.status>=400 || !resp.body.ok){
      throw new Error(resp.body.error||'action failed');
    }
    if(resp.body.email){
      outlookPoolState.selectedEmail=resp.body.email;
    }
    loadOutlookPoolSummary();
    loadOutlookPoolList();
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
    loadOutlookPoolMessages(outlookPoolState.selectedEmail);
    if(resp.body.current_bind_email!==undefined && G('bind_email')){
      G('bind_email').value=resp.body.current_bind_email||'';
    }
    if(action==='assign_for_run'){
      toast('Assigned for current run',true);
    }else if(action==='reserve_next_unused'){
      toast('Reserved next unused mailbox',true);
    }else{
      toast('Status updated',true);
    }
    return resp.body;
  }).catch(function(err){
    toast(String(err.message||err),false);
    return null;
  });
}

function renderOutlookMessageList(){
  if(!G('outlook-pool-message-list'))return;
  if(!outlookPoolState.messages.length){
    G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">暂无邮件。</div>';
    return;
  }
  G('outlook-pool-message-list').innerHTML=outlookPoolState.messages.map(function(item,idx){
    var active=idx===outlookPoolState.selectedMessageIndex?' active':'';
    var preview=item.preview?'<div class="meta">'+escapeHtml(item.preview)+'</div>':'';
    return '<button class="outlook-pool-mail-row'+active+'" type="button" onclick="selectOutlookPoolMessage('+idx+')"><div class="subj">'+escapeHtml(item.subject||'(无主题)')+'</div><div class="meta">'+escapeHtml(item.from||'')+' - '+escapeHtml(item.date||'')+'</div>'+preview+'</button>';
  }).join('');
}

function renderOutlookMessageBody(){
  if(!G('outlook-pool-message-body'))return;
  var item=outlookPoolState.messages[outlookPoolState.selectedMessageIndex];
  if(!item){
    G('outlook-pool-message-body').textContent='暂无邮件内容。';
    return;
  }
  var text=(item.subject?item.subject+'\\n\\n':'')+(item.body||item.preview||'');
  G('outlook-pool-message-body').textContent=text||'暂无邮件内容。';
}

function renderOutlookPoolMessages(items){
  outlookPoolState.messages=items||[];
  if(outlookPoolState.selectedMessageIndex>=outlookPoolState.messages.length){
    outlookPoolState.selectedMessageIndex=0;
  }
  renderOutlookMessageList();
  renderOutlookMessageBody();
}

function selectOutlookPoolMessage(index){
  outlookPoolState.selectedMessageIndex=index||0;
  renderOutlookMessageList();
  renderOutlookMessageBody();
}

function loadOutlookPoolMessages(email){
  if(!email){
    renderOutlookPoolMessages([]);
    return Promise.resolve(null);
  }
  if(G('outlook-pool-message-list'))G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">邮件加载中...</div>';
  if(G('outlook-pool-message-body'))G('outlook-pool-message-body').textContent='邮件加载中...';
  return fetch('/api/outlook-pool/messages?email='+encodeURIComponent(email)+'&limit=20').then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'messages failed');}
    outlookPoolState.selectedMessageIndex=0;
    renderOutlookPoolMessages(j.items||[]);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-message-list'))G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    if(G('outlook-pool-message-body'))G('outlook-pool-message-body').textContent='邮件加载失败。';
    return null;
  });
}

function refreshSelectedOutlookMessages(){
  if(!outlookPoolState.selectedEmail){
    toast('请先选择一个 Outlook 邮箱',false);
    return;
  }
  loadOutlookPoolMessages(outlookPoolState.selectedEmail);
}

bootstrapOutlookPoolView();
var logEl=G('log'),logTabsEl=G('log-tabs'),logCursor=0;
var allLogs=[],threadLogs={},activeLogTab='all';

function ensureThreadTab(threadId){
  var tabId='log-tab-thread-'+threadId;
  if(G(tabId))return;
  var btn=document.createElement('button');
  btn.type='button';
  btn.id=tabId;
  btn.className='btn-neutral log-tab';
  btn.textContent='T'+threadId;
  btn.onclick=function(){setActiveLogTab(String(threadId));};
  logTabsEl.appendChild(btn);
}

function renderLogLine(item){
  return '<div class="'+escapeHtml(item.tag||'info')+'"><span class="time">'+escapeHtml(item.time||'')+'</span>'+escapeHtml(item.msg||'')+'</div>';
}

function renderLogPanel(){
  var items=activeLogTab==='all'?allLogs:(threadLogs[activeLogTab]||[]);
  if(!items.length){
    logEl.innerHTML='<span class="info">等待启动...</span>';
  }else{
    logEl.innerHTML=items.map(renderLogLine).join('');
  }
  if(G('auto-scroll').checked)logEl.scrollTop=logEl.scrollHeight;
}

function setActiveLogTab(tabId){
  activeLogTab=String(tabId||'all');
  Array.prototype.forEach.call(document.querySelectorAll('.log-tab'),function(btn){
    var isAll=btn.id==='log-tab-all'&&activeLogTab==='all';
    var isThread=btn.id==='log-tab-thread-'+activeLogTab;
    btn.classList.toggle('active',isAll||isThread);
  });
  renderLogPanel();
}

function pollLog(){
  fetch('/api/log-since/'+logCursor).then(function(r){return r.json()}).then(function(d){
    if(d.lines.length>0){
      d.lines.forEach(function(item){
        allLogs.push(item);
        if(item.thread!==undefined&&item.thread!==null){
          var key=String(item.thread);
          if(!threadLogs[key])threadLogs[key]=[];
          threadLogs[key].push(item);
          ensureThreadTab(key);
        }
      });
      renderLogPanel();
    }
    logCursor=d.cursor;
  });
}
setInterval(pollLog,800);

function saveConfig(){
    var d={sms_provider:G('sms_provider').value,api_key:G('api_key').value,proxy:G('proxy').value,countries:G('countries').value,
    password:G('password').value,max_price:G('max_price').value,
    sms_timeout:30,code_timeout:30,
    email_provider:G('email_provider').value,
    mailmanage_key:G('mailmanage_key').value,mailmanage_category:G('mailmanage_category').value,
    mailmanage_keyword:G('mailmanage_keyword').value,
    outlook_pool:G('outlook_pool').value,
    imap_user:G('imap_user').value,imap_pass:G('imap_pass').value,
    upload_target:G('upload_target').value,
    cpa_management_url:G('cpa_management_url').value,
    cpa_api_url:G('cpa_api_url').value,
    cpa_management_key:G('cpa_management_key').value,
    cpa_upload_mode:G('cpa_upload_mode').value,
    sub2api_url:G('sub2api_url').value,sub2api_email:G('sub2api_email').value,
    sub2api_pwd:G('sub2api_pwd').value,bind_email:G('bind_email').value,
    sub2api_group:G('sub2api_group').value,icloud_cookies:G('icloud_cookies').value,
    plus_method:G('plus_method').value,plus_email:G('plus_email').value,
    plus_phone:G('plus_phone').value,plus_pin:G('plus_pin').value,
    plus_country:G('plus_country').value,plus_currency:G('plus_currency').value,
    debug_mode:'0',no_phase2:G('no_phase2').checked?'1':'0',
    phase2_auto_skip:G('phase2_auto_skip').checked?'1':'0',
    sms_sort_by_price:G('sms_sort_by_price').checked?'1':'0'};
  return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){return r.json()}).then(function(j){toast('配置已保存',j.ok);return j;});
}

function checkBalance(){
  fetch('/api/balance').then(function(r){return r.json()}).then(function(j){
    if(j.ok){G('balance').textContent=j.balance.replace('ACCESS_BALANCE:','');}
  }).catch(function(){});
}

function startReg(){
  saveConfig().then(function(){
    G('btn-start').disabled=true;G('btn-stop').disabled=false;G('status-msg').innerHTML='<span class=spin></span>运行中';
    G('ok-count').textContent='0';G('fail-count').textContent='0';
    G('total-ok-count').textContent=G('total-ok-count').textContent||'0';
    G('total-fail-count').textContent=G('total-fail-count').textContent||'0';
    clearLog();
    var d={count:parseInt(G('count').value)||1,retries:parseInt(G('retries').value)||2,concurrency:parseInt(G('concurrency').value)||1};
    fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){return r.json()}).then(function(j){if(!j.ok)toast(j.error,false);});
  });
}

function togglePlusFields(){
  var v=G('plus_method').value;
  G('plus-paypal-group').style.display=(v=='paypal'?'':'none');
  G('plus-gopay-group').style.display=(v=='gopay'?'':'none');
}

function upgradePlus(){
  var phone=G('plus_phone').value.trim();
  var pin=G('plus_pin').value.trim();
  if(!phone||!pin){toast('请填写 GoPay 手机号和 PIN',false);return;}
  saveConfig().then(function(){
    fetch('/api/plus-upgrade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      plus_method:G('plus_method').value,plus_phone:phone,plus_pin:pin,plus_email:G('plus_email').value,
      plus_country:G('plus_country').value,
      plus_currency:G('plus_currency').value
    })}).then(function(r){return r.json()}).then(function(j){
      if(j.ok) toast('Plus 升级已启动',true);
      else toast(j.error,false);
    });
  });
}

function stopReg(){
  G('btn-stop').disabled=true;
  fetch('/api/stop',{method:'POST'}).then(function(){toast('正在停止...',true);});
}

function downloadResults(){window.open('/api/download');}
function clearLog(){
  allLogs=[];threadLogs={};activeLogTab='all';
  logTabsEl.innerHTML=`<button class="btn-neutral log-tab active" id="log-tab-all" type="button" onclick="setActiveLogTab('all')">全部</button>`;
  renderLogPanel();
}

function submitCode(){
  var code=G('bind-code-input').value.trim();
  if(!code||code.length<4){toast('验证码太短',false);return;}
  var tid=G('code-hint').dataset.tid||'';
  fetch('/api/submit-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,thread_id:tid})})
    .then(function(r){return r.json()}).then(function(j){
      G('bind-code-input').value='';
      G('code-panel').style.display='none';
      if(j.ok)toast('验证码已提交',true);
      else toast(j.error,false);
    });
}

function doContinue(){
  fetch('/api/continue',{method:'POST'}).then(function(){
    G('pause-panel').style.display='none';
    toast('继续执行',true);
  });
}

function doSkipPhase2(){
  fetch('/api/skip-phase2',{method:'POST'}).then(function(){
    G('pause-panel').style.display='none';
    toast('已跳过 Phase 2',true);
  });
}

function importCookies(){
  var raw=G('cookies_input').value.trim();
  if(!raw){toast('请粘贴 Cookies JSON',false);return;}
  var btn=document.querySelectorAll('#cookies_input + div button')[0];
  btn.disabled=true;btn.textContent='导入中...';
  fetch('/api/icloud-cookies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookies:raw})})
    .then(function(r){return r.json()}).then(function(j){
      btn.disabled=false;btn.textContent='导入 Cookies';
      if(j.ok){G('cookies_status').textContent='已导入 ('+j.size+' bytes)';G('cookies_status').style.color='#2e7d32';toast('Cookies 已导入',true);G('cookies_input').value='';}
      else{G('cookies_status').textContent=j.error;G('cookies_status').style.color='#c62828';toast(j.error,false);}
    }).catch(function(){btn.disabled=false;btn.textContent='导入 Cookies';toast('网络错误',false);});
}

function loadCookiesStatus(){
  fetch('/api/icloud-cookies').then(function(r){return r.json()}).then(function(j){
    if(j.ok && j.loaded){G('cookies_status').textContent='已加载 ('+j.size+' bytes)';G('cookies_status').style.color='#2e7d32';}
    else{G('cookies_status').textContent='未导入';G('cookies_status').style.color='#8b6f4e';}
  });
}

function toggleUploadTargetFields(){
  var v=G('upload_target').value||'sub2api';
  G('sub2api-group').style.display=(v=='sub2api'?'':'none');
  G('cpa-group').style.display=(v=='cpa'?'':'none');
}

function toggleEmailProviderFields(){
  var v=G('email_provider').value;
  G('mm-group').style.display=(v=='mailmanage'?'':'none');
  G('outlook-group').style.display=(v=='outlook'?'':'none');
  G('icloud-group').style.display=(v==''?'':'none');
}

function loadConfig(){
  fetch('/api/config').then(function(r){return r.json()}).then(function(j){
    if(!j.ok)return;
    var c=j.config;
    // New sms.* config
    if(c.sms){
      G('sms_provider').value=c.sms.provider||'smsbower';
      G('api_key').value=c.sms.api_key||'';
      G('countries').value=(c.sms.countries||[]).join(',');
    } else if(c.smsbower) {
      // Legacy fallback
      G('sms_provider').value='smsbower';
      G('api_key').value=c.smsbower.api_key||'';
      G('countries').value=c.country||'';
    }
    G('proxy').value=c.proxy||'';
    G('max_price').value=(c.sms&&c.sms.max_price)||c.max_price||'0.03';
    if(c.register && c.register.password) G('password').value=c.register.password;
    if(c.icloud){G('imap_user').value=c.icloud.user||'';G('imap_pass').value=c.icloud.pass||'';}
    if(c.sub2api){G('sub2api_url').value=c.sub2api.url||'';G('sub2api_email').value=c.sub2api.email||'';G('sub2api_pwd').value=c.sub2api.pwd||'';G('sub2api_group').value=c.sub2api.group||'CHATGPT';}
    G('upload_target').value=c.upload_target||'sub2api';
    if(c.cpa){G('cpa_management_url').value=c.cpa.management_url||'';G('cpa_api_url').value=c.cpa.api_url||'';G('cpa_management_key').value=c.cpa.management_key||'';G('cpa_upload_mode').value=c.cpa.upload_mode||'auto';}
    toggleUploadTargetFields();
    if(c.mailmanage){G('mailmanage_key').value=c.mailmanage.api_key||'';G('mailmanage_category').value=c.mailmanage.category||'safe';G('mailmanage_keyword').value=c.mailmanage.keyword||'gpt';}
    G('outlook_pool').value=c.outlook_pool||'';
    syncOutlookPoolEditor(c.outlook_pool||'');
    G('bind_email').value=c.bind_email||'';
    G('icloud_cookies').value=c.icloud_cookies||'';
    G('plus_method').value=c.plus_method||'gopay';
    G('plus_email').value=c.plus_email||'';
    G('plus_phone').value=c.plus_phone||'';
    G('plus_pin').value=c.plus_pin||'';
    G('plus_country').value=c.plus_country||'ID';
    G('plus_currency').value=c.plus_currency||'IDR';
    G('email_provider').value=c.email_provider||'';
    toggleEmailProviderFields();
    togglePlusFields();
    if(c.no_phase2) G('no_phase2').checked=true;
    if(c.phase2_auto_skip) G('phase2_auto_skip').checked=true;
    if(c.sms_sort_by_price) G('sms_sort_by_price').checked=true;
    checkBalance();
  });
}

// 补跑 Phase2 相关 JS
var _batchFiles = [];
function openBatchPanel(){
  G('batch-panel').style.display='flex';
  G('batch-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px"><span class=spin></span>加载中...</div>';
  _batchFiles = [];
  var src=G('batch-source').value;
  fetch('/api/results-list?source='+src).then(function(r){return r.json()}).then(function(j){
    if(!j.ok||!j.items.length){
      G('batch-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px">没有可补跑的账号</div>';
      G('batch-summary').textContent='0 个待处理';
      return;
    }
    _batchFiles = j.items;
    var html='',todo=0,done=0;
    j.items.forEach(function(item){
      var isChecked=!item.has_phase2;
      if(isChecked) todo++; else done++;
      var key=item.filename||item.index;
      html+='<label style="display:flex;align-items:center;gap:6px;padding:3px 4px;border-bottom:1px solid #f0e4d0;cursor:pointer">'+
        '<input type="checkbox" class="batch-cb" data-key="'+key+'"'+(isChecked?' checked':'')+' style="width:auto;margin:0">'+
        '<span style="flex:1">'+item.phone+'</span>'+
        '<span style="font-size:11px;color:'+(item.has_phase2?'#2e7d32':'#999')+'">'+(item.has_phase2?'已完成':'待处理')+'</span></label>';
    });
    G('batch-list').innerHTML=html;
    G('batch-summary').textContent=todo+' 个待处理, '+done+' 个已完成';
    G('batch-select-all').checked=true;
  }).catch(function(){G('batch-list').innerHTML='<div style="text-align:center;color:#c62828;padding:20px">加载失败</div>';});
}
function closeBatchPanel(){G('batch-panel').style.display='none';}
function toggleSelectAll(){var sel=G('batch-select-all').checked;document.querySelectorAll('.batch-cb').forEach(function(cb){cb.checked=sel;});}
function selectedBatchKeys(){var files=[];document.querySelectorAll('.batch-cb:checked').forEach(function(cb){files.push(cb.dataset.key);});return files;}
function deleteSelectedBatchItems(){
  var files=selectedBatchKeys(),src=G('batch-source').value;
  if(!files.length){toast('请至少选择一个账号',false);return;}
  if(src!=='files'){toast('只能删除 results目录 的记录',false);return;}
  if(!confirm('确定删除所选 '+files.length+' 条补跑记录？'))return;
  G('btn-batch-delete').disabled=true;
  fetch('/api/batch-phase2-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:files,source:src})})
    .then(function(r){return r.json()}).then(function(j){
      G('btn-batch-delete').disabled=false;
      if(!j.ok){toast(j.error,false);return;}
      toast('已删除 '+j.deleted+' 条记录',true);
      openBatchPanel();
    }).catch(function(){G('btn-batch-delete').disabled=false;toast('删除失败',false);});
}
function startBatchPhase2(){
  var files=selectedBatchKeys();
  if(!files.length){toast('请至少选择一个账号',false);return;}
  var email=G('batch-email').value.trim(),src=G('batch-source').value,conc=parseInt(G('batch-concurrency')?.value)||1;
  G('btn-batch-start').disabled=true;G('batch-running').style.display='inline';
  fetch('/api/batch-phase2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:files,email:email,source:src,concurrency:conc})})
    .then(function(r){return r.json()}).then(function(j){
      if(!j.ok){toast(j.error,false);G('btn-batch-start').disabled=false;G('batch-running').style.display='none';}
      else toast('已开始补跑 '+files.length+' 个账号',true);
    }).catch(function(){G('btn-batch-start').disabled=false;G('batch-running').style.display='none';});
  var pollId=setInterval(function(){fetch('/api/status').then(function(r){return r.json()}).then(function(j){
    if(!j.running){clearInterval(pollId);G('btn-batch-start').disabled=false;G('batch-running').style.display='none';toast('补跑完成',true);openBatchPanel();}
  });},2000);
}

function openFailedUploadsPanel(){
  G('failed-upload-panel').style.display='flex';
  G('failed-upload-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px"><span class=spin></span>加载中...</div>';
  fetch('/api/failed-uploads/list').then(function(r){return r.json()}).then(function(j){
    if(!j.ok||!j.items.length){
      G('failed-upload-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px">没有待补传文件</div>';
      G('failed-upload-summary').textContent='0 个待补传';
      return;
    }
    var html='';
    j.items.forEach(function(item){
      html+='<label style="display:flex;align-items:center;gap:6px;padding:3px 4px;border-bottom:1px solid #f0e4d0;cursor:pointer">'+
        '<input type="checkbox" class="failed-upload-cb" data-path="'+escapeHtml(item.path)+'" checked style="width:auto;margin:0">'+
        '<span style="flex:1">'+escapeHtml(item.phone||item.email||item.filename)+'</span>'+
        '<span style="font-size:11px;color:#999">'+escapeHtml(item.upload_target||'sub2api')+'</span></label>';
    });
    G('failed-upload-list').innerHTML=html;
    G('failed-upload-summary').textContent=j.items.length+' 个待补传';
    G('failed-upload-select-all').checked=true;
  }).catch(function(){G('failed-upload-list').innerHTML='<div style="text-align:center;color:#c62828;padding:20px">加载失败</div>';});
}
function closeFailedUploadsPanel(){G('failed-upload-panel').style.display='none';}
function toggleFailedUploadSelectAll(){var sel=G('failed-upload-select-all').checked;document.querySelectorAll('.failed-upload-cb').forEach(function(cb){cb.checked=sel;});}
function selectedFailedUploadPaths(){var paths=[];document.querySelectorAll('.failed-upload-cb:checked').forEach(function(cb){paths.push(cb.dataset.path);});return paths;}
function retrySelectedFailedUploads(){
  var paths=selectedFailedUploadPaths();
  if(!paths.length){toast('请至少选择一个失败上传文件',false);return;}
  G('btn-failed-upload-retry').disabled=true;G('failed-upload-running').style.display='inline';
  var ok=0,fail=0;
  paths.reduce(function(p,path){
    return p.then(function(){
      return fetch('/api/failed-uploads/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:path})})
        .then(function(r){return r.json()}).then(function(j){if(j.ok)ok++;else fail++;})
        .catch(function(){fail++;});
    });
  },Promise.resolve()).then(function(){
    G('btn-failed-upload-retry').disabled=false;G('failed-upload-running').style.display='none';
    toast('补传完成：成功 '+ok+'，失败 '+fail,fail===0);
    openFailedUploadsPanel();
  });
}

function pollCodeNeed(){
  fetch('/api/waiting-code').then(function(r){return r.json()}).then(function(j){
    if(j.waiting){var hint=G('code-hint');hint.textContent=(j.thread_id||'')+' 验证码';hint.dataset.tid=j.thread_id||'';G('code-panel').style.display='flex';G('bind-code-input').focus();}
  });
}
setInterval(pollCodeNeed,2000);

function pollPause(){
  fetch('/api/waiting-pause').then(function(r){return r.json()}).then(function(j){
    if(j.paused){
      G('pause-panel').style.display='flex';
      if(j.phase2_retry){G('pause-msg').textContent='Phase 2 失败，是否重试？';G('btn-skip-phase2').style.display='inline-block';}
      else{G('pause-msg').textContent='调试暂停中';G('btn-skip-phase2').style.display='none';}
    }
  });
}
setInterval(pollPause,2000);

setInterval(function(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(j){
    var running=j.running;
    G('btn-start').disabled=running;G('btn-stop').disabled=!running;
    if(!running)G('status-msg').textContent='就绪';
    var stats=j.stats||{};
    G('ok-count').textContent=stats.current_success||0;
    G('fail-count').textContent=stats.current_fail||0;
    G('total-ok-count').textContent=stats.total_success||0;
    G('total-fail-count').textContent=stats.total_fail||0;
  });
},2000);

loadCookiesStatus();
loadConfig();

</script></body></html>
"""

if __name__ == "__main__":
    start_gui()


