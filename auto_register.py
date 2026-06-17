#!/usr/bin/env python3
"""
ChatGPT Auto Register - Fully automated phone-based registration.

Combines three independent techniques:
  1. curl_cffi   - Chrome TLS fingerprint (bypasses Cloudflare network layer)
  2. Sentinel    - FNV-1a Proof-of-Work (bypasses JS anti-bot challenges)
  3. SMSBower    - Automated SMS verification code retrieval

Usage:
  python auto_register.py                  # interactive mode
  python auto_register.py -n 5             # register 5 accounts
  python auto_register.py --gui            # start web GUI
"""

import argparse
import json
import os
import secrets
import string
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Tuple

from chatgpt_register import ChatGPTRegister
from phone_sms_adapter import StopRequested, UnifiedSMS, parse_countries
from sms_price_check import load_success_price_rows, record_success_price, save_success_price_rows


_SMS_SUCCESS_PRICE_FILE = Path(__file__).parent / "sms_success_prices.json"


def _record_sms_success_price(sms_provider: str, country: str, success_price):
    if not sms_provider or not country or success_price in (None, ""):
        return
    try:
        rows = load_success_price_rows(_SMS_SUCCESS_PRICE_FILE)
        rows = record_success_price(rows, sms_provider, country, success_price)
        save_success_price_rows(_SMS_SUCCESS_PRICE_FILE, rows)
    except Exception:
        pass

# 随机资料
# ============================================================

_FIRST_NAMES = [
    "James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Daniel",
    "Matthew","Anthony","Mark","Christopher","Paul","Steven","Andrew","Joshua","Kenneth","Kevin",
    "Brian","George","Timothy","Edward","Ronald","Jason","Jeffrey","Ryan","Jacob","Gary",
    "Nicholas","Eric","Stephen","Jonathan","Larry","Justin","Scott","Brandon","Frank","Raymond",
]
_LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Miller","Davis","Garcia","Rodriguez","Wilson",
    "Martinez","Anderson","Taylor","Thomas","Hernandez","Moore","Martin","Jackson","Thompson","White",
    "Lopez","Lee","Gonzalez","Harris","Clark","Lewis","Robinson","Walker","Perez","Hall",
    "Young","Allen","Sanchez","Wright","King","Scott","Green","Baker","Adams","Nelson",
]

def random_name() -> str:
    return f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}"

def random_birthdate() -> str:
    y = secrets.choice(range(1982, 2003))
    m = secrets.choice(range(1, 13))
    d = secrets.choice(range(1, 29))
    return f"{y:04d}-{m:02d}-{d:02d}"

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(chars) for _ in range(length))

def _retry_call(fn, max_retries=2, delay=2, label="", stop_requested=None):
    """重试包装器 — 失败自动重试，期间检查停止信号"""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_retries:
                raise
            if stop_requested and stop_requested():
                raise StopRequested("stopped during retry")
            if label:
                print(f"  [{label}] 失败 ({e})，{delay}s 后重试 ({attempt+1}/{max_retries})...")
            # Interruptible sleep — check stop every 0.1s
            for _ in range(int(delay * 10)):
                if stop_requested and stop_requested():
                    raise StopRequested("stopped during retry sleep")
                _time.sleep(0.1)



class PhoneNumberUnavailable(RuntimeError):
    """Raised after a country exhausts its phone-number attempts."""

    def __init__(self, provider: str, country: str, attempts: int, last_error: str = ""):
        self.provider = provider
        self.country = country
        self.attempts = attempts
        self.last_error = last_error
        message = f"{provider} country={country}: no numbers after {attempts} attempts"
        if last_error:
            message = f"{message} ({last_error})"
        super().__init__(message)


def _format_price(price: float) -> str:
    return f"{price:.6f}".rstrip("0").rstrip(".")


def _lower_price_limit(current: str, cheapest_price: float) -> str:
    if cheapest_price <= 0:
        return current or ""
    if current in (None, ""):
        return _format_price(cheapest_price)
    try:
        current_price = float(current)
    except (TypeError, ValueError):
        return _format_price(cheapest_price)
    return _format_price(min(current_price, cheapest_price))


