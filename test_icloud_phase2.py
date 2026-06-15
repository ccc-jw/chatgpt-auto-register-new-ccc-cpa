import base64
import json
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest import mock

import openai_bind_email
import openai_pipeline
import outlook_mail
import phase2_codex
import web_gui
from icloud_hme import ICloudHME
from outlook_mail import OutlookAccount


class FakeICloudHME(ICloudHME):
    def __init__(self, aliases, created_alias="new@icloud.com"):
        self._aliases = list(aliases)
        self._created_alias = created_alias
        self.create_calls = 0

    def list_aliases(self):
        return list(self._aliases)

    def create_alias(self, label=None, max_retries=5):
        self.create_calls += 1
        return self._created_alias

    def _log(self, msg: str):
        return None


class FakePollingICloudHME(ICloudHME):
    def __init__(self, message_batches, bodies):
        super().__init__({})
        self._message_batches = list(message_batches)
        self._bodies = dict(bodies)
        self._fetch_calls = 0

    def _fetch_mail_messages(self, limit: int = 20):
        idx = min(self._fetch_calls, len(self._message_batches) - 1)
        self._fetch_calls += 1
        return self._message_batches[idx]

    def _fetch_mail_body(self, msg_id: str) -> str:
        return self._bodies.get(msg_id, "")

    def _log(self, msg: str):
        return None


