"""Exercise the actual store container and the exact candidate using disposable Docker volumes."""
from contextlib import ExitStack, contextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, Request

BASELINE = "registry.lazycat.cloud/edward/lazycat_9527/bilisum@sha256:5551af52b741087fc105f5cee00c637f8f80e03436409171f8ae7f779c0eb6b2"
AUTH = "/api/v1/auth/"
CONVERSATIONS = "/api/v1/knowledge/conversations"


class TestIssuer(BaseHTTPRequestHandler):
    """Simulates the device's server-side code exchange/userinfo with two test subjects."""
    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if self.path != "/token" or form.get("code") not in (["alice"], ["bob"]):
            return self.send_error(400)
        self.send_json({"access_token": form["code"][0], "token_type": "Bearer"})

    def do_GET(self):
        subject = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.path != "/userinfo" or subject not in ("alice", "bob"):
            return self.send_error(401)
        self.send_json({"sub":subject, "name":subject, "groups":["NORMAL"]})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, grant, user):
        self.grant = grant
        self.user = user
        # Docker publishes HTTP only on loopback. Production sets Secure cookies behind HTTPS.
        self.jar = CookieJar(policy=DefaultCookiePolicy(secure_protocols=("https", "http")))
        self.http = build_opener(HTTPCookieProcessor(self.jar), NoRedirect())

    def request(self, path, status=200, method="GET", body=None, headers=None):
        extra = {"X-Bilisum-Grant":self.grant, "X-Bilisum-Uid":self.user, **(headers or {})}
        data = None
        if body is not None:
            extra["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(self.base + path, method=method, data=data, headers=extra)
        try:
            response = self.http.open(req, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            if response.status != status:
                raise AssertionError(f"{method} {path} expected {status}, got {response.status}: {raw[:300]!r}")
            if 300 <= status < 400:
                return response.headers["Location"]
            return json.loads(raw) if raw and "json" in response.headers.get("Content-Type", "") else raw

    def oidc(self, subject):
        location = self.request(AUTH + "oidc/login", 302)
        state = parse_qs(urlsplit(location).query)["state"][0]
        self.request(AUTH + "oidc/callback?" + urlencode({"state":state,"code":subject}), 302)
        assert self.request(AUTH + "status")["authMethod"] == "oidc"

    def inject(self, expected=200):
        return self.request(AUTH + "session", expected, "POST",
                            headers={"X-Requested-With":"LazyCat-Login"})


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, stdout=subprocess.PIPE,
                          text=True, **kwargs).stdout.strip()


@contextmanager
def service(image, name, volume, network, env):
    container = docker("run", "-d", "--network", network, "-p", "127.0.0.1::3838",
                       "-v", volume + ":/data", "--name", name, *env, image)
    try:
        port = docker("port", container, "3838/tcp").splitlines()[0].rsplit(":",1)[1]
        base = "http://127.0.0.1:" + port
        http = build_opener()
        for attempt in range(120):
            try:
                with http.open(base + "/health", timeout=2) as result:
                    if result.status == 200:
                        break
            except (OSError, URLError):
                pass
            if docker("inspect","-f","{{.State.Running}}",container) == "false":
                raise RuntimeError("BiliSum exited during startup")
            time.sleep(1)
        else:
            raise RuntimeError("BiliSum readiness timed out")
        yield base
    except Exception:
        print(docker("logs","--tail","24",container), file=sys.stderr)
        raise
    finally:
        docker("stop","--timeout","8",container)
        docker("rm","-fv",container)


def verify(candidate):
    prefix = "bilisum-check-" + secrets.token_hex(5)
    grant = secrets.token_urlsafe(32)
    env = []
    for k, v in {
        "BILISUM_DEPLOY_UID":"box-a", "BILISUM_INJECT_SECRET":grant, "VIDEO_SUM_SESSION_SECRET":secrets.token_hex(32),
        "VIDEO_SUM_OIDC_REDIRECT_URI":"https://app.test/api/v1/auth/oidc/callback",
        "LAZYCAT_AUTH_OIDC_CLIENT_ID":"test-client","LAZYCAT_AUTH_OIDC_CLIENT_SECRET":"fixture",
        "LAZYCAT_AUTH_OIDC_ISSUER_URI":"http://issuer:8099","LAZYCAT_AUTH_OIDC_AUTH_URI":"http://issuer:8099/auth",
        "LAZYCAT_AUTH_OIDC_TOKEN_URI":"http://issuer:8099/token","LAZYCAT_AUTH_OIDC_USERINFO_URI":"http://issuer:8099/userinfo",
    }.items():
        env.extend(["-e",k+"="+v])
    alice, bob = Client(grant,"box-a"), Client(grant,"box-b")
    with ExitStack() as resources:
        docker("network","create",prefix)
        resources.callback(docker,"network","rm",prefix)
        volumes = [prefix + "-upgrade", prefix + "-fresh"]
        for vol in volumes:
            docker("volume","create",vol)
            resources.callback(docker,"volume","rm",vol)
        # Pass the fixture as a read-only file; a detached container's stdin is not a reliable transfer channel.
        issuer = docker("run","-d","--network",prefix,"--network-alias","issuer",
                        "-v",str(Path(__file__).resolve())+":/fixture.py:ro",
                        "--entrypoint","python",candidate,"/fixture.py","issuer")
        resources.callback(docker,"rm","-fv",issuer)
        with service(BASELINE, prefix+"-old", volumes[0],prefix,env) as base:
            alice.base = bob.base = base
            shared = Client(grant, "box-a")
            shared.base = base
            shared.request(AUTH + "status")
            legacy_key = docker("exec", prefix+"-old", "python", "-c",
                                "import json; print(json.load(open('/data/auth.json'))['access_token'])")
            shared.request(AUTH + "session", 200, "POST", headers={"Authorization":"Bearer " + legacy_key})
            shared_id = shared.request(CONVERSATIONS, 200, "POST", {"title":"shared-key history"})["conversation_id"]
            ids = {}
            for client, subject in ((alice,"alice"),(bob,"bob")):
                client.oidc(subject)
                conv = client.request(CONVERSATIONS,200,"POST",{"title":subject})
                cid = conv["conversation_id"]
                ids[subject] = cid
                messages = [{"id":subject+str(i),"role":role,"content":subject+"-"+role}
                            for i,role in enumerate(("user","assistant"))]
                client.request(CONVERSATIONS+"/"+cid,200,"PUT",{"title":subject,"messages":messages})
        with service(candidate,prefix+"-new",volumes[0],prefix,env) as base:
            shared.base = base
            assert shared.request(AUTH+"status")["authMethod"] == "access_token"
            assert [r["conversation_id"] for r in shared.request(CONVERSATIONS)] == [shared_id]
            for client, subject, other in ((alice,"alice","bob"),(bob,"bob","alice")):
                client.base = base
                status = client.request(AUTH+"status")
                if subject == "alice":
                    assert status["authMethod"] == "oidc", "old signed session on original owner instance"
                else:
                    # This fixture deliberately exercises a second owner on the same backend.
                    # The device normally gives each user an independent instance/domain.
                    client.oidc(subject)
                assert [x["conversation_id"] for x in client.request(CONVERSATIONS)] == [ids[subject]]
                msgs = client.request(CONVERSATIONS+"/"+ids[subject]+"/messages")["messages"]
                assert [m["content"] for m in msgs] == [subject+"-user",subject+"-assistant"]
                client.jar.clear()
                if subject == "alice":
                    client.inject(409)
                client.oidc(subject)
                client.jar.clear()
                assert client.inject()["authenticated"]
                assert client.request(AUTH+"status")["authMethod"] == "inject"
                client.request(CONVERSATIONS+"/"+ids[other],404)
                client.request(CONVERSATIONS+"/"+shared_id,404)
                client.request(CONVERSATIONS+"/"+ids[subject],200,"PATCH",{"title":subject+"-updated"})
                client.request(CONVERSATIONS,201,"POST",{"title":"新增-"+subject})
        with service(candidate,prefix+"-restart",volumes[0],prefix,env) as base:
            alice.base = base
            assert len(alice.request(CONVERSATIONS)) == 2
            assert len(alice.request(CONVERSATIONS+"/"+ids["alice"]+"/messages")["messages"]) == 2
        with service(candidate,prefix+"-fresh",volumes[1],prefix,env) as base:
            alice.base = bob.base = base
            alice.jar.clear()
            bob.jar.clear()
            alice.inject()
            cid = alice.request(CONVERSATIONS,201,"POST",{"title":"fresh"})["conversation_id"]
            bob.inject()
            assert bob.request(CONVERSATIONS) == []
            bob.request(CONVERSATIONS+"/"+cid,404)
            alice.oidc("alice")
            assert alice.request(CONVERSATIONS+"/"+cid)["title"] == "fresh"
        print("PASS: real 1.19.2 → candidate, old OIDC cookies, two owners, history, new writes, restart and fresh inject login")


if __name__ == "__main__":
    if sys.argv[1] == "issuer":
        ThreadingHTTPServer(("0.0.0.0",8099),TestIssuer).serve_forever()
    else:
        verify(sys.argv[1])