def _get_number_with_retry(
    sms: UnifiedSMS,
    service: str,
    countries: list,
    provider_ids: str = "",
    max_price: str = "",
    verbose: bool = True,
    retry_delay: int = 2,
    stop_requested=None,
) -> Tuple[str, str, str, str]:
    """Retry getting a phone number forever: 100 attempts per country, then rotate."""
    if not countries:
        raise ValueError("countries list is empty — cannot get phone number without a country")
    normalized_countries = [str(c).strip() for c in countries if str(c).strip()]
    if not normalized_countries:
        raise ValueError("countries list is empty — cannot get phone number without a country")

    MAX_ATTEMPTS_PER_COUNTRY = 100
    cheapest_cache = {}

    def cheapest_args_for_country(country: str):
        if country in cheapest_cache:
            return cheapest_cache[country]

        country_provider_ids = provider_ids or ""
        country_max_price = max_price or ""
        actual_price = ""  # 实际扣费价格
        if sms.provider != "smsbower":
            # hero-sms: 用 getPrices 获取实际价格
            try:
                import requests as _requests
                r = _requests.get(
                    sms._base_url,
                    params={"api_key": sms.api_key, "action": "getPrices", "service": service, "country": country},
                    timeout=15,
                )
                data = r.json()
                info = data.get(country, {}).get(service, {})
                cost = info.get("cost")
                if cost is not None:
                    actual_price = str(float(cost))
            except Exception:
                pass
            cheapest_cache[country] = (country_provider_ids, country_max_price, actual_price)
            return cheapest_cache[country]

        try:
            cheapest_provider, cheapest_price = sms.get_cheapest_provider(service=service, country=country)
            if not cheapest_provider or not cheapest_price or cheapest_price <= 0 or cheapest_price >= 999:
                raise RuntimeError("no valid cheapest provider returned")
            country_provider_ids = cheapest_provider
            country_max_price = _lower_price_limit(country_max_price, cheapest_price)
            actual_price = str(cheapest_price)  # smsbower: 用查询到的最低价作为实际价格
            if verbose:
                provider_msg = f" providerIds={country_provider_ids}" if country_provider_ids else ""
                print(f"  [拿手机号] country={country} 最低价=${_format_price(cheapest_price)}{provider_msg} maxPrice={country_max_price}")
        except Exception as e:
            if verbose:
                print(f"  [拿手机号] country={country} 查询最低价失败，跳过当前国家: {e}")
            return None

        cheapest_cache[country] = (country_provider_ids, country_max_price, actual_price)
        return cheapest_cache[country]

    round_no = 0
    while True:
        round_no += 1
        tried_any_country = False
        for country in normalized_countries:
            cheapest_args = cheapest_args_for_country(country)
            if cheapest_args is None:
                continue
            tried_any_country = True
            country_provider_ids, country_max_price, actual_price = cheapest_args
            for attempt in range(1, MAX_ATTEMPTS_PER_COUNTRY + 1):
                if stop_requested and stop_requested():
                    raise StopRequested("stopped while waiting for phone number")
                try:
                    if verbose:
                        print(f"  [拿手机号] provider={sms.provider} country={country} 尝试 #{attempt}/{MAX_ATTEMPTS_PER_COUNTRY}...")
                    aid, phone = sms.get_number(
                        service=service,
                        country=country,
                        provider_ids=country_provider_ids,
                        max_price=country_max_price,
                    )
                    return aid, phone, country, actual_price
                except Exception as e:
                    if stop_requested and stop_requested():
                        raise StopRequested("stopped while waiting for phone number")
                    if attempt >= MAX_ATTEMPTS_PER_COUNTRY:
                        if verbose:
                            print(f"  [拿手机号] country={country} 已失败 {MAX_ATTEMPTS_PER_COUNTRY} 次，切换到下一个国家...")
                        break
                    if verbose:
                        print(f"  [拿手机号] provider={sms.provider} country={country} 失败 ({e})，{retry_delay}s 后继续重试当前国家 ({attempt}/{MAX_ATTEMPTS_PER_COUNTRY})...")
                    for _ in range(int(retry_delay * 10)):
                        if stop_requested and stop_requested():
                            raise StopRequested("stopped while waiting for phone number")
                        _time.sleep(0.1)
        if not tried_any_country:
            if verbose:
                print(f"  [拿手机号] 所有国家最低价查询均失败，{retry_delay}s 后重新查询...")
            for _ in range(int(retry_delay * 10)):
                if stop_requested and stop_requested():
                    raise StopRequested("stopped while waiting for phone number")
                _time.sleep(0.1)
            cheapest_cache.clear()
            continue
        if verbose:
            print(f"  [拿手机号] 所有国家已完成第 {round_no} 轮尝试，从第一个国家重新开始...")


