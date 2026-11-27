import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx

from video_sum_infra.config import ServiceSettings
from video_sum_service import app as service_app
from video_sum_service import auth_compat
from video_sum_service.auth import AccessTokenManager
from video_sum_service.routers import system
from video_sum_service.settings_manager import SettingsManager


ORIGIN = "https://bilisum.example.test"
AUTH_PATH = "/api/v1/auth"
SECRET = "fixture-legacy-session-secret"
GRANT = "fixture-inject-secret"
TOKEN = "fixture-shared-api-token"


def headers(uid):
    return {
        "X-Bilisum-Uid": uid, "X-Bilisum-Grant": GRANT,
        "X-Requested-With": "LazyCat-Login", "Origin": ORIGIN,
    }


def legacy_cookie(payload):
    # Serialize independently using the released 1.19.2 cookie contract.
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(SECRET.encode(), body.encode(), "sha256").hexdigest()
    return f"{body}.{signature}"


class AuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        self.db = path / "video_sum.db"
        self.provider_url = "https://identity.example.test/"
        env = {
            "VIDEO_SUM_SESSION_SECRET": SECRET, "VIDEO_SUM_ACCESS_TOKEN": TOKEN,
            "BILISUM_INJECT_SECRET": GRANT, "LAZYCAT_AUTH_OIDC_CLIENT_ID": "fixture-client",
            "LAZYCAT_AUTH_OIDC_CLIENT_SECRET": "fixture-client-secret",
            "LAZYCAT_AUTH_OIDC_AUTH_URI": self.provider_url + "authorize",
            "LAZYCAT_AUTH_OIDC_TOKEN_URI": self.provider_url + "token",
            "LAZYCAT_AUTH_OIDC_USERINFO_URI": self.provider_url + "userinfo",
            "VIDEO_SUM_OIDC_REDIRECT_URI": ORIGIN + AUTH_PATH + "/oidc/callback",
        }
        self.env = env
        manager = AccessTokenManager(path, env=env)
        for module in (auth_compat, service_app, system):
            self.enterContext(patch.object(module, "access_token_manager", manager))
        self.app = FastAPI()
        self.app.state.auth_database_file = str(self.db)
        self.app.state.settings_manager = SimpleNamespace(
            current=SimpleNamespace(database_url="sqlite:///" + str(self.db))
        )
        self.app.include_router(system.router)
        self.app.middleware("http")(service_app.require_api_access_token)

        @self.app.get("/api/v1/who")
        def who(request: Request):
            return auth_compat.actor_for_request(request)

        self.client = self.client_for_app()

    def client_for_app(self):
        return self.enterContext(TestClient(self.app, base_url=ORIGIN, follow_redirects=False))

    def simulate_upgrade(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("""
                CREATE TABLE knowledge_conversations (
                    conversation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                    messages_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            for actor in ("oidc:subject-a", "oidc:subject-b", "access-token:shared"):
                connection.execute(
                    "INSERT INTO knowledge_conversations VALUES (?, ?, 'private', '[]', '', '')", (actor, actor)
                )

    def oidc(self, client, subject, uid=None):
        gateway_headers = headers(uid) if uid is not None else {}
        login = client.get(AUTH_PATH + "/oidc/login", headers=gateway_headers)
        self.assertEqual(login.status_code, 302, login.text)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [ORIGIN + AUTH_PATH + "/oidc/callback"])

        def exchange(url, *, data, timeout):
            self.assertEqual(url, self.provider_url + "token")
            self.assertEqual(data["client_secret"], "fixture-client-secret")
            self.assertEqual(data["code"], "fixture-code")
            hashed = hashlib.sha256(data["code_verifier"].encode()).digest()
            self.assertEqual(query["code_challenge"], [base64.urlsafe_b64encode(hashed).decode().rstrip("=")])
            return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "fixture-oauth"})

        def userinfo(url, *, headers, timeout):
            self.assertEqual(url, self.provider_url + "userinfo")
            self.assertEqual(headers["Authorization"], "Bearer fixture-oauth")
            # All test subjects deliberately share both mutable profile attributes.
            data = {"sub": subject, "name": "相同名字", "email": "same@example.test", "groups": ["ADMIN"]}
            return httpx.Response(200, request=httpx.Request("GET", url), json=data)

        with patch.object(auth_compat.httpx, "post", exchange), patch.object(auth_compat.httpx, "get", userinfo):
            return client.get(AUTH_PATH + "/oidc/callback", headers=gateway_headers,
                              params={"code": "fixture-code", "state": query["state"][0]})

    def auto(self, uid, client=None):
        return (client or self.client).post(AUTH_PATH + "/auto-session", headers=headers(uid))

    def actor(self, client=None):
        res = (client or self.client).get("/api/v1/who")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_old_signed_oidc_and_shared_token_keep_their_original_actor(self):
        self.simulate_upgrade()
        payload = {"sub": "subject-a", "name": "原用户", "email": "a@example.test", "role": "ADMIN",
                   "issued_at": int(time.time()), "expires_at": int(time.time()) + 3600}
        self.client.cookies.set(auth_compat.OIDC_COOKIE, legacy_cookie(payload), domain="bilisum.example.test")
        self.assertEqual(self.actor(), "oidc:subject-a")
        status = self.client.get(AUTH_PATH + "/status").json()
        self.assertEqual(status["user"]["name"], "原用户")
        self.assertEqual(status["oidcLoginPath"], AUTH_PATH + "/oidc/login")
        self.assertEqual(self.auto("unrelated-gateway-account").status_code, 409)
        self.assertEqual(self.actor(), "oidc:subject-a")
        token_auth = self.client.post(AUTH_PATH + "/session", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(token_auth.status_code, 200)
        self.assertEqual(self.actor(), "access-token:shared")
        self.client.post(AUTH_PATH + "/logout")
        self.assertEqual(self.client.get("/api/v1/who").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/who", headers={"Authorization": "Bearer " + TOKEN}).json(),
                         "access-token:shared")

    def test_owner_instance_preserves_opaque_old_cookie_without_persisting_unverified_link(self):
        self.simulate_upgrade()
        self.env["BILISUM_DEPLOY_UID"] = "gateway-a"
        cookie = legacy_cookie({"sub": "subject-a", "expires_at": int(time.time()) + 3600})
        self.client.cookies.set(auth_compat.OIDC_COOKIE, cookie, domain="bilisum.example.test")
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).json(), "oidc:subject-a")
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-b")).status_code, 401)
        self.assertNotIn("canAutoBind", self.client.get(AUTH_PATH + "/status", headers=headers("gateway-a")).json())
        self.assertEqual(self.auto("gateway-a").json()["authMethod"], "oidc")
        blank = self.client_for_app()
        self.assertEqual(self.auto("gateway-a", blank).status_code, 409)
        self.assertEqual(self.oidc(blank, "subject-b", "gateway-a").status_code, 302)
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).status_code, 401)

    def test_owner_instance_can_bind_matching_legacy_subject_and_uid(self):
        self.simulate_upgrade()
        self.env["BILISUM_DEPLOY_UID"] = "subject-a"
        cookie = legacy_cookie({"sub": "subject-a", "expires_at": int(time.time()) + 3600})
        self.client.cookies.set(auth_compat.OIDC_COOKIE, cookie, domain="bilisum.example.test")
        self.assertIs(self.client.get(AUTH_PATH + "/status", headers=headers("subject-a")).json()["canAutoBind"], True)
        self.assertEqual(self.auto("subject-a").json()["authMethod"], "inject")
        blank = self.client_for_app()
        self.assertEqual(self.auto("subject-a", blank).status_code, 200)
        self.assertEqual(self.actor(blank), "oidc:subject-a")
        self.assertEqual(self.auto("subject-b", blank).status_code, 409)

    def test_gateway_account_switch_rejects_stale_personal_sessions(self):
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        actor = self.actor()
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).json(), actor)
        self.assertFalse(self.client.get(AUTH_PATH + "/status", headers=headers("gateway-b")).json()["authenticated"])
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-b")).status_code, 401)
        self.assertEqual(self.oidc(self.client, "subject-a", "gateway-a").status_code, 302)
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-b")).status_code, 401)
        self.assertEqual(self.oidc(self.client, "subject-a").status_code, 302)
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-b")).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).json(), actor)

    def test_new_personal_cookies_are_bound_to_the_database_that_issued_them(self):
        other_instance = str(self.db.with_suffix(".other"))
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        with patch.object(self.app.state, "auth_database_file", other_instance):
            self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).status_code, 401)
        self.assertEqual(self.oidc(self.client, "subject-a", "gateway-a").status_code, 302)
        with patch.object(self.app.state, "auth_database_file", other_instance):
            self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).status_code, 401)

    def test_settings_save_keeps_auth_with_the_running_repository_until_restart(self):
        settings = SettingsManager(ServiceSettings(
            _env_file=None, data_dir=self.db.parent, cache_dir=self.db.parent / "cache",
            tasks_dir=self.db.parent / "tasks", database_url="sqlite:///" + str(self.db),
        ))
        self.app.state.settings_manager = settings
        self.enterContext(patch.object(system, "settings_manager", settings))
        self.enterContext(patch.object(system, "_rebuild_worker_after_save"))
        self.enterContext(patch.object(system, "_load_cached_environment_snapshot", return_value={}))
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        original_actor = self.actor()
        pending_path = self.db.with_name("next-startup.db")
        pending_url = "sqlite:///" + str(pending_path)
        changed = self.client.put("/api/v1/settings", json={"database_url": pending_url})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(settings.current.database_url, pending_url)
        self.assertEqual(self.actor(), original_actor)
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        self.assertEqual(self.actor(), original_actor)
        self.assertEqual(self.oidc(self.client, "subject-a", "gateway-a").status_code, 302)
        self.assertEqual(self.actor(), original_actor)
        self.assertEqual(self.auto("gateway-b", self.client_for_app()).status_code, 200)
        with sqlite3.connect(self.db) as active_db:
            self.assertEqual(active_db.execute("SELECT count(*) FROM lzc_identity_bindings").fetchone()[0], 2)
        self.assertFalse(pending_path.exists())

    def test_invalid_or_expired_old_sessions_fail_closed(self):
        for value in ("garbage", legacy_cookie({"sub": "a", "expires_at": 1}),
                      legacy_cookie({"email": "a@example.test", "expires_at": int(time.time()) + 600}),
                      legacy_cookie({"sub": "a", "expires_at": "bad"}),
                      legacy_cookie({"sub": "a", "expires_at": int(time.time()) + 600}) + "x"):
            self.client.cookies.set(auth_compat.OIDC_COOKIE, value, domain="bilisum.example.test")
            self.assertEqual(self.client.get("/api/v1/who").status_code, 401)
            self.assertFalse(self.client.get(AUTH_PATH + "/status").json()["authenticated"])

    def test_missing_and_untrusted_gateway_proof_do_not_create_a_session(self):
        url = AUTH_PATH + "/auto-session"
        for bad in ({}, {"X-Requested-With": "LazyCat-Login"}, {"X-Bilisum-Uid": "alice"},
                    {**headers("alice"), "X-Bilisum-Grant": "incorrect"},
                    {**headers("alice"), "Origin": "https://other.example.test"},
                    {**headers("alice"), "Sec-Fetch-Site": "cross-site"},
                    {**headers("alice"), "X-Requested-With": ""}):
            self.assertIn(self.client.post(url, headers=bad).status_code, (401, 403))
        self.assertFalse(self.db.exists())
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("alice")).status_code, 401)

    def test_fresh_gateway_sessions_can_bind_and_fall_back_to_oidc(self):
        me, other = self.client, self.client_for_app()
        login = self.auto("gateway-a")
        self.assertEqual(login.status_code, 200)
        cookie = next(value for value in login.headers.get_list("set-cookie") if value.startswith(auth_compat.GATEWAY_COOKIE))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn(GRANT, str(login.headers) + login.text)
        actor = self.actor()
        self.assertTrue(actor.startswith("inject:"))
        self.assertEqual(self.auto("gateway-b", other).status_code, 200)
        self.assertNotEqual(self.actor(other), actor)
        self.assertEqual(self.oidc(me, "subject-a", "gateway-a").status_code, 302)
        self.assertEqual(self.actor(), actor)
        me.post(AUTH_PATH + "/logout")
        self.assertEqual(self.oidc(me, "subject-a").status_code, 302)
        self.assertEqual(self.actor(), actor)
        self.assertEqual(me.get(AUTH_PATH + "/status").json()["authMethod"], "oidc")
        self.assertEqual(self.oidc(other, "subject-a", "gateway-b").status_code, 409)
        self.assertEqual(self.oidc(other, "subject-b", "gateway-b").status_code, 302)
        self.assertNotEqual(self.actor(other), actor)

    def test_native_session_entrypoint_preserves_explicit_bearer_auth(self):
        session = AUTH_PATH + "/session"
        self.assertEqual(self.client.post(session, headers=headers("gateway-a")).json()["authMethod"], "inject")
        self.assertTrue(self.actor().startswith("inject:"))
        for invalid in ("Bearer invalid", "Bearer", "Basic invalid"):
            denied = self.client.post(session, headers={**headers("gateway-a"), "Authorization": invalid})
            self.assertEqual(denied.status_code, 401)
        shared = self.client.post(session, headers={**headers("gateway-a"), "Authorization": "Bearer " + TOKEN})
        self.assertEqual(shared.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/who", headers=headers("gateway-a")).json(), "access-token:shared")
        self.assertEqual(self.client.post(session, headers={"X-Requested-With": "LazyCat-Login"}).status_code, 401)

    def test_oidc_first_on_fresh_install_preserves_private_owner(self):
        self.assertEqual(self.oidc(self.client, "subject-a").status_code, 302)
        self.assertEqual(self.actor(), "oidc:subject-a")
        status = self.client.get(AUTH_PATH + "/status").json()
        self.assertEqual(status["user"], {"sub": "subject-a", "name": "相同名字",
                                          "email": "same@example.test", "role": "ADMIN"})
        self.client.post(AUTH_PATH + "/logout")
        self.assertEqual(self.auto("gateway-a").status_code, 409)
        self.assertEqual(self.oidc(self.client, "subject-a", "gateway-a").status_code, 302)
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        self.assertEqual(self.actor(), "oidc:subject-a")

    def test_upgrade_needs_verified_subject_and_binds_idempotently(self):
        self.simulate_upgrade()
        self.assertEqual(self.auto("gateway-a").status_code, 409)
        for _ in range(2):
            self.assertEqual(self.oidc(self.client, "subject-a", "gateway-a").status_code, 302)
            self.assertEqual(self.auto("gateway-a").status_code, 200)
            self.assertEqual(self.actor(), "oidc:subject-a")
        self.assertEqual(self.auto("gateway-b").status_code, 409)
        self.assertEqual(self.oidc(self.client, "subject-a", "gateway-b").status_code, 409)
        self.assertEqual(self.oidc(self.client, "subject-b", "gateway-a").status_code, 409)
        self.assertEqual(self.oidc(self.client, "subject-b", "gateway-b").status_code, 302)
        self.assertEqual(self.auto("gateway-b").status_code, 200)
        self.assertEqual(self.actor(), "oidc:subject-b")

    def test_oidc_state_requires_same_browser_and_gateway_principal(self):
        login = self.client.get(AUTH_PATH + "/oidc/login", headers=headers("gateway-a"))
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        url = AUTH_PATH + "/oidc/callback"
        with patch.object(auth_compat.httpx, "post", side_effect=AssertionError("invalid state reached provider")):
            for client, gateway, candidate in (
                (self.client, "gateway-b", state), (self.client, None, state),
                (self.client, "gateway-a", "wrong"), (self.client_for_app(), "gateway-a", state),
            ):
                res = client.get(url, headers=headers(gateway) if gateway else {},
                                 params={"state": candidate, "code": "fixture-code"})
                self.assertEqual(res.status_code, 400)

    def test_concurrent_first_login_has_a_single_durable_binding(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            actors = list(pool.map(lambda _: auth_compat._resolve_binding(self.db, "gateway-a"), range(24)))
        self.assertEqual(len(set(actors)), 1)
        self.assertEqual(self.auto("gateway-a").status_code, 200)
        self.assertEqual(self.actor(), actors[0])


if __name__ == "__main__":
    unittest.main()
