import unittest
from unittest.mock import patch
from phone_sms_adapter import UnifiedSMS, parse_countries


class TestParseCountries(unittest.TestCase):
    def test_single_string(self):
        self.assertEqual(parse_countries("151"), ["151"])

    def test_comma_string(self):
        self.assertEqual(parse_countries("151,52,6"), ["151", "52", "6"])

    def test_list(self):
        self.assertEqual(parse_countries(["151", "52"]), ["151", "52"])

    def test_empty_string(self):
        self.assertEqual(parse_countries(""), [])

    def test_none(self):
        self.assertEqual(parse_countries(None), [])


class TestUnifiedSMSInit(unittest.TestCase):
    def test_unsupported_provider(self):
        with self.assertRaises(ValueError):
            UnifiedSMS(provider="unsupported")

    def test_smsbower_url(self):
        sms = UnifiedSMS(provider="smsbower", api_key="test")
        self.assertEqual(sms._base_url, "https://smsbower.page/stubs/handler_api.php")

    def test_hero_sms_url(self):
        sms = UnifiedSMS(provider="hero-sms", api_key="test")
        self.assertEqual(sms._base_url, "https://hero-sms.com/stubs/handler_api.php")

    def test_hero_sms_operator(self):
        sms = UnifiedSMS(provider="hero-sms", api_key="test", operator="any")
        self.assertEqual(sms.operator, "any")


class TestUnifiedSMSGetNumber(unittest.TestCase):
    @patch("phone_sms_adapter.requests.get")
    def test_smsbower_success(self, mock_get):
        mock_get.return_value.text = "ACCESS_NUMBER:123:12345678"
        sms = UnifiedSMS(provider="smsbower", api_key="test")
        aid, phone = sms.get_number(service="dr", country="151")
        self.assertEqual(aid, "123")
        self.assertEqual(phone, "12345678")
        self.assertEqual(sms.activation_id, "123")

    @patch("phone_sms_adapter.requests.get")
    def test_hero_sms_success(self, mock_get):
        mock_get.return_value.text = "ACCESS_NUMBER:456:98765432"
        sms = UnifiedSMS(provider="hero-sms", api_key="test")
        aid, phone = sms.get_number(service="dr", country="52")
        self.assertEqual(aid, "456")
        self.assertEqual(phone, "98765432")

    @patch("phone_sms_adapter.requests.get")
    def test_hero_sms_with_operator(self, mock_get):
        mock_get.return_value.text = "ACCESS_NUMBER:789:11111111"
        sms = UnifiedSMS(provider="hero-sms", api_key="test", operator="any")
        sms.get_number(service="dr", country="6")
        call_args = mock_get.call_args
        self.assertIn("operator", call_args.kwargs.get("params", {}))


if __name__ == "__main__":
    unittest.main()
