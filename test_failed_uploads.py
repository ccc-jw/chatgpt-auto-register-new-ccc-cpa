import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import failed_uploads


class FailedUploadsTests(unittest.TestCase):
    def test_save_failed_upload_writes_json_without_management_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = failed_uploads.save_failed_upload(
                {
                    "upload_target": "cpa",
                    "upload_mode": "auto",
                    "phone": "+56123456789",
                    "email": "user@example.com",
                    "session_token": "session-token",
                    "access_token": "access-token",
                    "management_key": "must-not-save",
                    "sub2api_pwd": "must-not-save",
                    "last_error": "CPA upload failed",
                },
                base_dir=Path(tmp),
            )

            saved_path = Path(path)
            self.assertTrue(saved_path.exists())
            self.assertEqual(saved_path.suffix, ".json")
            self.assertFalse(saved_path.with_suffix(saved_path.suffix + ".tmp").exists())
            data = json.loads(saved_path.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["upload_target"], "cpa")
        self.assertEqual(data["attempts"], 1)
        self.assertEqual(data["phone"], "+56123456789")
        self.assertNotIn("management_key", data)
        self.assertNotIn("sub2api_pwd", data)

    def test_save_failed_upload_generates_unique_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = failed_uploads.save_failed_upload({"phone": "+1", "email": "a@example.com"}, base_dir=Path(tmp))
            second = failed_uploads.save_failed_upload({"phone": "+1", "email": "a@example.com"}, base_dir=Path(tmp))

        self.assertNotEqual(first, second)

    def test_load_failed_upload_reads_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = failed_uploads.save_failed_upload(
                {"upload_target": "sub2api", "phone": "+1", "access_token": "at"},
                base_dir=Path(tmp),
            )
            record = failed_uploads.load_failed_upload(path)

        self.assertEqual(record["upload_target"], "sub2api")
        self.assertEqual(record["access_token"], "at")

    def test_retry_failed_upload_sub2api_rejects_missing_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "sub2api",
                        "email": "user@example.com",
                        "access_token": "at",
                        "expires_at": 1790000000,
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw", "group": "CHATGPT"}}

            with self.assertRaisesRegex(RuntimeError, "missing refresh_token"):
                failed_uploads.retry_failed_upload(path, config)

    def test_retry_failed_upload_sub2api_rejects_invalid_expires_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "sub2api",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": 0,
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw", "group": "CHATGPT"}}

            with self.assertRaisesRegex(RuntimeError, "invalid expires_at"):
                failed_uploads.retry_failed_upload(path, config)

    def test_retry_failed_upload_sub2api_rejects_upload_without_refresh_token_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "sub2api",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": 1790000000,
                        "group_ids": [7],
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw"}}
            login_resp = mock.Mock()
            login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}
            upload_resp = mock.Mock()
            upload_resp.json.return_value = {
                "code": 0,
                "data": {"id": "acc-1", "credentials_status": {"has_access_token": True, "has_refresh_token": False}},
            }

            def fake_post(url, **kwargs):
                if url.endswith("/api/v1/admin/accounts"):
                    return upload_resp
                return login_resp

            with mock.patch("requests.post", side_effect=fake_post):
                result = failed_uploads.retry_failed_upload(path, config)

            self.assertFalse(result["ok"])
            self.assertIn("refresh_token", result["error"])
            self.assertTrue(path.exists())

    def test_retry_failed_upload_sub2api_uses_record_group_and_expires_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "sub2api",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": 1790000000,
                        "group_ids": [7],
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw", "group_id": 1}}
            login_resp = mock.Mock()
            login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}
            upload_resp = mock.Mock()
            upload_resp.json.return_value = {"code": 0, "data": {"id": "acc-1"}}
            captured = {}

            def fake_post(url, **kwargs):
                if url.endswith("/api/v1/admin/accounts"):
                    captured["body"] = kwargs["json"]
                    return upload_resp
                return login_resp

            with mock.patch("requests.post", side_effect=fake_post):
                result = failed_uploads.retry_failed_upload(path, config)

            self.assertTrue(result["ok"])
            self.assertEqual(captured["body"]["group_ids"], [7])
            self.assertEqual(captured["body"]["credentials"]["expires_at"], 1790000000)
            self.assertEqual(captured["body"]["credentials"]["refresh_token"], "rt")

    def test_retry_failed_upload_sub2api_resolves_configured_group_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "sub2api",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_at": 1790000000,
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"sub2api": {"url": "https://sub.example.com", "email": "admin@example.com", "pwd": "pw", "group": "CHATGPT"}}
            login_resp = mock.Mock()
            login_resp.json.return_value = {"code": 0, "data": {"access_token": "admin-token"}}
            groups_resp = mock.Mock()
            groups_resp.json.return_value = {"code": 0, "data": {"items": [{"id": 9, "name": "CHATGPT"}]}}
            upload_resp = mock.Mock()
            upload_resp.json.return_value = {"code": 0, "data": {"id": "acc-1"}}
            captured = {}

            def fake_post(url, **kwargs):
                if url.endswith("/api/v1/admin/accounts"):
                    captured["body"] = kwargs["json"]
                    return upload_resp
                return login_resp

            def fake_get(url, **kwargs):
                return groups_resp

            with mock.patch("requests.post", side_effect=fake_post):
                with mock.patch("requests.get", side_effect=fake_get):
                    result = failed_uploads.retry_failed_upload(path, config)

            self.assertTrue(result["ok"])
            self.assertEqual(captured["body"]["group_ids"], [9])

    def test_retry_failed_upload_moves_successful_file_to_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(
                failed_uploads.save_failed_upload(
                    {
                        "upload_target": "cpa",
                        "upload_mode": "auto",
                        "email": "user@example.com",
                        "access_token": "at",
                        "refresh_token": "rt",
                    },
                    base_dir=Path(tmp),
                )
            )
            config = {"cpa": {"api_url": "https://cpa.example.com", "management_key": "key"}}

            with mock.patch("failed_uploads.upload_cpa_auth_file", return_value={"ok": True, "filename": "codex-user.json"}):
                result = failed_uploads.retry_failed_upload(path, config)

            self.assertTrue(result["ok"])
            self.assertFalse(path.exists())
            self.assertTrue((Path(tmp) / "done" / path.name).exists())


if __name__ == "__main__":
    unittest.main()
