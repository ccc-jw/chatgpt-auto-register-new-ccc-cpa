"""
Unified SMS adapter — compatible with the existing auto_register.register_one() call pattern.
Supports smsbower and hero-sms providers with multi-country rotation.
"""
import time
import re
import json
from typing import Optional, Tuple, List

import requests


class UnifiedSMS:
    """
    Exposes the same interface that auto_register.py expects:
      balance(), get_cheapest_provider(), get_number(), set_ready(), wait_code(), complete(), cancel()
    Internally dispatches to the selected provider.
    """

    def __init__(self, provider: str = "smsbower", api_key: str = "", operator: str = "any"):
        if provider not in ("smsbower", "hero-sms"):
            raise ValueError(f"Unsupported SMS provider: {provider}")
        self.provider = provider
        self.api_key = api_key
        self.operator = operator
        self.activation_id: Optional[str] = None
        self.phone: Optional[str] = None
        self._base_url = (
            "https://smsbower.page/stubs/handler_api.php"
            if provider == "smsbower"
            else "https://hero-sms.com/stubs/handler_api.php"
        )

    def _call(self, params: dict) -> str:
        params["api_key"] = self.api_key
        r = requests.get(self._base_url, params=params, timeout=30)
        text = r.text.strip()
        if text.startswith("{") and "message" in text:
            try:
                data = json.loads(text)
                if data.get("message") == "No access":
                    raise RuntimeError(f"{self.provider}: API key invalid or no access")
            except json.JSONDecodeError:
                pass
        return text

    def balance(self) -> str:
        return self._call({"action": "getBalance"})

    def get_cheapest_provider(self, service: str = "dr", country: str = "151") -> Tuple[str, float]:
        """Only supported for smsbower. For hero-sms, returns ('', 0)."""
        if self.provider == "hero-sms":
            return "", 0.0
        r = requests.get(
            self._base_url,
            params={"api_key": self.api_key, "action": "getPricesV3", "service": service, "country": country},
            timeout=15,
        )
        data = r.json()
        providers = data.get(country, {}).get(service, {})
        cheapest, cheapest_price = "", 999.0
        for pid, info in providers.items():
            price = float(info.get("price", 999))
            if price < cheapest_price:
                cheapest_price = price
                cheapest = pid
        return cheapest, cheapest_price

    def get_number(
        self,
        service: str = "dr",
        country: str = "151",
        provider_ids: str = "",
        max_price: str = "",
    ) -> Tuple[str, str]:
        params: dict = {"action": "getNumber", "service": service, "country": country}
        if self.provider == "smsbower":
            if provider_ids:
                params["providerIds"] = provider_ids
            if max_price:
                params["maxPrice"] = max_price
        else:  # hero-sms
            if self.operator:
                params["operator"] = self.operator
            if max_price:
                params["maxPrice"] = max_price
                params["fixedPrice"] = "true"
        resp = self._call(params)
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            aid, phone = parts[1], parts[2]
            self.activation_id = aid
            self.phone = phone
            return aid, phone
        # Try to classify error
        if resp.startswith("NO_BALANCE") or resp.startswith("BAD_KEY") or resp.startswith("NO_NUMBERS") or resp.startswith("WRONG_MAX_PRICE") or resp.startswith("BANNED"):
            raise RuntimeError(f"{self.provider} error: {resp}")
        raise RuntimeError(f"{self.provider} getNumber failed: {resp}")

    def set_ready(self):
        """Notify platform that we're ready to receive SMS."""
        try:
            self._call({"action": "setStatus", "status": "1", "id": self.activation_id})
        except Exception:
            pass  # hero-sms may not need explicit set_ready

    def wait_code(self, timeout: int = 300, interval: int = 3) -> Optional[str]:
        if not self.activation_id:
            raise RuntimeError("No active activation")
        started = time.time()
        while time.time() - started < timeout:
            resp = self._call({"action": "getStatus", "id": self.activation_id})
            if resp.startswith("STATUS_OK:"):
                code = resp.split(":", 1)[1].strip()
                # For hero-sms, extract 4-8 digit code if the response contains extra text
                if self.provider == "hero-sms" and len(code) > 8:
                    m = re.search(r"\b(\d{4,8})\b", code)
                    if m:
                        code = m.group(1)
                return code
            elif resp == "STATUS_CANCEL":
                raise RuntimeError("Activation cancelled (may have timed out)")
            # STATUS_WAIT_CODE, STATUS_WAIT_RETRY, STATUS_WAIT_RESEND → continue waiting
            time.sleep(interval)
        return None

    def complete(self):
        self._call({"action": "setStatus", "status": "6", "id": self.activation_id})

    def cancel(self):
        try:
            self._call({"action": "setStatus", "status": "8", "id": self.activation_id})
        except Exception:
            pass


def parse_countries(value) -> List[str]:
    """Parse country config into a list. Supports string, comma-separated string, or list."""
    if isinstance(value, list):
        return [str(c).strip() for c in value if str(c).strip()]
    if isinstance(value, str):
        parts = [c.strip() for c in value.split(",") if c.strip()]
        return parts if parts else [value.strip()] if value.strip() else []
    if value:
        return [str(value)]
    return []
