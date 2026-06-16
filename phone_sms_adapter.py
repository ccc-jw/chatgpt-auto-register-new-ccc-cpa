"""
Unified SMS adapter — compatible with the existing auto_register.register_one() call pattern.
Supports smsbower and hero-sms providers with multi-country rotation.
"""
import time
import re
import json
from typing import Optional, Tuple, List

import requests


class StopRequested(RuntimeError):
    """Raised when an external stop signal interrupts SMS polling."""


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

    def _call(self, params: dict, timeout: int = 30) -> str:
        params["api_key"] = self.api_key
        r = requests.get(self._base_url, params=params, timeout=timeout, verify=True)
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
        except Exception as e:
            # hero-sms may not need explicit set_ready; smsbower requires it
            # Log but don't fail — OTP wait will timeout if set_ready truly failed
            pass

    def wait_code(self, timeout: int = 300, interval: int = 3, stop_requested=None) -> Optional[str]:
        if not self.activation_id:
            raise RuntimeError("No active activation")
        started = time.time()
        while time.time() - started < timeout:
            if stop_requested and stop_requested():
                self.cancel()
                raise StopRequested("stopped while waiting for SMS code")
            try:
                resp = self._call({"action": "getStatus", "id": self.activation_id}, timeout=30)
            except StopRequested:
                self.cancel()
                raise
            if resp.startswith("STATUS_OK:"):
                code = resp.split(":", 1)[1].strip()
                if self.provider == "hero-sms" and len(code) > 8:
                    m = re.search(r"\b(\d{4,8})\b", code)
                    if m:
                        code = m.group(1)
                return code
            elif resp == "STATUS_CANCEL":
                raise RuntimeError("Activation cancelled (may have timed out)")
            # Interruptible sleep
            sleep_start = time.time()
            while time.time() - sleep_start < interval:
                if stop_requested and stop_requested():
                    self.cancel()
                    raise StopRequested("stopped during SMS wait sleep")
                time.sleep(min(0.5, interval - (time.time() - sleep_start)))
        return None

    def complete(self):
        self._call({"action": "setStatus", "status": "6", "id": self.activation_id})

    def cancel(self):
        try:
            self._call({"action": "setStatus", "status": "8", "id": self.activation_id})
        except Exception:
            pass

    def _extract_country_name(self, *items) -> str:
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("country_name", "countryName", "chn", "eng", "name", "title", "rus"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    def _country_names(self) -> dict[str, str]:
        payloads = [
            {"api_key": self.api_key, "action": "getCountries"},
            {"action": "getCountries"},
        ]
        data = None
        for params in payloads:
            try:
                r = requests.get(
                    self._base_url,
                    params=params,
                    timeout=15,
                    verify=True,
                )
                data = r.json()
                break
            except Exception:
                continue
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            return {}
        names = {}
        for country, info in data.items():
            name = self._extract_country_name(info)
            if name:
                names[str(country)] = name
        return names

    def get_price_catalog(self, service: str = "dr") -> List[dict]:
        country_names = self._country_names()
        if self.provider == "smsbower":
            r = requests.get(
                self._base_url,
                params={"api_key": self.api_key, "action": "getPricesV3", "service": service},
                timeout=15,
                verify=True,
            )
            data = r.json()
            rows = []
            for country, services in data.items():
                providers = (services or {}).get(service, {})
                price_counts = {}
                country_name = self._extract_country_name(services) or country_names.get(str(country), "")
                for info in providers.values():
                    if not country_name:
                        country_name = self._extract_country_name(info) or country_names.get(str(country), "")
                    try:
                        price = float(info.get("price", 999))
                    except (TypeError, ValueError):
                        continue
                    if price <= 0 or price >= 999:
                        continue
                    try:
                        count = int(info.get("count", 0) or 0)
                    except (TypeError, ValueError):
                        count = 0
                    price_counts[price] = price_counts.get(price, 0) + count
                if not price_counts:
                    continue
                unique_prices = sorted(price_counts.keys())
                lowest_price = unique_prices[0]
                second_price = unique_prices[1] if len(unique_prices) > 1 else None
                rows.append(
                    {
                        "sms_provider": self.provider,
                        "country": str(country),
                        "country_name": country_name,
                        "lowest_price": lowest_price,
                        "second_price": second_price,
                        "lowest_count": price_counts.get(lowest_price, 0),
                        "second_count": price_counts.get(second_price, 0) if second_price is not None else 0,
                    }
                )
            rows.sort(key=lambda row: (row["lowest_price"], row["country"]))
            return rows
        if self.provider == "hero-sms":
            r = requests.get(
                self._base_url,
                params={"api_key": self.api_key, "action": "getPrices", "service": service},
                timeout=15,
                verify=True,
            )
            data = r.json()
            rows = []
            for country, services in data.items():
                info = (services or {}).get(service, {})
                try:
                    price = float(info.get("cost", 0))
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                rows.append(
                    {
                        "sms_provider": self.provider,
                        "country": str(country),
                        "country_name": self._extract_country_name(info, services) or country_names.get(str(country), ""),
                        "lowest_price": price,
                        "second_price": None,
                        "lowest_count": int(info.get("count", 0) or 0),
                        "second_count": 0,
                    }
                )
            rows.sort(key=lambda row: (row["lowest_price"], row["country"]))
            return rows
        return []

    def get_sorted_countries_by_price(self, countries: List[str], service: str = "dr") -> List[dict]:
        """Query price for each country and return sorted by cheapest price first.
        Returns list of [{"country": "4", "price": 0.025}, ...] sorted ascending.
        Countries that fail to query are silently skipped.
        Falls back to original order if no prices can be fetched.
        """
        priced = []
        for c in countries:
            try:
                if self.provider == "smsbower":
                    _, price = self.get_cheapest_provider(service=service, country=c)
                    if price < 999.0:
                        priced.append({"country": c, "price": price})
                elif self.provider == "hero-sms":
                    r = requests.get(
                        self._base_url,
                        params={"api_key": self.api_key, "action": "getPrices", "service": service, "country": c},
                        timeout=15,
                    )
                    data = r.json()
                    cost = data.get(c, {}).get(service, {}).get("cost")
                    if cost is not None:
                        priced.append({"country": c, "price": float(cost)})
            except Exception:
                continue

        if not priced:
            return [{"country": c, "price": 0.0} for c in countries]

        priced.sort(key=lambda x: x["price"])
        return priced


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