def _fail_result(phone: str, failure_stage: str, error: str, sms_provider: str = "", country: str = "", activation_id: str = "") -> dict:
    return {
        "ok": False, "phone": phone, "password": "",
        "name": "", "birthdate": "",
        "session_token": "", "access_token": "", "activation_id": activation_id,
        "status_version": 2,
        "phone_ok": False, "account_created": False, "token_ok": False,
        "email_selected": False, "email_bound": False, "uploaded": False,
        "final_ok": False, "status": "register_failed", "failure_stage": failure_stage,
        "retryable": False, "quota_charged": False,
        "sms_provider": sms_provider, "country": country, "error": error,
    }

DEFAULT_SMS_MAX_PRICE = "0.03"
_SUPPORTED_SMS_PROVIDERS = ("smsbower", "hero-sms")


def _default_sms_provider_config(provider: str) -> dict:
    return {
        "api_key": "",
        "countries": [],
        "service": "dr",
        "operator": "any",
        "max_price": DEFAULT_SMS_MAX_PRICE,
    }


def _normalize_sms_config(sms_cfg: dict, legacy_found: dict = None) -> dict:
    legacy_found = legacy_found or {}
    active_provider = str(sms_cfg.get("active_provider") or sms_cfg.get("provider") or "smsbower")
    if active_provider not in _SUPPORTED_SMS_PROVIDERS:
        active_provider = "smsbower"

    raw_providers = sms_cfg.get("providers") or {}
    providers = {}
    for provider in _SUPPORTED_SMS_PROVIDERS:
        provider_cfg = _default_sms_provider_config(provider)
        provider_cfg.update(dict(raw_providers.get(provider) or {}))
        provider_cfg["countries"] = parse_countries(provider_cfg.get("countries"))
        provider_cfg["max_price"] = str(provider_cfg.get("max_price") or DEFAULT_SMS_MAX_PRICE)
        providers[provider] = provider_cfg

    active_provider_cfg = providers[active_provider]
    for key in ("api_key", "countries", "service", "operator", "max_price"):
        if key in sms_cfg and sms_cfg.get(key) not in (None, "", []):
            active_provider_cfg[key] = sms_cfg[key]
    active_provider_cfg["countries"] = parse_countries(active_provider_cfg.get("countries"))
    active_provider_cfg["max_price"] = str(active_provider_cfg.get("max_price") or DEFAULT_SMS_MAX_PRICE)

    if "smsbower" in legacy_found:
        providers["smsbower"]["api_key"] = legacy_found["smsbower"].get("api_key", providers["smsbower"].get("api_key", ""))
        if legacy_found.get("country") and not providers["smsbower"].get("countries"):
            providers["smsbower"]["countries"] = parse_countries(legacy_found.get("country"))

    current = dict(providers[active_provider])
    return {
        "active_provider": active_provider,
        "provider": active_provider,
        "providers": providers,
        "api_key": current.get("api_key", ""),
        "countries": parse_countries(current.get("countries")),
        "service": current.get("service", "dr"),
        "operator": current.get("operator", "any"),
        "max_price": str(current.get("max_price") or DEFAULT_SMS_MAX_PRICE),
    }


# ============================================================
# 配置
# ============================================================

