import json
import tempfile
from pathlib import Path
import threading
import unittest
from unittest import mock

import web_gui

CONFIG_FILE = Path(web_gui.__file__).with_name("config.json")


class WebGuiStatsTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = dict(web_gui._state)
        self._config_existed = CONFIG_FILE.exists()
        self._config_backup = CONFIG_FILE.read_text(encoding="utf-8") if self._config_existed else None

    def tearDown(self):
        web_gui._state.clear()
        web_gui._state.update(self._saved_state)
        if self._config_existed:
            CONFIG_FILE.write_text(self._config_backup, encoding="utf-8")
        elif CONFIG_FILE.exists():
            CONFIG_FILE.unlink()

    def test_api_status_includes_current_and_total_stats(self):
        web_gui._state["running"] = True
        web_gui._state["results"] = [{"ok": True}, {"ok": False}]
        web_gui._state["stats"] = {
            "current_success": 1,
            "current_fail": 1,
            "total_success": 5,
            "total_fail": 3,
        }

        with web_gui.app.test_client() as client:
            resp = client.get("/api/status")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(
            data["stats"],
            {
                "current_success": 1,
                "current_fail": 1,
                "total_success": 5,
                "total_fail": 3,
            },
        )

    def test_log_writer_keeps_buffers_separate_per_thread(self):
        entries = []
        writer = web_gui._LogWriter(
            lambda msg, tag="info", thread_id=None: entries.append(
                {"msg": msg, "tag": tag, "thread": thread_id}
            )
        )
        ready = threading.Barrier(2)

        def worker(thread_id, part1, part2):
            writer.bind_thread(thread_id)
            try:
                writer.write(part1)
                ready.wait(timeout=2)
                writer.write(part2)
                writer.flush()
            finally:
                writer.unbind_thread()

        t1 = threading.Thread(target=worker, args=(1, "A", "1\n"), daemon=True)
        t2 = threading.Thread(target=worker, args=(2, "B", "2\n"), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        self.assertEqual(
            entries,
            [
                {"msg": "B2", "tag": "info", "thread": 2},
                {"msg": "A1", "tag": "info", "thread": 1},
            ],
        )

    def test_parallel_country_order_keeps_cheapest_country_first_for_every_thread(self):
        countries = ["16", "4", "33", "151"]

        orders = [
            web_gui._country_order_for_attempt(countries, attempt_index=i, concurrency=3)
            for i in range(3)
        ]

        self.assertEqual(orders, [countries, countries, countries])

    def test_run_counts_phase2_final_success_toward_target(self):
        config = {
            "sms": {
                "provider": "smsbower",
                "api_key": "test",
                "countries": ["151"],
                "service": "dr",
            },
            "sub2api": {"url": "https://sub2api.example.com", "email": "admin@example.com"},
            "upload_target": "sub2api",
            "bind_email": "target@example.com",
        }
        register_results = [
            {
                "ok": True,
                "phone": f"+1555000000{i}",
                "phone_ok": True,
                "final_ok": False,
                "status": "phone_ok",
                "session_token": f"session-{i}",
                "password": "pw",
            }
            for i in range(10)
        ]

        with mock.patch("web_gui.UnifiedSMS") as sms_cls:
            sms_cls.return_value.balance.return_value = "1.00"
            with mock.patch("web_gui.ar.register_one", side_effect=register_results) as register_one:
                with mock.patch.object(web_gui, "_phase2_for_result", return_value={"ok": True, "uploaded": True, "upload_verified": True, "sub2api_account_id": "sub-1", "upload_target": "sub2api"}):
                    with mock.patch.object(web_gui, "_save_result"):
                        with mock.patch.object(web_gui, "_log"):
                            web_gui._run(config, count=2, retries=0, concurrency=1)

        self.assertEqual(register_one.call_count, 2)
        self.assertEqual(web_gui._state["stats"]["current_success"], 2)
        self.assertEqual(web_gui._state["stats"]["total_success"], 2)

        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "sms": {
                        "provider": "smsbower",
                        "api_key": "test",
                        "countries": ["16"],
                        "service": "dr",
                    },
                    "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json={"countries": "16"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        cfg = data["config"]
        self.assertEqual(cfg["sms"]["max_price"], "0.03")
        self.assertEqual(cfg["sms_max_price"], "0.03")

    def test_api_config_uses_posted_max_price_when_provided(self):
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "sms": {
                        "provider": "smsbower",
                        "api_key": "test",
                        "countries": ["16"],
                        "service": "dr",
                    },
                    "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json={"countries": "16", "max_price": "0.01"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        cfg = data["config"]
        self.assertEqual(cfg["sms"]["max_price"], "0.01")
        self.assertEqual(cfg["sms_max_price"], "0.01")

        payload = {
            "plus_method": "paypal",
            "plus_email": "pay@example.com",
            "plus_phone": "+6281234567890",
            "plus_pin": "123456",
            "plus_country": "ID",
            "plus_currency": "IDR",
        }

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json=payload)
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])

            resp = client.get("/api/config")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        cfg = data["config"]
        self.assertEqual(cfg["plus_method"], "paypal")
        self.assertEqual(cfg["plus_email"], "pay@example.com")
        self.assertEqual(cfg["plus_phone"], "+6281234567890")
        self.assertEqual(cfg["plus_pin"], "123456")
        self.assertEqual(cfg["plus_country"], "ID")
        self.assertEqual(cfg["plus_currency"], "IDR")

    def test_api_config_roundtrips_outlook_pool_text(self):
        payload = {
            "email_provider": "outlook",
            "outlook_pool": "a@outlook.com----pw----cid----rt",
        }

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json=payload)
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])

            resp = client.get("/api/config")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        cfg = data["config"]
        self.assertEqual(cfg["email_provider"], "outlook")
        self.assertEqual(cfg["outlook_pool"], "a@outlook.com----pw----cid----rt")

    def test_monitor_anomaly_notifications_are_rate_limited_when_summary_changes(self):
        web_gui._state["_last_monitor_notify_key"] = ""
        web_gui._state["_last_monitor_notify_at"] = 0
        payload = {"running": True, "stats": {"total_success": 0, "total_fail": 0}, "results": []}

        with mock.patch.object(web_gui.time, "time", side_effect=[1000, 1001]), mock.patch.object(web_gui, "_send_feishu") as send:
            web_gui._monitor_anomalies_from_payload(payload, [{"tag": "error", "msg": "验证码超时 1"}], source="日志监控")
            web_gui._monitor_anomalies_from_payload(payload, [{"tag": "error", "msg": "验证码超时 2"}], source="日志监控")

        self.assertEqual(send.call_count, 1)

    def test_monitor_anomaly_ignores_register_rejected_status_400(self):
        web_gui._state["_last_monitor_notify_key"] = ""
        web_gui._state["_last_monitor_notify_at"] = 0
        payload = {
            "running": True,
            "stats": {"total_success": 0, "total_fail": 1},
            "results": [
                {
                    "country": "4",
                    "error": "注册被拒(status=400)",
                    "failure_stage": "register_rejected",
                    "status": "register_failed",
                }
            ],
        }

        with mock.patch.object(web_gui, "_send_feishu") as send:
            web_gui._monitor_anomalies_from_payload(
                payload,
                [{"tag": "error", "msg": "provider=hero-sms country=4 失败: 注册被拒(status=400)"}],
                source="日志监控",
            )

        send.assert_not_called()

    def test_monitor_anomaly_ignores_no_numbers(self):
        web_gui._state["_last_monitor_notify_key"] = ""
        web_gui._state["_last_monitor_notify_at"] = 0
        payload = {
            "running": True,
            "stats": {"total_success": 0, "total_fail": 1},
            "results": [
                {
                    "country": "4",
                    "error": "hero-sms error: NO_NUMBERS",
                    "failure_stage": "get_phone",
                    "status": "phone_failed",
                }
            ],
        }

        with mock.patch.object(web_gui, "_send_feishu") as send:
            web_gui._monitor_anomalies_from_payload(
                payload,
                [{"tag": "error", "msg": "provider=hero-sms country=4 失败 (hero-sms error: NO_NUMBERS)"}],
                source="日志监控",
            )

        send.assert_not_called()


    def test_main_page_has_save_config_button(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("保存配置", html)
        self.assertIn("onclick=\"saveConfigOnly()\"", html)

    def test_main_page_has_in_page_sms_price_check_view(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("id=\"view-sms-price-check\"", html)
        self.assertIn("id=\"nav-sms-price-check\"", html)
        self.assertIn("switchView(&quot;sms-price-check&quot;)", html)
        self.assertNotIn("location.href='/sms-price-check'", html)
        self.assertIn("function loadSmsPriceCheck", html)
        self.assertIn("<select data-filter=\"sms_provider\"", html)
        self.assertIn("<select data-filter=\"country\"", html)
        self.assertNotIn("data-filter=\"in_purchase_pool\"", html)
        self.assertNotIn("data-filter=\"recommendation\"", html)
        self.assertNotIn("data-filter=\"success_price\"", html)
        self.assertNotIn("data-filter=\"success_count\"", html)

    def test_sms_price_check_page_exists_with_two_filterable_tables(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/sms-price-check")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("短信价格检查", html)
        self.assertIn("触发价格检查", html)
        self.assertIn("国家/地区id", html)
        self.assertIn("国家/地区名称", html)
        self.assertIn("成功价格统计", html)
        self.assertIn("data-filter=\"sms_provider\"", html)
        self.assertIn("data-filter=\"country\"", html)
        self.assertIn("<select data-filter=\"sms_provider\"", html)
        self.assertIn("<select data-filter=\"country\"", html)
        self.assertIn("data-sort=\"number\"", html)
        self.assertNotIn("lowest_price\" placeholder=\"筛选\"", html)
        self.assertNotIn("second_price\" placeholder=\"筛选\"", html)
        self.assertNotIn("lowest_count\" placeholder=\"筛选\"", html)
        self.assertNotIn("second_count\" placeholder=\"筛选\"", html)
        self.assertNotIn("data-filter=\"in_purchase_pool\"", html)
        self.assertNotIn("data-filter=\"recommendation\"", html)
        self.assertNotIn("data-filter=\"success_price\"", html)
        self.assertNotIn("data-filter=\"success_count\"", html)

    def test_main_page_sms_price_tables_use_compact_header_filters(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn(".sms-price-table th{background:rgba(243,239,228,.85);", html)
        self.assertIn("font-weight:500", html)
        self.assertIn(".sms-price-table th select{display:inline-block;width:92px", html)
        self.assertIn("margin:0 0 0 8px", html)
        self.assertIn("padding:3px 18px 3px 8px", html)
        self.assertIn(".nav-links{display:flex;align-items:center;margin-left:auto", html)
        self.assertIn(".nav-secondary{display:flex", html)
        self.assertIn("id=\"nav-download-results\"", html)

    def test_sms_price_check_api_hides_sensitive_config(self):
        web_gui._state["config"] = {
            "sms": {
                "provider": "smsbower",
                "api_key": "secret-sms-key",
                "countries": ["4"],
                "service": "dr",
                "max_price": "0.03",
            },
            "sub2api": {"pwd": "secret-sub2api-password"},
        }

        class FakeSMS:
            def __init__(self, provider, api_key):
                self.provider = provider
                self.api_key = api_key

            def get_price_catalog(self, service="dr"):
                return [
                    {
                        "sms_provider": self.provider,
                        "country": "4",
                        "country_name": "United States",
                        "lowest_price": 0.02,
                        "second_price": 0.03,
                        "lowest_count": 1,
                        "second_count": 2,
                    }
                ]

        with mock.patch.object(web_gui, "UnifiedSMS", FakeSMS):
            with mock.patch.object(web_gui, "load_success_price_rows", return_value=[]):
                with web_gui.app.test_client() as client:
                    resp = client.get("/api/sms-price-check")

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-sms-key", raw)
        self.assertNotIn("secret-sub2api-password", raw)
        self.assertEqual(payload["configured_countries"]["smsbower"], ["4"])
        self.assertEqual(payload["price_rows"][0]["recommendation"], "已在池中")
        self.assertEqual(payload["price_rows"][0]["country_name"], "United States")
        self.assertEqual(payload["success_rows"], [])

    def test_api_config_saves_only_active_sms_provider(self):
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "sms": {
                        "active_provider": "hero-sms",
                        "providers": {
                            "hero-sms": {"api_key": "hero-old", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.025"},
                            "smsbower": {"api_key": "bower-old", "countries": ["151"], "service": "dr", "operator": "any", "max_price": "0.03"},
                        },
                    },
                    "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
                },
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        with web_gui.app.test_client() as client:
            resp = client.post("/api/config", json={"sms_provider": "smsbower", "api_key": "bower-new", "countries": "33,151", "max_price": "0.02"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

        sms = data["config"]["sms"]
        self.assertEqual(sms["active_provider"], "smsbower")
        self.assertEqual(sms["provider"], "smsbower")
        self.assertEqual(sms["api_key"], "bower-new")
        self.assertEqual(sms["countries"], ["33", "151"])
        self.assertEqual(sms["providers"]["smsbower"]["api_key"], "bower-new")
        self.assertEqual(sms["providers"]["smsbower"]["countries"], ["33", "151"])
        self.assertEqual(sms["providers"]["hero-sms"]["api_key"], "hero-old")
        self.assertEqual(sms["providers"]["hero-sms"]["countries"], ["4"])

    def test_main_page_navigation_keeps_primary_menu_visible_and_download_right(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("<div class=\"nav-primary\"", html)
        self.assertIn("<div class=\"nav-secondary\"", html)
        self.assertIn("id=\"nav-download-results\"", html)
        self.assertIn(".nav-primary{display:flex", html)
        self.assertIn(".nav-secondary{display:flex", html)
        self.assertIn(".nav-links{display:flex;align-items:center;margin-left:auto", html)
        self.assertIn("<span class=\"brand-meta\" id=\"status-msg\">就绪</span>\n    <nav class=\"nav-links\">", html)
        self.assertLess(html.index("id=\"status-msg\""), html.index("<nav class=\"nav-links\">"))
        self.assertLess(html.index("<nav class=\"nav-links\">"), html.index("id=\"nav-download-results\""))
        self.assertNotIn("nav-spacer", html)

    def test_main_page_has_provider_switch_loader(self):
        with web_gui.app.test_client() as client:
            resp = client.get("/")

        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("function applySmsProviderConfig", html)
        self.assertIn("onchange=\"applySmsProviderConfig()\"", html)

    def test_sms_price_check_api_queries_all_configured_sms_providers(self):
        web_gui._state["config"] = {
            "sms": {
                "active_provider": "hero-sms",
                "provider": "hero-sms",
                "providers": {
                    "hero-sms": {"api_key": "hero-key", "countries": ["4"], "service": "dr", "operator": "any", "max_price": "0.03"},
                    "smsbower": {"api_key": "bower-key", "countries": ["151"], "service": "dr", "operator": "any", "max_price": "0.02"},
                },
            }
        }

        class FakeSMS:
            def __init__(self, provider, api_key):
                self.provider = provider
                self.api_key = api_key

            def get_price_catalog(self, service="dr"):
                return [{"sms_provider": self.provider, "country": "4" if self.provider == "hero-sms" else "151", "lowest_price": 0.02, "second_price": None, "lowest_count": 3, "second_count": 0}]

        with mock.patch.object(web_gui, "UnifiedSMS", FakeSMS):
            with mock.patch.object(web_gui, "load_success_price_rows", return_value=[]):
                with web_gui.app.test_client() as client:
                    resp = client.get("/api/sms-price-check")

        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        providers = {row["sms_provider"] for row in payload["price_rows"]}
        self.assertEqual(providers, {"hero-sms", "smsbower"})
        by_provider = {row["sms_provider"]: row for row in payload["price_rows"]}
        self.assertTrue(by_provider["hero-sms"]["in_purchase_pool"])
        self.assertTrue(by_provider["smsbower"]["in_purchase_pool"])


    def test_auto_retry_phase2_on_startup_skips_when_config_disables_it(self):
        web_gui._state["config"] = {"phase2_auto_skip": False}
        entries = []

        with mock.patch.object(web_gui, "_find_pending_phase2_items", return_value=["one.json"]):
            with mock.patch.object(web_gui.threading, "Thread") as thread_cls:
                with mock.patch.object(web_gui, "_log", side_effect=lambda msg, tag="info", thread_id=None: entries.append((msg, tag))):
                    web_gui._auto_retry_phase2_on_startup()

        thread_cls.assert_not_called()
        self.assertIn(("[自动补跑] 已关闭，跳过历史 Phase2 补跑", "info"), entries)

    def test_auto_retry_phase2_on_startup_runs_when_config_enables_it(self):
        web_gui._state["config"] = {
            "phase2_auto_skip": True,
            "email_provider": "outlook",
        }

        with mock.patch.object(web_gui, "_find_pending_phase2_items", return_value=["one.json"]):
            with mock.patch.object(web_gui.threading, "Thread") as thread_cls:
                thread = thread_cls.return_value
                web_gui._auto_retry_phase2_on_startup()

        thread_cls.assert_called_once()
        thread.start.assert_called_once()

    def test_run_cleans_up_running_state_when_phase2_flags_are_missing(self):
        config = {
            "sms": {
                "provider": "smsbower",
                "api_key": "test",
                "countries": ["16"],
                "service": "dr",
            },
            "register": {"password": "", "name": "A", "birthdate": "2000-01-01"},
            "no_phase2": True,
        }

        web_gui._state["running"] = True
        web_gui._state["stop"] = False

        with mock.patch("web_gui.UnifiedSMS") as sms_cls:
            sms_cls.return_value.balance.return_value = "ACCESS_BALANCE:1"
            with mock.patch("web_gui.ar.register_one", return_value={"ok": True, "phone": "+15551234567", "phone_ok": True, "final_ok": True, "status": "final_ok"}) as register_one:
                web_gui._run(config, count=1, retries=0, concurrency=1)

        self.assertFalse(web_gui._state["running"])
        register_one.assert_called_once()

    def test_run_saves_phone_ok_result_when_phase2_is_configured(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        all_path = results_dir / "_all.json"
        before_all = all_path.read_text(encoding="utf-8") if all_path.exists() else None
        phone = "+15550001111"
        try:
            results_dir.mkdir(exist_ok=True)
            for path in results_dir.glob("15550001111_*.json"):
                path.unlink()

            config = {
                "sms": {
                    "provider": "smsbower",
                    "api_key": "test",
                    "countries": ["16"],
                    "service": "dr",
                },
                "register": {"password": "pw", "name": "A", "birthdate": "2000-01-01"},
                "bind_email": "target@example.com",
                "upload_target": "sub2api",
                "sub2api": {"url": "https://sub.example.com", "email": "owner@example.com"},
            }
            result = {
                "ok": True,
                "phone_ok": True,
                "final_ok": False,
                "retryable": True,
                "status": "phone_ok",
                "phone": phone,
                "password": "pw",
                "session_token": "session-token",
                "access_token": "access-token",
            }

            web_gui._state["running"] = True
            web_gui._state["stop"] = False
            web_gui._state["stats"] = web_gui._empty_stats()

            with mock.patch("web_gui.UnifiedSMS") as sms_cls:
                sms_cls.return_value.balance.return_value = "ACCESS_BALANCE:1"
                with mock.patch("web_gui.ar.register_one", return_value=result):
                    with mock.patch.object(web_gui, "_phase2_for_result", return_value={"ok": False, "error": "phase2_failed"}):
                        web_gui._run(config, count=1, retries=0, concurrency=1)

            saved_files = list(results_dir.glob("15550001111_*.json"))
            self.assertGreaterEqual(len(saved_files), 1)
            saved_results = [json.loads(path.read_text(encoding="utf-8")) for path in saved_files]
            phone_ok_results = [item for item in saved_results if item.get("phone_ok")]
            self.assertGreaterEqual(len(phone_ok_results), 1)
            saved = phone_ok_results[-1]
            self.assertTrue(saved["phone_ok"])
            self.assertFalse(saved["final_ok"])
            self.assertEqual(saved["status"], "phone_ok")
            self.assertEqual(web_gui._state["stats"]["current_success"], 0)
            self.assertEqual(web_gui._state["stats"]["total_success"], 0)
            self.assertGreater(web_gui._state["stats"]["current_fail"], 0)
            self.assertEqual(web_gui._state["stats"]["current_fail"], web_gui._state["stats"]["total_fail"])
        finally:
            for path in results_dir.glob("15550001111_*.json"):
                path.unlink()
            if before_all is None:
                if all_path.exists():
                    all_path.unlink()
            else:
                all_path.write_text(before_all, encoding="utf-8")

    def test_batch_phase2_fallbacks_to_new_outlook_email_when_original_missing_from_pool(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        result_path = results_dir / "test_phase2_original_email_missing.json"
        try:
            results_dir.mkdir(exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15551234567",
                        "password": "pw",
                        "bind_email": "old@outlook.com",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as tmp:
                used_path = Path(tmp) / "outlook_used.txt"
                used_path.write_text(
                    "2026-06-14 10:00:00\told@outlook.com\treserved\n",
                    encoding="utf-8",
                )
                config = {
                    "email_provider": "outlook",
                    "outlook_pool": "new@outlook.com----pw----client----refresh",
                    "outlook_used": str(used_path),
                    "upload_target": "cpa",
                    "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                    "bind_email": "",
                }

                with mock.patch.object(web_gui, "_phase2_for_result", return_value={"ok": True, "uploaded": True, "upload_verified": True, "upload_target": "cpa", "upload_method": "api"}) as phase2:
                    with mock.patch.object(web_gui, "_log") as log:
                        web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

            phase2.assert_called_once()
            phase2_config = phase2.call_args.args[1]
            self.assertEqual(phase2_config["bind_email"], "new@outlook.com")
            messages = "\n".join(str(call.args[0]) for call in log.call_args_list if call.args)
            self.assertIn("原始邮箱不可用", messages)
            self.assertIn("降级取新邮箱", messages)
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bind_email"], "new@outlook.com")
            self.assertTrue(saved["final_ok"])
        finally:
            if result_path.exists():
                result_path.unlink()

    def test_phase2_for_result_reports_missing_password(self):
        config = {
            "upload_target": "cpa",
            "bind_email": "target@outlook.com",
            "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
            "outlook_pool": "",
        }
        account = {"phone": "+15551234567"}

        with self.assertRaisesRegex(RuntimeError, "missing_phase2_field:password"):
            web_gui._phase2_for_result(account, config)

    def test_batch_phase2_skips_record_missing_password_before_oauth(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        result_path = results_dir / "test_phase2_missing_password.json"
        try:
            results_dir.mkdir(exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15551234567",
                        "bind_email": "old@outlook.com",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            config = {
                "email_provider": "outlook",
                "outlook_pool": "old@outlook.com----pw----client----refresh",
                "outlook_used": "outlook_used.txt",
                "upload_target": "cpa",
                "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                "bind_email": "",
            }

            with mock.patch.object(web_gui, "_phase2_for_result") as phase2:
                with mock.patch.object(web_gui, "_log") as log:
                    web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

            phase2.assert_not_called()
            messages = "\n".join(str(call.args[0]) for call in log.call_args_list if call.args)
            self.assertIn("missing_phase2_field:password", messages)
        finally:
            if result_path.exists():
                result_path.unlink()

    def test_batch_phase2_uses_new_outlook_email_when_previous_email_was_already_in_use(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        result_path = results_dir / "test_phase2_email_already_in_use.json"
        captured = {}
        try:
            web_gui._state["stats"] = web_gui._empty_stats()
            results_dir.mkdir(exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15551234567",
                        "password": "pw",
                        "bind_email": "used@outlook.com",
                        "email_bound": False,
                        "error": "verify_email_otp: email_already_in_use",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as tmp:
                used_path = Path(tmp) / "outlook_used.txt"
                used_path.write_text("2026-06-15 10:00:00\tused@outlook.com\temail_already_in_use\n", encoding="utf-8")
                config = {
                    "email_provider": "outlook",
                    "outlook_pool": "used@outlook.com----pw----client----refresh\nnew@outlook.com----pw----client----refresh",
                    "outlook_used": str(used_path),
                    "upload_target": "cpa",
                    "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                    "bind_email": "",
                }

                def fake_phase2(result, config, thread_tag="", thread_id=None):
                    captured["bind_email"] = config["bind_email"]
                    return {"ok": True, "uploaded": True, "upload_verified": True, "upload_target": "cpa"}

                with mock.patch.object(web_gui, "_phase2_for_result", side_effect=fake_phase2):
                    with mock.patch.object(web_gui, "_log"):
                        web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

            self.assertEqual(captured["bind_email"], "new@outlook.com")
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bind_email"], "new@outlook.com")
            self.assertTrue(saved["email_bound"])
            self.assertTrue(saved["final_ok"])
            self.assertEqual(web_gui._state["stats"]["current_success"], 1)
            self.assertEqual(web_gui._state["stats"]["total_success"], 1)
        finally:
            if result_path.exists():
                result_path.unlink()

    def test_batch_phase2_uses_new_outlook_email_when_record_has_no_original_email(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        result_path = results_dir / "test_phase2_no_original_email.json"
        captured = {}
        try:
            results_dir.mkdir(exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15551234567",
                        "password": "pw",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as tmp:
                used_path = Path(tmp) / "outlook_used.txt"
                config = {
                    "email_provider": "outlook",
                    "outlook_pool": "new@outlook.com----pw----client----refresh",
                    "outlook_used": str(used_path),
                    "upload_target": "cpa",
                    "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
                    "bind_email": "",
                }

                def fake_phase2(result, config, thread_tag="", thread_id=None):
                    captured["bind_email"] = config["bind_email"]
                    return {"ok": True, "uploaded": True, "upload_verified": True, "upload_target": "cpa"}

                with mock.patch.object(web_gui, "_phase2_for_result", side_effect=fake_phase2):
                    with mock.patch.object(web_gui, "_log"):
                        web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

            self.assertEqual(captured["bind_email"], "new@outlook.com")
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bind_email"], "new@outlook.com")
            self.assertTrue(saved["final_ok"])
        finally:
            if result_path.exists():
                result_path.unlink()

    def test_batch_phase2_upload_without_verification_is_not_final_ok(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        result_path = results_dir / "test_phase2_upload_unverified.json"
        try:
            results_dir.mkdir(exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15551234567",
                        "password": "pw",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as tmp:
                used_path = Path(tmp) / "outlook_used.txt"
                config = {
                    "email_provider": "outlook",
                    "outlook_pool": "new@outlook.com----pw----client----refresh",
                    "outlook_used": str(used_path),
                    "upload_target": "sub2api",
                    "sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw"},
                    "bind_email": "",
                }

                def fake_phase2(result, config, thread_tag="", thread_id=None):
                    return {"ok": True, "uploaded": True, "upload_verified": False, "upload_target": "sub2api"}

                with mock.patch.object(web_gui, "_phase2_for_result", side_effect=fake_phase2):
                    with mock.patch.object(web_gui, "_log"):
                        web_gui._run_batch_phase2([result_path.name], config, source="files", concurrency=1)

            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertFalse(saved["final_ok"])
            self.assertEqual(saved["status"], "upload_unverified")
        finally:
            if result_path.exists():
                result_path.unlink()

    def test_api_batch_phase2_delete_removes_selected_result_files(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        keep_path = results_dir / "test_phase2_keep.json"
        delete_a = results_dir / "test_phase2_delete_a.json"
        delete_b = results_dir / "test_phase2_delete_b.json"
        try:
            results_dir.mkdir(exist_ok=True)
            for path in (keep_path, delete_a, delete_b):
                path.write_text('{"ok": true, "phone_ok": true}\n', encoding="utf-8")

            with web_gui.app.test_client() as client:
                resp = client.post(
                    "/api/batch-phase2-delete",
                    json={"source": "files", "files": [delete_a.name, delete_b.name]},
                )

            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["deleted"], 2)
            self.assertTrue(keep_path.exists())
            self.assertFalse(delete_a.exists())
            self.assertFalse(delete_b.exists())
        finally:
            for path in (keep_path, delete_a, delete_b):
                if path.exists():
                    path.unlink()

    def test_api_batch_phase2_delete_rejects_all_source(self):
        with web_gui.app.test_client() as client:
            resp = client.post(
                "/api/batch-phase2-delete",
                json={"source": "all", "files": ["0"]},
            )

        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("results目录", data["error"])

    def test_api_batch_phase2_delete_rejects_path_traversal(self):
        with web_gui.app.test_client() as client:
            resp = client.post(
                "/api/batch-phase2-delete",
                json={"source": "files", "files": ["../config.json"]},
            )

        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])

    def test_phase2_for_result_uses_cpa_oauth_url_and_passes_cpa_config(self):
        config = {
            "upload_target": "cpa",
            "bind_email": "target@outlook.com",
            "cpa": {
                "management_url": "http://mgmt.example.com",
                "api_url": "https://cpa.example.com",
                "management_key": "mgmt-key",
                "upload_mode": "auto",
            },
            "proxy": "",
            "outlook_pool": "",
        }
        account = {
            "phone": "+15551234567",
            "password": "pw",
            "session_token": "session-token",
            "access_token": "access-token",
        }
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "uploaded": True, "upload_target": "cpa"}

        with mock.patch("phase2_codex.get_cpa_oauth_url", return_value={"auth_url": "https://auth.openai.com/oauth/authorize?state=cpa-state", "state": "cpa-state"}):
            with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
                result = web_gui._phase2_for_result(account, config)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["upload_target"], "cpa")
        self.assertEqual(captured["cpa_api_url"], "https://cpa.example.com")
        self.assertEqual(captured["cpa_management_key"], "mgmt-key")
        self.assertEqual(captured["cpa_oauth_state"], "cpa-state")
        self.assertEqual(captured["session_token"], "session-token")
        self.assertEqual(captured["access_token"], "access-token")

    def test_sanitize_result_preserves_upload_failure_fields(self):
        result = web_gui._sanitize_result(
            {
                "ok": True,
                "phone": "+15551234567",
                "session_token": "s" * 60,
                "access_token": "a" * 60,
                "uploaded": False,
                "upload_error": "CPA upload failed",
                "failed_upload_file": "failed_uploads/fail.json",
                "upload_target": "cpa",
            }
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["upload_error"], "CPA upload failed")
        self.assertEqual(result["failed_upload_file"], "failed_uploads/fail.json")
        self.assertEqual(result["upload_target"], "cpa")
        self.assertTrue(result["access_token"].endswith("..."))

    def test_api_retry_failed_upload_calls_retry_function(self):
        web_gui._state["config"] = {
            "upload_target": "cpa",
            "cpa": {"api_url": "https://cpa.example.com", "management_key": "key"},
        }

        with mock.patch("failed_uploads.retry_failed_upload", return_value={"ok": True}) as retry_mock:
            with web_gui.app.test_client() as client:
                resp = client.post("/api/failed-uploads/retry", json={"path": "failed_uploads/fail.json"})

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        expected_path = (Path(web_gui.__file__).parent / "failed_uploads" / "fail.json").resolve()
        retry_mock.assert_called_once_with(expected_path, web_gui._state["config"])

    def test_api_retry_failed_upload_updates_matching_result_record(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        failed_path = Path(web_gui.__file__).with_name("failed_uploads") / "test_retry_success.json"
        result_path = results_dir / "test_failed_upload_result.json"
        try:
            web_gui._state["stats"] = web_gui._empty_stats()
            results_dir.mkdir(exist_ok=True)
            failed_path.parent.mkdir(exist_ok=True)
            failed_path.write_text('{"phone":"+15550002222"}\n', encoding="utf-8")
            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "final_ok": False,
                        "phone": "+15550002222",
                        "bind_email": "retry@example.com",
                        "failed_upload_file": str(failed_path),
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch("failed_uploads.retry_failed_upload", return_value={"ok": True, "sub2api_account_id": "sub-123", "upload_target": "sub2api"}):
                with web_gui.app.test_client() as client:
                    resp = client.post("/api/failed-uploads/retry", json={"path": f"failed_uploads/{failed_path.name}"})

            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["uploaded"])
            self.assertTrue(saved["upload_verified"])
            self.assertTrue(saved["final_ok"])
            self.assertEqual(saved["status"], "final_ok")
            self.assertEqual(saved["sub2api_id"], "sub-123")
            self.assertEqual(web_gui._state["stats"]["current_success"], 1)
            self.assertEqual(web_gui._state["stats"]["total_success"], 1)
        finally:
            for path in (failed_path, result_path):
                if path.exists():
                    path.unlink()

    def test_api_results_list_hides_completed_phase2_records(self):
        results_dir = Path(web_gui.__file__).with_name("results")
        pending_path = results_dir / "test_results_list_pending.json"
        done_path = results_dir / "test_results_list_done.json"
        try:
            results_dir.mkdir(exist_ok=True)
            pending_path.write_text(
                json.dumps({"ok": True, "phone_ok": True, "phone": "+15550005555", "final_ok": False}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            done_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "phone": "+15550006666",
                        "final_ok": True,
                        "uploaded": True,
                        "upload_verified": True,
                        "sub2api_id": "sub-1",
                        "needs_retry": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with web_gui.app.test_client() as client:
                resp = client.get("/api/results-list?source=files")

            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            phones = {item["phone"] for item in data["items"]}
            self.assertIn("+15550005555", phones)
            self.assertNotIn("+15550006666", phones)
        finally:
            for path in (pending_path, done_path):
                if path.exists():
                    path.unlink()

    def test_api_failed_uploads_list_returns_pending_files(self):
        failed_dir = Path(web_gui.__file__).with_name("failed_uploads")
        failed_path = failed_dir / "test_pending_failed_upload.json"
        done_dir = failed_dir / "done"
        done_path = done_dir / "test_done_failed_upload.json"
        try:
            failed_dir.mkdir(exist_ok=True)
            done_dir.mkdir(exist_ok=True)
            failed_path.write_text('{"phone":"+15550003333","email":"pending@example.com","upload_target":"sub2api"}\n', encoding="utf-8")
            done_path.write_text('{"phone":"+15550004444","email":"done@example.com"}\n', encoding="utf-8")

            with web_gui.app.test_client() as client:
                resp = client.get("/api/failed-uploads/list")

            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            names = [item["filename"] for item in data["items"]]
            self.assertIn(failed_path.name, names)
            self.assertNotIn(done_path.name, names)
            item = next(item for item in data["items"] if item["filename"] == failed_path.name)
            self.assertEqual(item["phone"], "+15550003333")
            self.assertEqual(item["email"], "pending@example.com")
        finally:
            for path in (failed_path, done_path):
                if path.exists():
                    path.unlink()

    def test_api_retry_failed_upload_rejects_paths_outside_failed_uploads(self):
        with web_gui.app.test_client() as client:
            resp = client.post("/api/failed-uploads/retry", json={"path": "config.json"})

        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])

    def test_api_outlook_pool_summary_and_list_classify_entries_from_local_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "outlook.txt"
            used_path = Path(tmp) / "outlook_used.txt"
            results_dir = Path(tmp) / "results"
            results_dir.mkdir()

            pool_path.write_text(
                "\n".join(
                    [
                        "success@outlook.com----pw----cid----rt",
                        "bad@outlook.com----pw----cid----rt",
                        "verify@outlook.com----pw----cid----rt",
                        "reserved@outlook.com----pw----cid----rt",
                        "failed@outlook.com----pw----cid----rt",
                        "pending-upload@outlook.com----pw----cid----rt",
                        "unused@outlook.com----pw----cid----rt",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            used_path.write_text(
                "\n".join(
                    [
                        "2026-06-04 10:00:00\tsuccess@outlook.com\tbad",
                        "2026-06-04 10:01:00\tbad@outlook.com\tbad",
                        "2026-06-04 10:02:00\tverify@outlook.com\tverify_failed",
                        "2026-06-04 10:03:00\treserved@outlook.com\treserved",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (results_dir / "111_20260604_100500.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone": "+111",
                        "bind_email": "success@outlook.com",
                        "sub2api_id": "sub-111",
                        "upload_verified": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (results_dir / "333_20260604_100700.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone_ok": True,
                        "final_ok": False,
                        "phone": "+333",
                        "bind_email": "pending-upload@outlook.com",
                        "sub2api_id": "sub-pending",
                        "upload_verified": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (results_dir / "222_20260604_100600.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone": "+222-new",
                        "bind_email": "failed@outlook.com",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (results_dir / "222_20260604_100100.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "phone": "+222-old",
                        "bind_email": "failed@outlook.com",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (results_dir / "_all.json").write_text(
                json.dumps(
                    [
                        {
                            "ok": True,
                            "phone": "+333",
                            "bind_email": "verify@outlook.com",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            web_gui._state["config"] = {
                "outlook_pool": str(pool_path),
                "outlook_used": str(used_path),
                "bind_email": "failed@outlook.com",
                "email_provider": "outlook",
            }

            with mock.patch.object(web_gui, "_outlook_results_dir", return_value=results_dir, create=True):
                with web_gui.app.test_client() as client:
                    summary_resp = client.get("/api/outlook-pool/summary")
                    list_resp = client.get("/api/outlook-pool/list")
                    detail_resp = client.get("/api/outlook-pool/detail", query_string={"email": "failed@outlook.com"})

            self.assertEqual(summary_resp.status_code, 200)
            self.assertEqual(list_resp.status_code, 200)
            self.assertEqual(detail_resp.status_code, 200)

            summary = summary_resp.get_json()
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["total"], 7)
            self.assertEqual(
                summary["counts"],
                {
                    "unused": 1,
                    "reserved": 1,
                    "success": 1,
                    "register_failed": 2,
                    "verify_failed": 1,
                    "bad": 1,
                },
            )
            self.assertEqual(summary["current_bind_email"], "failed@outlook.com")

            items = list_resp.get_json()["items"]
            by_email = {item["email"]: item for item in items}
            self.assertEqual(items[0]["email"], "unused@outlook.com")
            self.assertEqual(items[1]["email"], "reserved@outlook.com")
            self.assertEqual(by_email["success@outlook.com"]["status"], "success")
            self.assertEqual(by_email["bad@outlook.com"]["status"], "bad")
            self.assertEqual(by_email["verify@outlook.com"]["status"], "verify_failed")
            self.assertEqual(by_email["failed@outlook.com"]["status"], "register_failed")
            self.assertEqual(by_email["pending-upload@outlook.com"]["status"], "register_failed")
            self.assertEqual(by_email["unused@outlook.com"]["status"], "unused")
            self.assertTrue(by_email["success@outlook.com"]["has_result"])
            self.assertTrue(by_email["failed@outlook.com"]["has_result"])

            detail = detail_resp.get_json()["entry"]
            self.assertEqual(detail["email"], "failed@outlook.com")
            self.assertEqual(detail["status"], "register_failed")
            self.assertEqual(detail["phone"], "+222-new")
            self.assertEqual(detail["bind_email"], "failed@outlook.com")
            self.assertEqual(detail["sub2api_id"], "")

    def test_api_outlook_pool_actions_update_config_and_used_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "outlook.txt"
            used_path = Path(tmp) / "outlook_used.txt"
            results_dir = Path(tmp) / "results"
            results_dir.mkdir()

            pool_path.write_text(
                "\n".join(
                    [
                        "bad@outlook.com----pw----cid----rt",
                        "unused-a@outlook.com----pw----cid----rt",
                        "unused-b@outlook.com----pw----cid----rt",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            used_path.write_text(
                "2026-06-04 11:00:00\tbad@outlook.com\tbad\n",
                encoding="utf-8",
            )
            web_gui._state["config"] = {
                "outlook_pool": str(pool_path),
                "outlook_used": str(used_path),
                "bind_email": "",
                "email_provider": "",
            }

            with mock.patch.object(web_gui, "_outlook_results_dir", return_value=results_dir, create=True):
                with mock.patch.object(web_gui, "_save_config_file") as save_cfg:
                    with web_gui.app.test_client() as client:
                        bad_assign = client.post(
                            "/api/outlook-pool/action",
                            json={"action": "assign_for_run", "email": "bad@outlook.com"},
                        )
                        reserve_next = client.post(
                            "/api/outlook-pool/action",
                            json={"action": "reserve_next_unused"},
                        )
                        assign_specific = client.post(
                            "/api/outlook-pool/action",
                            json={"action": "assign_for_run", "email": "unused-b@outlook.com"},
                        )
                        mark_resp = client.post(
                            "/api/outlook-pool/action",
                            json={"action": "mark_status", "email": "unused-b@outlook.com", "status": "verify_failed"},
                        )

            self.assertEqual(bad_assign.status_code, 400)
            self.assertFalse(bad_assign.get_json()["ok"])

            reserve_data = reserve_next.get_json()
            self.assertEqual(reserve_next.status_code, 200)
            self.assertTrue(reserve_data["ok"])
            self.assertEqual(reserve_data["email"], "unused-a@outlook.com")
            self.assertEqual(web_gui._state["config"]["bind_email"], "unused-b@outlook.com")
            self.assertEqual(web_gui._state["config"]["email_provider"], "outlook")
            self.assertEqual(save_cfg.call_count, 2)

            assign_data = assign_specific.get_json()
            self.assertEqual(assign_specific.status_code, 200)
            self.assertEqual(assign_data["email"], "unused-b@outlook.com")
            self.assertEqual(assign_data["entry"]["status"], "reserved")

            mark_data = mark_resp.get_json()
            self.assertEqual(mark_resp.status_code, 200)
            self.assertEqual(mark_data["entry"]["status"], "verify_failed")

            lines = used_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("unused-a@outlook.com\treserved" in line for line in lines))
            self.assertTrue(any("unused-b@outlook.com\treserved" in line for line in lines))
            self.assertTrue(any("unused-b@outlook.com\tverify_failed" in line for line in lines))

    def test_api_outlook_pool_messages_returns_recent_mail_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "outlook.txt"
            used_path = Path(tmp) / "outlook_used.txt"
            results_dir = Path(tmp) / "results"
            results_dir.mkdir()
            pool_path.write_text(
                "mailbox@outlook.com----pw----cid----rt\n",
                encoding="utf-8",
            )
            web_gui._state["config"] = {
                "outlook_pool": str(pool_path),
                "outlook_used": str(used_path),
                "bind_email": "",
                "email_provider": "outlook",
            }

            fake_messages = [
                {
                    "id": "m1",
                    "from": "noreply@openai.com",
                    "subject": "code",
                    "body": "654321",
                }
            ]

            fake_client = mock.Mock()
            fake_client.list_recent_messages.return_value = fake_messages

            with mock.patch.object(web_gui, "_outlook_results_dir", return_value=results_dir, create=True):
                with mock.patch.object(web_gui, "OutlookMailClient", return_value=fake_client, create=True):
                    with web_gui.app.test_client() as client:
                        resp = client.get(
                            "/api/outlook-pool/messages",
                            query_string={"email": "mailbox@outlook.com", "limit": 20},
                        )

            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["email"], "mailbox@outlook.com")
            self.assertEqual(data["items"], fake_messages)
            fake_client.list_recent_messages.assert_called_once_with(limit=20, include_body=True)


if __name__ == "__main__":
    unittest.main()