class ICloudPhase2Tests(unittest.TestCase):
    @staticmethod
    def _jwt(claims):
        def enc(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"

    def test_build_cpa_auth_payload_extracts_account_id_from_id_token(self):
        id_token = self._jwt({
            "email": "user@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-chatgpt-123"},
        })

        payload = openai_bind_email._build_cpa_auth_payload(
            email="user@example.com",
            access_token="at",
            refresh_token="rt",
            id_token=id_token,
        )

        self.assertEqual(payload["account_id"], "acct-chatgpt-123")

    def test_icloud_hme_accepts_browser_export_cookie_array(self):
        client = ICloudHME([
            {"name": "X-APPLE-WEBAUTH-HSA-LOGIN", "value": "abc"},
            {"name": "X-APPLE-WEBAUTH-USER", "value": "def"},
        ])

        self.assertEqual(
            client.cookies,
            {
                "X-APPLE-WEBAUTH-HSA-LOGIN": "abc",
                "X-APPLE-WEBAUTH-USER": "def",
            },
        )

    def test_reuse_or_create_alias_prefers_active_alias(self):
        client = FakeICloudHME(
            [
                {"email": "old@icloud.com", "active": False, "used": False},
                {"email": "reuse@icloud.com", "active": True, "used": False},
            ],
            created_alias="created@icloud.com",
        )

        alias = client.reuse_or_create_alias()

        self.assertEqual(alias, "reuse@icloud.com")
        self.assertEqual(client.create_calls, 0)

    def test_reuse_or_create_alias_creates_when_no_active_alias_exists(self):
        client = FakeICloudHME(
            [{"email": "dead@icloud.com", "active": False, "used": True}],
            created_alias="created@icloud.com",
        )

        alias = client.reuse_or_create_alias()

        self.assertEqual(alias, "created@icloud.com")
        self.assertEqual(client.create_calls, 1)

    def test_web_gui_loads_phase2_icloud_cookies_from_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.json"
            cookies = [{"name": "X-APPLE-WEBAUTH-HSA-LOGIN", "value": "abc"}]
            cookie_path.write_text(json.dumps(cookies), encoding="utf-8")

            loaded = web_gui._load_phase2_icloud_cookies({"icloud_cookies": str(cookie_path)})

        self.assertEqual(loaded, cookies)

    def test_web_gui_loads_phase2_icloud_cookies_from_chrome_when_files_missing(self):
        cookies = {"X-APPLE-WEBAUTH-HSA-LOGIN": "abc"}

        with mock.patch.object(web_gui, "_iter_phase2_icloud_cookie_paths", return_value=[]):
            with mock.patch("icloud_hme.extract_chrome_cookies", return_value=cookies):
                loaded = web_gui._load_phase2_icloud_cookies({})

        self.assertEqual(loaded, cookies)

    def test_reserve_next_outlook_reuses_account_after_register_failed_status(self):
        pool_text = (
            "user1@outlook.com----pw-1----client-1----refresh-1\n"
            "user2@outlook.com----pw-2----client-2----refresh-2\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"
            used_path.write_text(
                "2026-06-14 10:00:00\tuser1@outlook.com\treserved\n"
                "2026-06-14 10:01:00\tuser1@outlook.com\tregister_failed\n",
                encoding="utf-8",
            )

            account = outlook_mail.reserve_next_outlook(pool_text, str(used_path))

        self.assertEqual(account.email, "user1@outlook.com")

    def test_reserve_next_outlook_skips_latest_reserved_account(self):
        pool_text = (
            "user1@outlook.com----pw-1----client-1----refresh-1\n"
            "user2@outlook.com----pw-2----client-2----refresh-2\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"
            used_path.write_text(
                "2026-06-14 10:00:00\tuser1@outlook.com\tregister_failed\n"
                "2026-06-14 10:01:00\tuser1@outlook.com\treserved\n",
                encoding="utf-8",
            )

            account = outlook_mail.reserve_next_outlook(pool_text, str(used_path))

        self.assertEqual(account.email, "user2@outlook.com")

    def test_reserve_next_outlook_accepts_inline_pool_content(self):
        pool_text = (
            "user1@outlook.com----pw-1----client-1----refresh-1\n"
            "user2@outlook.com----pw-2----client-2----refresh-2\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            used_path = Path(tmp) / "outlook_used.txt"

            account = outlook_mail.reserve_next_outlook(pool_text, str(used_path))

        self.assertEqual(account.email, "user1@outlook.com")
        self.assertEqual(account.client_id, "client-1")
        self.assertEqual(account.refresh_token, "refresh-1")

    def test_poll_mail_for_code_matches_target_alias_after_send_time(self):
        client = FakePollingICloudHME(
            [
                [
                    {
                        "guid": "old-target",
                        "from": "noreply@openai.com",
                        "subject": "OpenAI verification code",
                        "to": ["target@icloud.com"],
                        "dateReceived": 1000,
                    }
                ],
                [
                    {
                        "guid": "old-target",
                        "from": "noreply@openai.com",
                        "subject": "OpenAI verification code",
                        "to": ["target@icloud.com"],
                        "dateReceived": 1000,
                    },
                    {
                        "guid": "new-other",
                        "from": "noreply@openai.com",
                        "subject": "OpenAI verification code",
                        "to": ["other@icloud.com"],
                        "dateReceived": 1010,
                    },
                    {
                        "guid": "new-target",
                        "from": "noreply@openai.com",
                        "subject": "OpenAI verification code",
                        "to": ["target@icloud.com"],
                        "dateReceived": 1015,
                    },
                ],
            ],
            {
                "old-target": "Your verification code is 000000",
                "new-other": "Your verification code is 111111",
                "new-target": "Your verification code is 654321",
            },
        )

        with mock.patch("icloud_hme.time.sleep", lambda _seconds: None):
            code = client.poll_mail_for_code(
                target_email="target@icloud.com",
                sender_filters=["openai", "noreply", "verification"],
                timeout=0.05,
                interval=0,
                start_after=1005,
            )

        self.assertEqual(code, "654321")

    def test_poll_mail_for_code_uses_body_peek_payload_for_imap_messages(self):
        raw_message = (
            b"From: noreply@openai.com\r\n"
            b"To: target@icloud.com\r\n"
            b"Subject: OpenAI verification code\r\n"
            b"Date: Thu, 01 Jan 1970 00:00:10 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Your verification code is 654321\r\n"
        )

        class FakeMail:
            def __init__(self):
                self.fetch_queries = []

            def login(self, *_args, **_kwargs):
                return "OK", [b"LOGIN"]

            def select(self, *_args, **_kwargs):
                return "OK", [b"INBOX"]

            def search(self, *_args, **_kwargs):
                return "OK", [b"1"]

            def fetch(self, _mid, query):
                self.fetch_queries.append(query)
                if query == "(BODY.PEEK[])":
                    return "OK", [(b"1 (BODY[] {172})", raw_message)]
                return "OK", [b"239 ()"]

            def logout(self):
                return "OK", [b"BYE"]

        fake_mail = FakeMail()
        fake_imaplib = types.SimpleNamespace(
            IMAP4_SSL=lambda _host, _port: fake_mail,
        )

        client = ICloudHME({})

        with mock.patch.dict("sys.modules", {"imaplib": fake_imaplib}):
            with mock.patch("icloud_hme.time.sleep", lambda _seconds: None):
                code = client.poll_mail_for_code(
                    target_email="target@icloud.com",
                    sender_filters=["openai", "noreply", "verification"],
                    timeout=0.1,
                    interval=0,
                    imap_user="user@icloud.com",
                    imap_password="app-password",
                    start_after=1,
                )

        self.assertEqual(fake_mail.fetch_queries[0], "(BODY.PEEK[])")
        self.assertEqual(code, "654321")

    def test_run_second_half_passes_start_after_to_mail_polling(self):
        poll_calls = []

        class FakeFlow:
            def __init__(self, proxy="", verbose=True, device_id=""):
                self.proxy = proxy
                self.verbose = verbose

            @staticmethod
            def parse_oauth_url(oauth_url: str):
                return {"client_id": "client-1", "redirect_uri": "http://localhost/callback"}

            def initiate_oauth(self, oauth_url: str):
                return True, oauth_url, ""

            def sentinel_authorize(self):
                return ""

            def submit_phone(self, phone: str):
                return {"page": {"type": "password"}}

            def sentinel_password(self):
                return ""

            def verify_password(self, password: str):
                return {"page": {"type": "add_email"}}

            def send_bind_email(self, email: str):
                return {"page": {"type": "email_otp_verification"}}

            def verify_email_otp(self, code: str):
                return {"page": {"type": "consent"}, "continue_url": "https://auth.openai.com/continue"}

            def follow_continue_until_code(self, continue_url: str, max_hops: int = 8):
                return "auth-code-1"

            def final_oauth(self, oauth_params):
                return "auth-code-1"

            def get_session_dump(self):
                return {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}

            def select_workspace(self, workspace_id: str):
                return {"continue_url": "https://auth.openai.com/continue"}

        class FakePoller:
            def __init__(self, cookies, verbose=False):
                self.cookies = cookies
                self.verbose = verbose

            def poll_mail_for_code(self, **kwargs):
                poll_calls.append(kwargs)
                return "654321"

        with mock.patch.object(openai_bind_email, "OAuthSecondHalf", FakeFlow):
            with mock.patch("icloud_hme.ICloudHME", FakePoller):
                result = openai_bind_email.run_second_half(
                    oauth_url="https://auth.openai.com/oauth/authorize?client_id=client-1",
                    phone="+15551234567",
                    password="pw-123",
                    icloud_email="target@icloud.com",
                    icloud_cookies={},
                    verbose=False,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(poll_calls), 1)
        self.assertIn("start_after", poll_calls[0])
        self.assertIsInstance(poll_calls[0]["start_after"], float)

    def test_run_second_half_uses_outlook_polling_for_outlook_bind_email(self):
        poll_calls = []

        class FakeFlow:
            def __init__(self, proxy="", verbose=True, device_id=""):
                self.proxy = proxy
                self.verbose = verbose

            @staticmethod
            def parse_oauth_url(oauth_url: str):
                return {"client_id": "client-1", "redirect_uri": "http://localhost/callback"}

            def initiate_oauth(self, oauth_url: str):
                return True, oauth_url, ""

            def sentinel_authorize(self):
                return ""

            def submit_phone(self, phone: str):
                return {"page": {"type": "password"}}

            def sentinel_password(self):
                return ""

            def verify_password(self, password: str):
                return {"page": {"type": "add_email"}}

            def send_bind_email(self, email: str):
                return {"page": {"type": "email_otp_verification"}}

            def verify_email_otp(self, code: str):
                return {"page": {"type": "consent"}, "continue_url": "https://auth.openai.com/continue"}

            def follow_continue_until_code(self, continue_url: str, max_hops: int = 8):
                return "auth-code-1"

            def final_oauth(self, oauth_params):
                return "auth-code-1"

            def get_session_dump(self):
                return {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}

            def select_workspace(self, workspace_id: str):
                return {"continue_url": "https://auth.openai.com/continue"}

        class FakeICloudPoller:
            def __init__(self, cookies, verbose=False):
                self.cookies = cookies
                self.verbose = verbose

            def poll_mail_for_code(self, **kwargs):
                return "654321"

        account = OutlookAccount(
            email="target@outlook.com",
            password="pw",
            client_id="cid",
            refresh_token="rt",
        )

        def fake_poll_outlook_for_code(outlook_account, **kwargs):
            poll_calls.append((outlook_account, kwargs))
            return "654321"

        with mock.patch.object(openai_bind_email, "OAuthSecondHalf", FakeFlow):
            with mock.patch("icloud_hme.ICloudHME", FakeICloudPoller):
                with mock.patch("outlook_mail.load_outlook_accounts", return_value=[account]):
                    with mock.patch("outlook_mail.poll_outlook_for_code", side_effect=fake_poll_outlook_for_code):
                        result = openai_bind_email.run_second_half(
                            oauth_url="https://auth.openai.com/oauth/authorize?client_id=client-1",
                            phone="+15551234567",
                            password="pw-123",
                            icloud_email="target@outlook.com",
                            icloud_cookies={},
                            verbose=False,
                        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(poll_calls), 1)
        self.assertEqual(poll_calls[0][0].email, "target@outlook.com")
        self.assertIn("start_after", poll_calls[0][1])
        self.assertIsInstance(poll_calls[0][1]["start_after"], float)

    def test_poll_bind_code_retries_outlook_without_proxy_when_proxied_poll_returns_empty(self):
        account = OutlookAccount(
            email="target@outlook.com",
            password="pw",
            client_id="cid",
            refresh_token="rt",
        )
        poll_proxies = []

        def fake_poll_outlook_for_code(outlook_account, **kwargs):
            self.assertEqual(outlook_account.email, "target@outlook.com")
            poll_proxies.append(kwargs.get("proxy", ""))
            return "" if kwargs.get("proxy") else "654321"

        with mock.patch("outlook_mail.load_outlook_accounts", return_value=[account]):
            with mock.patch("outlook_mail.poll_outlook_for_code", side_effect=fake_poll_outlook_for_code):
                code = openai_bind_email._poll_bind_code(
                    bind_email="target@outlook.com",
                    icloud_cookies={},
                    verbose=False,
                    timeout=10,
                    imap_user="",
                    imap_password="",
                    start_after=100.0,
                    proxy="socks5://127.0.0.1:1080",
                    outlook_pool="target@outlook.com----pw----cid----rt",
                )

        self.assertEqual(code, "654321")
        self.assertEqual(poll_proxies, ["socks5://127.0.0.1:1080", ""])


class Phase2WrapperTests(unittest.TestCase):
    @staticmethod
    def _jwt(claims):
        def enc(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"

    def test_phase2_bind_and_upload_does_not_pass_mailmanage_kwargs(self):
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "sub2api_account_id": "sub-1"}

        pipeline = openai_pipeline.FullPipeline(
            mailmanage_api_key="mak-123",
            mailmanage_base_url="https://mailmanage.example.com",
            mailmanage_keyword="gpt",
            verbose=False,
        )

        with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
            ok = pipeline.phase2_bind_and_upload(
                phone="+15551234567",
                password="pw-123",
                icloud_email="target@outlook.com",
                session_token="session-1",
                oauth_url="https://auth.openai.com/oauth/authorize?client_id=client-1",
            )

        self.assertTrue(ok)
        self.assertNotIn("mailmanage_api_key", captured)
        self.assertNotIn("mailmanage_base_url", captured)
        self.assertNotIn("mailmanage_keyword", captured)

    def test_resume_pipeline_does_not_pass_mailmanage_kwargs(self):
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
            ok = openai_pipeline.resume_pipeline(
                oauth_url="https://auth.openai.com/oauth/authorize?client_id=client-1",
                phone="+15551234567",
                password="pw-123",
                icloud_email="target@outlook.com",
                icloud_cookies={},
                sub2api_url="",
                sub2api_email="",
                sub2api_password="",
                mailmanage_api_key="mak-123",
                mailmanage_base_url="https://mailmanage.example.com",
                mailmanage_keyword="gpt",
                verbose=False,
            )

        self.assertTrue(ok)
        self.assertNotIn("mailmanage_api_key", captured)
        self.assertNotIn("mailmanage_base_url", captured)
        self.assertNotIn("mailmanage_keyword", captured)

    def test_phase2_codex_forwards_session_and_state(self):
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "sub2api_account_id": "sub-1"}

        with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
            result = phase2_codex.codex_login(
                session_token="session-1",
                phone="+15551234567",
                password="pw-123",
                bind_email="target@outlook.com",
                oauth_url="https://auth.openai.com/oauth/authorize?client_id=client-1",
                icloud_cookies={},
                sub2api_url="https://sub2api.example.com",
                sub2api_email="admin@example.com",
                sub2api_pwd="secret",
                sub2api_proxy_id=7,
                sub2api_session_id="sid-123",
                sub2api_state="state-123",
                verbose=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["sub2api_session_id"], "sid-123")
        self.assertEqual(captured["sub2api_state"], "state-123")

    def test_phase2_codex_get_oauth_url_returns_auth_url_session_and_state(self):
        login_resp = mock.Mock()
        login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}

        auth_url = (
            "https://auth.openai.com/oauth/authorize?"
            f"redirect_uri={quote('http://localhost:1455/auth/callback', safe='')}&state=state-123"
        )
        oauth_resp = mock.Mock()
        oauth_resp.json.return_value = {
            "code": 0,
            "data": {
                "auth_url": auth_url,
                "session_id": "sid-123",
            },
        }

        with mock.patch("requests.post", side_effect=[login_resp, oauth_resp]):
            oauth_info = phase2_codex.get_oauth_url(
                "https://sub2api.example.com",
                "admin@example.com",
                "secret",
                sub2api_proxy_id=7,
            )

        self.assertEqual(oauth_info["auth_url"], auth_url)
        self.assertEqual(oauth_info["session_id"], "sid-123")
        self.assertEqual(oauth_info["state"], "state-123")

    def test_get_cpa_oauth_url_returns_url_and_state(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {
            "status": "ok",
            "state": "cpa-state-123",
            "url": "https://auth.openai.com/oauth/authorize?state=cpa-state-123",
        }

        with mock.patch("requests.get", return_value=resp) as get_mock:
            info = phase2_codex.get_cpa_oauth_url("https://cpa.example.com/", "mgmt-key")

        get_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/codex-auth-url",
            headers={"X-Management-Key": "mgmt-key"},
            timeout=30,
        )
        self.assertEqual(info["auth_url"], "https://auth.openai.com/oauth/authorize?state=cpa-state-123")
        self.assertEqual(info["state"], "cpa-state-123")

    def test_complete_cpa_oauth_callback_posts_provider_code_and_state(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {"status": "ok", "message": "imported"}

        with mock.patch("requests.post", return_value=resp) as post_mock:
            result = phase2_codex.complete_cpa_oauth_callback(
                "https://cpa.example.com",
                "mgmt-key",
                "code-123",
                "state-123",
            )

        post_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/oauth-callback",
            json={"provider": "codex", "code": "code-123", "state": "state-123"},
            headers={"X-Management-Key": "mgmt-key"},
            timeout=120,
        )
        self.assertTrue(result["ok"])

    def test_upload_cpa_auth_file_posts_codex_payload(self):
        resp = mock.Mock(status_code=200, text='{"status":"ok"}')
        resp.json.return_value = {"status": "ok", "filename": "codex-user.json"}

        with mock.patch("requests.post", return_value=resp) as post_mock:
            result = phase2_codex.upload_cpa_auth_file(
                "https://cpa.example.com",
                "mgmt-key",
                {"type": "codex", "email": "user@example.com", "access_token": "at"},
                "codex-user.json",
            )

        post_mock.assert_called_once_with(
            "https://cpa.example.com/v0/management/auth-files",
            json={"filename": "codex-user.json", "content": {"type": "codex", "email": "user@example.com", "access_token": "at"}},
            headers={"X-Management-Key": "mgmt-key"},
            timeout=60,
        )
        self.assertTrue(result["ok"])

    def test_phase2_codex_codex_login_forwards_upload_target_and_cpa_config(self):
        captured = {}

        def fake_run_second_half(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "uploaded": True, "upload_target": "cpa"}

        with mock.patch("openai_bind_email.run_second_half", side_effect=fake_run_second_half):
            result = phase2_codex.codex_login(
                session_token="session-1",
                phone="+15551234567",
                password="pw-123",
                bind_email="target@outlook.com",
                oauth_url={"auth_url": "https://auth.openai.com/oauth/authorize?state=cpa-state", "state": "cpa-state"},
                icloud_cookies={},
                upload_target="cpa",
                cpa_api_url="https://cpa.example.com",
                cpa_management_key="mgmt-key",
                cpa_upload_mode="auto",
                verbose=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["upload_target"], "cpa")
        self.assertEqual(captured["cpa_api_url"], "https://cpa.example.com")
        self.assertEqual(captured["cpa_management_key"], "mgmt-key")
        self.assertEqual(captured["cpa_oauth_state"], "cpa-state")

    def test_run_second_half_cpa_callback_success_returns_uploaded(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.verify_password.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.selected_workspace_id = "ws-1"
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": True, "data": {"status": "ok"}}):
                with mock.patch("phase2_codex.ensure_cpa_auth_file_account_id", return_value={"ok": True}) as patch_mock:
                    result = openai_bind_email.run_second_half(
                        oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                        phone="+15551234567",
                        password="pw",
                        icloud_email="user@example.com",
                        icloud_cookies={},
                        upload_target="cpa",
                        cpa_api_url="https://cpa.example.com",
                        cpa_management_key="mgmt-key",
                        cpa_oauth_state="cpa-state",
                        verbose=False,
                    )

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["upload_target"], "cpa")
        patch_mock.assert_called_once_with("https://cpa.example.com", "mgmt-key", "codex-user@example.com.json", "", "user@example.com")

    def test_run_second_half_cpa_callback_failure_falls_back_to_auth_file(self):
        id_token = self._jwt({
            "email": "user@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-chatgpt-123"},
        })
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.verify_password.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": False, "data": {"error": "bad"}}):
                with mock.patch("phase2_codex.upload_cpa_auth_file", return_value={"ok": True, "filename": "codex-user.json"}) as upload_mock:
                    result = openai_bind_email.run_second_half(
                        oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                        phone="+15551234567",
                        password="pw",
                        icloud_email="user@example.com",
                        icloud_cookies={},
                        upload_target="cpa",
                        cpa_api_url="https://cpa.example.com",
                        cpa_management_key="mgmt-key",
                        cpa_oauth_state="cpa-state",
                        access_token="at",
                        refresh_token="rt",
                        id_token=id_token,
                        verbose=False,
                    )

        self.assertTrue(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["upload_method"], "cpa_auth_file")
        self.assertEqual(upload_mock.call_args.args[2]["account_id"], "acct-chatgpt-123")

    def test_run_second_half_cpa_fallback_without_account_id_saves_failed_upload(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.verify_password.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": False, "data": {"error": "bad"}}):
                with mock.patch("phase2_codex.upload_cpa_auth_file") as upload_mock:
                    with mock.patch("failed_uploads.save_failed_upload", return_value="failed_uploads/missing-account.json") as save_mock:
                        result = openai_bind_email.run_second_half(
                            oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                            phone="+15551234567",
                            password="pw",
                            icloud_email="user@example.com",
                            icloud_cookies={},
                            upload_target="cpa",
                            cpa_api_url="https://cpa.example.com",
                            cpa_management_key="mgmt-key",
                            cpa_oauth_state="cpa-state",
                            access_token="at",
                            refresh_token="rt",
                            verbose=False,
                        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["failed_upload_file"], "failed_uploads/missing-account.json")
        self.assertIn("account_id", result["upload_error"])
        upload_mock.assert_not_called()
        self.assertNotIn("account_id", save_mock.call_args.args[0])

    def test_run_second_half_cpa_final_failure_saves_failed_upload(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.verify_password.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"
            with mock.patch("phase2_codex.complete_cpa_oauth_callback", return_value={"ok": False, "data": {"error": "bad"}}):
                with mock.patch("phase2_codex.upload_cpa_auth_file", return_value={"ok": False, "data": {"error": "bad fallback"}}):
                    with mock.patch("failed_uploads.save_failed_upload", return_value="failed_uploads/fail.json") as save_mock:
                        result = openai_bind_email.run_second_half(
                            oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=cpa-state",
                            phone="+15551234567",
                            password="pw",
                            icloud_email="user@example.com",
                            icloud_cookies={},
                            upload_target="cpa",
                            cpa_api_url="https://cpa.example.com",
                            cpa_management_key="mgmt-key",
                            cpa_oauth_state="cpa-state",
                            access_token="at",
                            account_id="acct-chatgpt-123",
                            verbose=False,
                        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["failed_upload_file"], "failed_uploads/fail.json")
        saved_record = save_mock.call_args.args[0]
        self.assertEqual(saved_record["phone"], "+15551234567")
        self.assertEqual(saved_record["access_token"], "at")
        self.assertNotIn("cpa_management_key", saved_record)

    def test_run_second_half_sub2api_upload_failure_saves_failed_upload(self):
        with mock.patch("openai_bind_email.OAuthSecondHalf") as flow_cls:
            flow = flow_cls.return_value
            flow.initiate_oauth.return_value = (True, "", "")
            flow.submit_phone.return_value = {"page": {"type": "consent"}}
            flow.verify_password.return_value = {"page": {"type": "consent"}}
            flow.get_session_dump.return_value = {"client_auth_session": {"workspaces": [{"id": "ws-1"}]}}
            flow.select_workspace.return_value = {"page": {"type": "done"}, "continue_url": "https://auth.openai.com/continue"}
            flow.follow_continue_until_code.return_value = "code-123"

            login_resp = mock.Mock(status_code=200)
            login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}
            exchange_resp = mock.Mock(status_code=500, text="server error")

            with mock.patch("requests.post", side_effect=[login_resp, exchange_resp]):
                with mock.patch("failed_uploads.save_failed_upload", return_value="failed_uploads/sub.json") as save_mock:
                    result = openai_bind_email.run_second_half(
                        oauth_url="https://auth.openai.com/oauth/authorize?client_id=cid&state=sub-state",
                        phone="+15551234567",
                        password="pw",
                        icloud_email="user@example.com",
                        icloud_cookies={},
                        sub2api_url="https://sub2api.example.com",
                        sub2api_email="admin@example.com",
                        sub2api_password="secret",
                        sub2api_session_id="sid-123",
                        sub2api_state="sub-state",
                        session_token="session-token",
                        access_token="access-token",
                        verbose=False,
                    )

        self.assertTrue(result["ok"])
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["failed_upload_file"], "failed_uploads/sub.json")
        saved_record = save_mock.call_args.args[0]
        self.assertEqual(saved_record["upload_target"], "sub2api")
        self.assertEqual(saved_record["session_token"], "session-token")
        self.assertEqual(saved_record["access_token"], "access-token")
        self.assertNotIn("sub2api_password", saved_record)


