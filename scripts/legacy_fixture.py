"""Record and replay disposable data produced by BiliSum's 1.19.2 store image."""

import base64
import hashlib
import hmac
from http.cookiejar import Cookie
import json
from pathlib import Path
import secrets
import time

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v1.19.2"
COOKIE = "bilisum_oidc_session"


def sign_payload(payload, key):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()
    return encoded.decode() + "." + signature


def record(docker, container, baseline, ids, shared_id, clients, signing_key):
    # A SQLite backup includes committed WAL data. The text dump contains only
    # the synthetic requests made by this test on its newly allocated volume.
    sql = docker("exec", container, "python", "-c", """
import sqlite3
source = sqlite3.connect("file:/data/video_sum.db?mode=ro", uri=True)
snapshot = sqlite3.connect(":memory:")
source.backup(snapshot)
print("\\n".join(snapshot.iterdump()))
""") + "\n"
    FIXTURE.mkdir(exist_ok=True, parents=True)
    payloads = {}
    for subject, client in clients.items():
        value = next(cookie.value for cookie in client.jar if cookie.name == COOKIE)
        encoded = value.split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(encoded + "==="))
        if value != sign_payload(payload, signing_key):
            raise ValueError("The fixture signer differs from the original 1.19.2 cookie")
        payloads[subject] = {k: v for k, v in payload.items() if k not in ("issued_at", "expires_at")}
    metadata = {"baseline": baseline, "sha256": hashlib.sha256(sql.encode()).hexdigest(),
                "ids": ids, "shared_id": shared_id, "oidc_profiles": payloads}
    (FIXTURE / "database.sql").write_bytes(sql.encode())
    (FIXTURE / "state.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def set_test_cookie(client, name, value):
    client.jar.set_cookie(Cookie(0, name, value, None, False, "127.0.0.1", False, False,
                                 "/", True, False, None, True, None, None, {"HttpOnly": None}))


def restore(docker, candidate, volume, baseline, clients, shared, signing_key):
    metadata = json.loads((FIXTURE / "state.json").read_text())
    sql = (FIXTURE / "database.sql").read_bytes()
    if metadata["baseline"] != baseline or hashlib.sha256(sql).hexdigest() != metadata["sha256"]:
        raise ValueError("The 1.19.2 upgrade fixture does not match its baseline or checksum")
    native_key = secrets.token_urlsafe(32)
    docker("run", "--rm", "--network=none", "-i", "-v", volume + ":/data", "--entrypoint", "python",
           candidate, "-c", """
import json, sqlite3, sys
from pathlib import Path
data = json.load(sys.stdin)
with sqlite3.connect("/data/video_sum.db") as db:
    db.executescript(data["sql"])
Path("/data/auth.json").write_text(json.dumps({"access_token": data["access_token"]}))
""", input=json.dumps({"sql": sql.decode(), "access_token": native_key}))
    set_test_cookie(shared, "bilisum_session", native_key)
    now = int(time.time())
    for subject, client in clients.items():
        payload = {**metadata["oidc_profiles"][subject], "issued_at": now, "expires_at": now + 3600}
        set_test_cookie(client, COOKIE, sign_payload(payload, signing_key))
    return metadata["ids"], metadata["shared_id"]
