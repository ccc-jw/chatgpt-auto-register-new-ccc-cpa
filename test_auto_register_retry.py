import unittest
from unittest.mock import patch

import auto_register as ar


class FakeSms:
    def __init__(self, failures_before_success=0):
        self.failures_before_success = failures_before_success
        self.get_number_calls = 0
        self.ready_called = False
        self.completed = False
        self.cancelled = False
        self.provider = "smsbower"

    def get_number(self, **kwargs):
        self.get_number_calls += 1
        if self.get_number_calls <= self.failures_before_success:
            raise RuntimeError("no numbers")
        return "aid-1", "1234567890"

    def set_ready(self):
        self.ready_called = True

    def wait_code(self, timeout):
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


if __name__ == "__main__":
    unittest.main()