def load_config(path: str = None) -> dict:
    config = {
        "sms": {"provider": "smsbower", "api_key": "", "countries": [], "service": "dr", "operator": "any", "max_price": DEFAULT_SMS_MAX_PRICE},
        "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
        "proxy": "",
        "country": "",
        "service": "dr",
        "code_timeout": 30,
        "upload_target": "sub2api",
        "cpa": {
            "management_url": "",
            "api_url": "",
            "management_key": "",
            "upload_mode": "auto",
        },
    }
    candidates = [path, "config.json", str(Path(__file__).parent / "config.json")]
    found = {}
    for p in candidates:
        if p and Path(p).exists():
            with open(p, "r", encoding="utf-8") as f:
                found = json.load(f)
            if "sms" in found:
                config["sms"] = dict(found["sms"])
            elif "smsbower" in found:
                config["sms"]["api_key"] = found["smsbower"].get("api_key", "")
                config["sms"]["provider"] = "smsbower"
                old_country = found.get("country", "")
                config["sms"]["countries"] = [str(old_country)] if str(old_country).strip() else []
            # Passthrough old keys
            for k in ["proxy", "country", "service", "code_timeout"]:
                if k in found:
                    config[k] = found[k]
            for k, v in found.items():
                if k not in {"sms", "smsbower", "register", "proxy", "country", "service", "code_timeout"}:
                    config[k] = v

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
            break
    if os.environ.get("SMSBOWER_KEY"):
        config["sms"]["api_key"] = os.environ["SMSBOWER_KEY"]
    config["sms"] = _normalize_sms_config(config.get("sms", {}), found)
    proxy_env = os.environ.get("PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_env:
        config["proxy"] = proxy_env
    # Normalize countries
    if not isinstance(config["sms"].get("countries"), list):
        config["sms"]["countries"] = parse_countries(config["sms"].get("countries"))
    if not config["sms"]["countries"] and config.get("country"):
        config["sms"]["countries"] = parse_countries(config.get("country"))
    if not config["sms"]["countries"]:
        raise ValueError("请在配置中填写 sms.countries（多个国家用逗号或数组），不能使用硬编码默认国家")
    if not str(config["sms"].get("max_price", "")).strip():
        config["sms"]["max_price"] = DEFAULT_SMS_MAX_PRICE
    return config

# ============================================================
# 注册核心
# ============================================================

def register_one(
    sms: UnifiedSMS,
    config: dict,
    provider_ids: str = "",
    max_price: str = "",
    verbose: bool = True,
    step_retries: int = 2,
    create_account_max_retries: int = 20,
    phone_retry_delay: int = 2,
    stop_requested=None,
    no_phase2: bool = False,
) -> dict:
    service = config["sms"]["service"]
    countries = config["sms"]["countries"]
    reg_cfg = config["register"]

    # Stage status tracking
    phone_ok = False
    account_created = False
    token_ok = False
    email_selected = False  # Set by caller (runner/web_gui)
    email_bound = False     # Set by Phase2 caller
    uploaded = False        # Set by Phase2 caller
    final_ok = False
    status = "register_failed"
    failure_stage = ""
    retryable = False
    quota_charged = False
    sms_provider = sms.provider
    used_country = ""
    sms_price = ""

    password = reg_cfg["password"] or random_password()
    name = reg_cfg.get("name") or random_name()
    birthdate = reg_cfg.get("birthdate") or random_birthdate()
    if name == "A" and birthdate == "2000-01-01":
        name = random_name()
        birthdate = random_birthdate()

    phone = "?"
    aid = ""
    reg = None
    sr = step_retries

    try:
        # ── 阶段 1: 获取手机号 ──
        if verbose:
            print(f"  [阶段] 获取手机号 provider={sms.provider} countries={countries}")
        aid, phone_raw, used_country, sms_price = _get_number_with_retry(
            sms,
            service=service,
            countries=countries,
            provider_ids=provider_ids,
            max_price=max_price,
            verbose=verbose,
            retry_delay=phone_retry_delay,
            stop_requested=stop_requested,
        )
        phone = "+" + phone_raw if not phone_raw.startswith("+") else phone_raw
        if verbose:
            print(f"  [阶段] 手机号获取成功: {phone} 激活ID: {aid}")
        sms.set_ready()
        if verbose:
            print(f"  [阶段] set_ready 成功")

        # ── 阶段 2: 建立 ChatGPT 注册会话 ──
        if verbose:
            print(f"  [阶段] 初始化 ChatGPT 注册会话 proxy={config.get('proxy', '直连')}")
        reg = ChatGPTRegister(proxy=config["proxy"])

        if verbose:
            print(f"  [1/9] 访问登录页 chatgpt.com/auth/login")
        _retry_call(lambda: reg.visit(), sr, label="访问首页", stop_requested=stop_requested)

        if verbose:
            print(f"  [2/9] 获取 CSRF token")
        csrf = _retry_call(lambda: reg.get_csrf(), sr, label="CSRF", stop_requested=stop_requested)
        if verbose:
            print(f"  [2/9] CSRF 获取成功")

        if verbose:
            print(f"  [3/9] 发起手机号登录/注册")
        redirect = _retry_call(lambda: reg.signin(phone, csrf), sr, label="发起登录", stop_requested=stop_requested)
        if verbose:
            print(f"  [3/9] 登录请求已发送 redirect_url 获取成功")

        if verbose:
            print(f"  [4/9] 跳转 auth.openai.com OAuth")
        _retry_call(lambda: reg.jump_to_auth(redirect), sr, label="OAuth跳转", stop_requested=stop_requested)
        if verbose:
            print(f"  [4/9] OAuth 跳转成功")

        if verbose:
            print(f"  [5/9] 提交手机号+密码注册")
        result = _retry_call(lambda: reg.register_user(phone, password), sr, label="注册", stop_requested=stop_requested)

        continue_url = result.get("continue_url", "")
        if not continue_url:
            sms.cancel()
            if verbose:
                print(f"  [5/9] 注册被拒 status={result.get('_status')}")
            return _fail_result(phone, "register_rejected", f"注册被拒(status={result.get('_status')})", sms_provider, used_country, aid)
        if verbose:
            print(f"  [5/9] 手机号注册成功 continue_url 已返回")

        # ── 阶段 3: 发送并接收 OTP ──
        if verbose:
            print(f"  [6/9] 发送手机验证码 OTP")
        _retry_call(lambda: reg.send_otp(continue_url), sr, label="发送验证码", stop_requested=stop_requested)
        if verbose:
            print(f"  [6/9] OTP 发送成功 等待平台返回验证码 (timeout={config['code_timeout']}s)")

        code = sms.wait_code(timeout=config["code_timeout"], stop_requested=stop_requested)
        if not code:
            sms.cancel()
            if verbose:
                print(f"  [6/9] OTP 验证码超时 timeout={config['code_timeout']}s")
            return _fail_result(phone, "otp_timeout", "验证码超时", sms_provider, used_country, aid)

        if verbose:
            print(f"  [6/9] 收到验证码: {code}")

        if verbose:
            print(f"  [7/9] 校验 OTP 验证码")
        result = _retry_call(lambda: reg.validate_otp(code), sr, label="校验验证码", stop_requested=stop_requested)
        continue_url = result.get("continue_url", "")
        if not continue_url:
            sms.cancel()
            if verbose:
                print(f"  [7/9] OTP 校验失败 status={result.get('_status')}")
            return _fail_result(phone, "otp_validation_failed", f"验证码校验失败(status={result.get('_status')})", sms_provider, used_country, aid)
        if verbose:
            print(f"  [7/9] OTP 校验成功")

        # ── 阶段 4: 创建账户资料 ──
        if verbose:
            print(f"  [8/9] 访问 about-you 页面建立会话上下文")
        _retry_call(lambda: reg.visit_about_you(continue_url), sr, label="访问about-you", stop_requested=stop_requested)
        if verbose:
            print(f"  [8/9] about-you 页面访问成功 开始创建账户资料")

        last_create_error = ""
        for ca_attempt in range(create_account_max_retries):
            ca_name = random_name() if ca_attempt > 0 else name
            ca_birthdate = random_birthdate() if ca_attempt > 0 else birthdate
            if verbose:
                print(f"  [8/9] 创建账户资料 [{ca_attempt+1}/{create_account_max_retries}]: name={ca_name} birthdate={ca_birthdate}")

            result = reg.create_account(ca_name, ca_birthdate)
            callback_url = result.get("continue_url", "")
            if callback_url:
                name = ca_name
                birthdate = ca_birthdate
                if verbose:
                    print(f"  [8/9] 账户资料创建成功 callback_url 已返回")
                break

            last_create_error = result.get("_body", "") or f"status={result.get('_status')}"
            if verbose:
                detail = last_create_error[:200]
                print(f"  [8/9] 创建账户失败 [{ca_attempt+1}]: {detail}")
            if ca_attempt < create_account_max_retries - 1:
                # Interruptible sleep — check stop every 0.1s
                for _ in range(10):
                    if stop_requested and stop_requested():
                        raise StopRequested("stopped during account creation")
                    _time.sleep(0.1)

        if not callback_url:
            sms.cancel()
            if verbose:
                print(f"  [8/9] 账户资料创建最终失败 (已重试{create_account_max_retries}次)")
            return _fail_result(phone, "account_creation_failed", f"创建账户失败(已重试{create_account_max_retries}次): {last_create_error[:200]}", sms_provider, used_country, aid)

        # ── 阶段 5: OAuth 回调 & 获取 Token ──
        if verbose:
            print(f"  [9/9] OAuth 回调获取 session token")
        token = _retry_call(lambda: reg.oauth_callback(callback_url), sr, label="OAuth回调", stop_requested=stop_requested)
        if verbose:
            print(f"  [9/9] session token 获取成功")

        if verbose:
            print(f"  [9/9] 获取 access token")
        access_token = _retry_call(lambda: reg.get_access_token(), sr, label="获取Token", stop_requested=stop_requested)
        if verbose:
            print(f"  [9/9] access token 获取成功: {bool(access_token)}")

        sms.complete()
        if verbose:
            print(f"  [阶段] 短信平台激活标记完成 complete()")

        # Set stage status
        phone_ok = True
        account_created = True
        if no_phase2:
            final_ok = True
            status = "final_ok"
            token_ok = True
        else:
            status = "phone_ok"
            token_ok = False  # Will be set to True by Phase2 caller
            retryable = True

        if verbose:
            print(f"  [完成] 手机号阶段成功 phone={phone} provider={sms_provider} country={used_country} final_ok={final_ok}")
        _record_sms_success_price(sms_provider, used_country, sms_price)

        return {
            "ok": final_ok, "phone": phone, "password": password,
            "name": name, "birthdate": birthdate,
            "session_token": token, "access_token": access_token, "activation_id": aid,
            "status_version": 2,
            "phone_ok": phone_ok, "account_created": account_created, "token_ok": token_ok,
            "email_selected": email_selected, "email_bound": email_bound, "uploaded": uploaded,
            "final_ok": final_ok, "status": status, "failure_stage": failure_stage,
            "retryable": retryable, "quota_charged": quota_charged,
            "sms_provider": sms_provider, "country": used_country, "sms_price": sms_price,
        }

    except StopRequested:
        raise
    except Exception as e:
        try: sms.cancel()
        except Exception: pass
        if verbose:
            print(f"  [异常] 未预期错误: {e}")
        return _fail_result(phone, "unexpected_error", str(e), sms_provider, used_country, aid)

# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="ChatGPT 自动注册")
    parser.add_argument("--config", "-c", type=str, help="配置文件路径")
    parser.add_argument("--count", "-n", type=int, default=1, help="目标成功数量")
    parser.add_argument("--country", type=str, help="国家 ID（覆盖配置里的 sms.countries）")
    parser.add_argument("--service", type=str, help="服务代码 (默认 dr=OpenAI)")
    parser.add_argument("--provider", type=str, default="", help="指定运营商 ID")
    parser.add_argument("--max-price", type=str, default="", help="最高价格")
    parser.add_argument("--proxy", type=str, help="代理地址")
    parser.add_argument("--password", type=str, help="密码 (留空随机)")
    parser.add_argument("--retry", "-r", type=int, default=2, help="各步骤重试次数")
    parser.add_argument("--create-retry", type=int, default=20, help="创建账户重试次数 (默认20)")
    parser.add_argument("--output", "-o", type=str, default="register_results.json")
    parser.add_argument("--gui", action="store_true", help="启动 Web GUI")
    # Phase 2
    parser.add_argument("--phase2", action="store_true")
    parser.add_argument("--bind-email", type=str)
    parser.add_argument("--icloud-cookies", type=str)
    parser.add_argument("--sub2api-url", type=str)
    parser.add_argument("--sub2api-email", type=str)
    parser.add_argument("--sub2api-pwd", type=str)
    parser.add_argument("--sub2api-proxy-id", type=int, default=0)
    parser.add_argument("--sub2api-group-id", type=int, default=1)

    args = parser.parse_args()

    if args.gui:
        from web_gui import start_gui
        start_gui()
        return

    config = load_config(args.config)
    sms_cfg = config["sms"]
    if args.country: sms_cfg["countries"] = [args.country]
    if args.service: sms_cfg["service"] = args.service
    if args.proxy: config["proxy"] = args.proxy
    if args.password: config["register"]["password"] = args.password

    if not sms_cfg["api_key"]:
        print("错误: 需要短信平台 API Key.")
        sys.exit(1)

    sms = UnifiedSMS(
        provider=sms_cfg["provider"],
        api_key=sms_cfg["api_key"],
        operator=sms_cfg.get("operator", "any"),
    )
    bal = sms.balance()
    try:
        pid, price = sms.get_cheapest_provider(sms_cfg["service"], sms_cfg["countries"][0])
    except Exception:
        pid, price = "?", 0
    print(f"余额: {bal}  provider={sms_cfg['provider']} countries={sms_cfg['countries']}  运营商: {pid} (${price:.4f})")
    print(f"代理: {config['proxy'] or '直连'}  目标: {args.count}个")
    print("-" * 50)

    results = []
    ok_count = 0
    attempt = 0
    max_attempts = args.count * 10

    while ok_count < args.count and attempt < max_attempts:
        attempt += 1
        print(f"\n第 {attempt} 次 [{ok_count}/{args.count}]")
        try:
            result = register_one(sms, config, provider_ids="",
                                  max_price=sms_cfg.get("max_price", ""),
                                  step_retries=args.retry,
                                  create_account_max_retries=args.create_retry,
                                  verbose=True,
                                  no_phase2=not args.phase2)
        except Exception as e:
            result = {"ok": False, "phone": "?", "error": str(e)}
        results.append(result)
        if result["ok"]:
            ok_count += 1
            phone = result.get("phone", "?")
            token = result.get("session_token", "")
            at = result.get("access_token", "")
            print(f"  成功: {phone}  名称: {result.get('name','?')} ChatGPT密码: {result.get('password','')}")
            if args.phase2 and args.sub2api_url:
                try:
                    from phase2_codex import upload_session
                    upload_result = upload_session(token, args.bind_email or "", args.sub2api_url,
                                                   args.sub2api_email, args.sub2api_pwd,
                                                   sub2api_proxy_id=args.sub2api_proxy_id,
                                                   group_ids=[args.sub2api_group_id], access_token=at)
                    result["upload_target"] = "sub2api"
                    result["uploaded"] = bool(upload_result.get("uploaded", upload_result.get("ok")))
                    result["upload_verified"] = bool(upload_result.get("upload_verified", False))
                    result["sub2api_id"] = upload_result.get("account_id", "")
                    if upload_result.get("ok") and result["upload_verified"]:
                        print(f"  已上传到 SUB2API id={result.get('sub2api_id') or '?'}")
                    else:
                        result["upload_error"] = upload_result.get("error") or str(upload_result.get("_raw", upload_result))
                        print(f"  上传失败: {result['upload_error']}")
                except Exception as e:
                    print(f"  上传失败: {e}")
        else:
            print(f"  失败: {result.get('phone','?')} - {result.get('error','?')}")

    if ok_count < args.count:
        print(f"\n注意: 仅成功 {ok_count}/{args.count} (已达最大尝试次数)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = Path(args.output)
    output_path = raw.parent / f"{raw.stem}_{ts}{raw.suffix}"
    safe = [dict(r) for r in results if r.get("ok")]
    for item in safe:
        if item.get("password"):
            item["chatgpt_password"] = item.get("password", "")
    output_path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    credentials_path = output_path.parent / f"{raw.stem}_credentials_{ts}.jsonl"
    with credentials_path.open("w", encoding="utf-8") as f:
        for item in safe:
            f.write(json.dumps({
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "phone": item.get("phone", ""),
                "chatgpt_password": item.get("chatgpt_password", ""),
                "bind_email": item.get("bind_email", ""),
                "status": item.get("status", ""),
                "result_file": str(output_path),
            }, ensure_ascii=False) + "\n")
    print(f"\n已保存 {len(safe)} 条结果到 {output_path}")
    print(f"凭证索引已保存到 {credentials_path}")

if __name__ == "__main__":
    main()
