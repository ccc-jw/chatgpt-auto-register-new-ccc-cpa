import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_register as ar


class FakeSms:
    def __init__(self, failures_before_success=0, country_failures=None, cheapest_prices=None, cheapest_errors=None):
        self.failures_before_success = failures_before_success
        self.country_failures = dict(country_failures or {})
        self.cheapest_prices = dict(cheapest_prices or {})
        self.cheapest_errors = set(cheapest_errors or [])
        self.get_number_calls = 0
        self.calls_by_country = {}
        self.number_call_kwargs = []
        self.ready_called = False
        self.completed = False
        self.cancelled = False
        self.provider = "smsbower"

    def get_cheapest_provider(self, service="dr", country="151"):
        country = str(country)
        if country in self.cheapest_errors:
            raise RuntimeError("price query failed")
        return self.cheapest_prices.get(country, (f"p{country}", 0.01))

    def get_number(self, **kwargs):
        self.get_number_calls += 1
        self.number_call_kwargs.append(dict(kwargs))
        country = str(kwargs.get("country", ""))
        self.calls_by_country[country] = self.calls_by_country.get(country, 0) + 1
        if country in self.country_failures:
            if self.calls_by_country[country] <= self.country_failures[country]:
                raise RuntimeError("no numbers")
            return f"aid-{country}", f"123456789{country[-1:]}"
        if self.get_number_calls <= self.failures_before_success:
            raise RuntimeError("no numbers")
        return "aid-1", "1234567890"

    def set_ready(self):
        self.ready_called = True

    def wait_code(self, timeout, stop_requested=None):
        return "123456"

    def complete(self):
        self.completed = True

    def cancel(self):
        self.cancelled = True


class FakeRegister:
    def __init__(self, proxy=""):
        self.proxy = proxy

    def visit(self):
        return None

    def get_csrf(self):
        return "csrf"

    def signin(self, phone, csrf):
        return "redirect"

    def jump_to_auth(self, redirect):
        return None

    def register_user(self, phone, password):
        return {"continue_url": "https://example.com/continue"}

    def send_otp(self, continue_url):
        return None

    def validate_otp(self, code):
        return {"continue_url": "https://example.com/about-you"}

    def visit_about_you(self, continue_url):
        return None

    def create_account(self, name, birthdate):
        return {"continue_url": "https://example.com/callback"}

    def oauth_callback(self, callback_url):
        return "session-token"

    def get_access_token(self):
        return "access-token"


class AutoRegisterRetryTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "sms": {
                "provider": "smsbower",
                "api_key": "test",
                "countries": ["33"],
                "service": "dr",
                "operator": "any",
                "max_price": "",
            },
            "register": {
                "password": "pw123456",
                "name": "Alice Smith",
                "birthdate": "1999-01-02",
            },
            "proxy": "",
            "code_timeout": 30,
        }

    def test_register_one_keeps_retrying_phone_acquisition_until_success(self):
        sms = FakeSms(failures_before_success=3)

        with patch.object(ar, "ChatGPTRegister", FakeRegister), patch.object(ar._time, "sleep", return_value=None):
            result = ar.register_one(sms, self.config, verbose=False, no_phase2=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["phone"], "+1234567890")
        self.assertEqual(sms.get_number_calls, 4)
        self.assertTrue(sms.ready_called)
        self.assertTrue(sms.completed)
        # Verify status_version=2 fields
        self.assertEqual(result["status_version"], 2)
        self.assertTrue(result["phone_ok"])
        self.assertTrue(result["token_ok"])
        self.assertTrue(result["final_ok"])
        self.assertEqual(result["status"], "final_ok")

    def test_phone_retry_can_be_interrupted_by_stop_request(self):
        sms = FakeSms(failures_before_success=999999)
        stop_checks = {"count": 0}

        def stop_requested():
            stop_checks["count"] += 1
            return stop_checks["count"] >= 2

        with patch.object(ar._time, "sleep", return_value=None):
            with self.assertRaises(ar.StopRequested):
                ar._get_number_with_retry(
                    sms,
                    service="dr",
                    countries=["33"],
                    stop_requested=stop_requested,
                    verbose=False,
                )

        self.assertEqual(sms.get_number_calls, 1)

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

    def test_phone_retry_rotates_after_100_failures_per_country(self):
        sms = FakeSms(country_failures={"151": 100, "4": 0})

        with patch.object(ar._time, "sleep", return_value=None):
            aid, phone, country = ar._get_number_with_retry(
                sms,
                service="dr",
                countries=["151", "4"],
                verbose=False,
            )

        self.assertEqual(country, "4")
        self.assertEqual(aid, "aid-4")
        self.assertEqual(phone, "1234567894")
        self.assertEqual(sms.calls_by_country["151"], 100)
        self.assertEqual(sms.calls_by_country["4"], 1)

    def test_phone_retry_cycles_back_to_first_country_after_all_countries_fail(self):
        sms = FakeSms(country_failures={"151": 100, "4": 100, "16": 100})

        with patch.object(ar._time, "sleep", return_value=None):
            aid, phone, country = ar._get_number_with_retry(
                sms,
                service="dr",
                countries=["151", "4", "16"],
                verbose=False,
            )

        self.assertEqual(country, "151")
        self.assertEqual(aid, "aid-151")
        self.assertEqual(phone, "1234567891")
        self.assertEqual(sms.calls_by_country["151"], 101)
        self.assertEqual(sms.calls_by_country["4"], 100)
        self.assertEqual(sms.calls_by_country["16"], 100)

    def test_phone_retry_uses_cheapest_provider_and_price_per_country(self):
        sms = FakeSms(
            country_failures={"151": 1, "4": 0},
            cheapest_prices={"151": ("p151", 0.02), "4": ("p4", 0.015)},
        )

        with patch.object(ar._time, "sleep", return_value=None):
            aid, phone, country = ar._get_number_with_retry(
                sms,
                service="dr",
                countries=["151", "4"],
                verbose=False,
            )

        self.assertEqual(country, "151")
        self.assertEqual(aid, "aid-151")
        self.assertEqual(sms.number_call_kwargs[0]["provider_ids"], "p151")
        self.assertEqual(sms.number_call_kwargs[0]["max_price"], "0.02")
        self.assertEqual(sms.number_call_kwargs[1]["provider_ids"], "p151")
        self.assertEqual(sms.number_call_kwargs[1]["max_price"], "0.02")

    def test_phone_retry_skips_country_when_cheapest_price_query_fails(self):
        sms = FakeSms(
            country_failures={"4": 0},
            cheapest_prices={"4": ("p4", 0.015)},
            cheapest_errors={"151"},
        )

        with patch.object(ar._time, "sleep", return_value=None):
            aid, phone, country = ar._get_number_with_retry(
                sms,
                service="dr",
                countries=["151", "4"],
                verbose=False,
            )

        self.assertEqual(country, "4")
        self.assertEqual(aid, "aid-4")
        self.assertNotIn("151", sms.calls_by_country)
        self.assertEqual(sms.number_call_kwargs[0]["provider_ids"], "p4")
        self.assertEqual(sms.number_call_kwargs[0]["max_price"], "0.015")


if __name__ == "__main__":
    unittest.main()