class OutlookMailFallbackTests(unittest.TestCase):
    def test_poll_code_falls_back_to_graph_when_imap_preferred_and_imap_fails(self):
        account = OutlookAccount(
            email="target@outlook.com",
            password="pw",
            client_id="cid",
            refresh_token="rt",
        )
        client = outlook_mail.OutlookMailClient(account, prefer_imap=True)

        with mock.patch.object(client, "_poll_imap_once", side_effect=RuntimeError("imap down")) as imap_mock:
            with mock.patch.object(client, "_poll_graph_once", return_value="654321") as graph_mock:
                code = client.poll_code(timeout=1, interval=0)

        self.assertEqual(code, "654321")
        self.assertEqual(imap_mock.call_count, 1)
        self.assertEqual(graph_mock.call_count, 1)

    def test_list_recent_messages_falls_back_to_graph_when_imap_preferred_and_imap_fails(self):
        account = OutlookAccount(
            email="target@outlook.com",
            password="pw",
            client_id="cid",
            refresh_token="rt",
        )
        client = outlook_mail.OutlookMailClient(account, prefer_imap=True)

        with mock.patch.object(client, "_list_imap_messages", side_effect=RuntimeError("imap down")) as imap_mock:
            with mock.patch.object(
                client,
                "_list_graph_messages",
                return_value=[{"id": "m1", "subject": "OpenAI verification code"}],
            ) as graph_mock:
                items = client.list_recent_messages(limit=5, include_body=True)

        self.assertEqual(items, [{"id": "m1", "subject": "OpenAI verification code"}])
        self.assertEqual(imap_mock.call_count, 1)
        self.assertEqual(graph_mock.call_count, 1)

    def test_outlook_imap_accepts_same_second_message_as_start_after(self):
        raw_message = (
            b"From: noreply@tm.openai.com\r\n"
            b"To: target@outlook.com\r\n"
            b"Subject: Your temporary OpenAI verification code\r\n"
            b"Date: Thu, 01 Jan 1970 00:00:10 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Enter this temporary verification code to continue: 776737\r\n"
        )

        class FakeMail:
            def authenticate(self, *_args, **_kwargs):
                return "OK", [b"AUTH"]

            def select(self, *_args, **_kwargs):
                return "OK", [b"INBOX"]

            def search(self, *_args, **_kwargs):
                return "OK", [b"1"]

            def fetch(self, _msg_id, _query):
                return "OK", [(b"1 (RFC822 {200})", raw_message)]

            def logout(self):
                return "OK", [b"BYE"]

        account = OutlookAccount(
            email="target@outlook.com",
            password="pw",
            client_id="cid",
            refresh_token="rt",
        )
        client = outlook_mail.OutlookMailClient(account, verbose=False, proxy="", prefer_imap=True)

        with mock.patch.object(client, "_get_imap_token", return_value="imap-token"):
            with mock.patch("outlook_mail.imaplib.IMAP4_SSL", return_value=FakeMail()):
                code = client._poll_imap_once(
                    ["openai", "noreply", "verification"],
                    set(),
                    start_after=10.9,
                )

        self.assertEqual(code, "776737")


if __name__ == "__main__":
    unittest.main()
