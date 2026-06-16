import unittest
from unittest.mock import patch
from phone_sms_adapter import UnifiedSMS, parse_countries


class _FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


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
class TestUnifiedSMSPriceCatalog(unittest.TestCase):
    @patch("phone_sms_adapter.requests.get")
    def test_get_price_catalog_returns_country_entries_for_all_prices(self, mock_get):
        mock_get.return_value = _FakeJsonResponse({
            "4": {
                "dr": {
                    "p1": {"price": 0.02, "count": 7, "name": "United States"},
                    "p2": {"price": 0.03, "count": 11, "name": "United States"},
                    "p3": {"price": 0.03, "count": 13, "name": "United States"},
                }
            },
            "16": {"dr": {"p4": {"price": 0.05}}},
        })
        sms = UnifiedSMS(provider="smsbower", api_key="test")

        rows = sms.get_price_catalog(service="dr")

        self.assertEqual(rows[0]["country"], "4")
        self.assertEqual(rows[0]["country_name"], "United States")
        self.assertEqual(rows[0]["lowest_price"], 0.02)
        self.assertEqual(rows[0]["second_price"], 0.03)
        self.assertEqual(rows[0]["lowest_count"], 7)
        self.assertEqual(rows[0]["second_count"], 24)
        self.assertEqual(rows[1]["country"], "16")
        self.assertEqual(rows[1]["second_price"], None)
    @patch("phone_sms_adapter.requests.get")
    def test_hero_sms_price_catalog_returns_all_country_prices(self, mock_get):
        mock_get.return_value = _FakeJsonResponse({
            "1": {"dr": {"cost": 0.1, "count": 574, "physicalCount": 268, "name": "Canada"}},
            "4": {"dr": {"cost": 0.025, "count": 5344, "physicalCount": 3277, "name": "United States"}},
        })
        sms = UnifiedSMS(provider="hero-sms", api_key="test")

        rows = sms.get_price_catalog(service="dr")

        self.assertEqual(
            rows,
            [
                {
                    "sms_provider": "hero-sms",
                    "country": "4",
                    "country_name": "United States",
                    "lowest_price": 0.025,
                    "second_price": None,
                    "lowest_count": 5344,
                    "second_count": 0,
                },
                {
                    "sms_provider": "hero-sms",
                    "country": "1",
                    "country_name": "Canada",
                    "lowest_price": 0.1,
                    "second_price": None,
                    "lowest_count": 574,
                    "second_count": 0,
                },
            ],
        )
    @patch("phone_sms_adapter.requests.get")
    def test_get_price_catalog_uses_smsbower_country_list_when_price_lacks_names(self, mock_get):
        mock_get.side_effect = [
            _FakeJsonResponse({
                "status": 1,
                "data": {
                    "4": {"id": 4, "eng": "United States", "chn": "美国"},
                },
            }),
            _FakeJsonResponse({
                "4": {"dr": {"p1": {"price": 0.02, "count": 7}}},
            }),
        ]
        sms = UnifiedSMS(provider="smsbower", api_key="test")

        rows = sms.get_price_catalog(service="dr")

        self.assertEqual(rows[0]["country_name"], "美国")

    @patch("phone_sms_adapter.requests.get")
    def test_hero_sms_price_catalog_uses_country_list_when_price_lacks_names(self, mock_get):
        mock_get.side_effect = [
            _FakeJsonResponse({
                "4": {"id": 4, "eng": "United States", "chn": "美国"},
            }),
            _FakeJsonResponse({
                "4": {"dr": {"cost": 0.025, "count": 5344}},
            }),
        ]
        sms = UnifiedSMS(provider="hero-sms", api_key="test")

        rows = sms.get_price_catalog(service="dr")

        self.assertEqual(rows[0]["country_name"], "美国")
    @patch("phone_sms_adapter.requests.get")
    def test_hero_sms_country_list_retries_without_key_when_keyed_call_fails(self, mock_get):
        mock_get.side_effect = [
            ConnectionError("reset"),
            _FakeJsonResponse({
                "4": {"id": 4, "eng": "United States", "chn": "美国"},
            }),
            _FakeJsonResponse({
                "4": {"dr": {"cost": 0.025, "count": 5344}},
            }),
        ]
        sms = UnifiedSMS(provider="hero-sms", api_key="test")

        rows = sms.get_price_catalog(service="dr")

        self.assertEqual(rows[0]["country_name"], "美国")


if __name__ == "__main__":
    unittest.main()
