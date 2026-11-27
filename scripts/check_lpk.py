"""Inspect a generated LPK without unpacking into the working tree. Requires PyYAML."""
import argparse
import io
from pathlib import PurePosixPath
import tarfile

import yaml


def check(path):
    with tarfile.open(path) as package:
        manifest = yaml.safe_load(package.extractfile("manifest.yml"))
        metadata = yaml.safe_load(package.extractfile("package.yml"))
        contents = package.extractfile("content.tar").read()
        assert package.extractfile("icon.png").read(8) == b"\x89PNG\r\n\x1a\n"
    assert metadata["package"] == "cloud.lazycat.app.bilisum1"
    app = manifest["application"]
    assert app["multi_instance"] and app["oidc_redirect_path"] == "/api/v1/auth/oidc/callback"
    assert not app.get("public_path")
    assert all(svc["image"].startswith("registry.lazycat.cloud/") for svc in manifest["services"].values())
    service = manifest["services"]["bilisum"]
    assert "/lzcapp/var/data:/data" in service["binds"]
    env = dict(item.split("=", 1) for item in service["environment"])
    assert env["VIDEO_SUM_SESSION_SECRET"] == '{{ stable_secret "session_secret" }}'
    rules = {r["id"]:r for r in app["injects"]}
    grant = rules["bilisum-auth-gateway"]
    assert grant["on"] == "request" and grant["do"][0]["params"]["grant"] == env["BILISUM_INJECT_SECRET"]
    login = rules["bilisum-login"]
    assert login["on"] == "browser" and login["do"][0]["params"]["sessionPath"] == "/api/v1/auth/session"
    assert rules["lazycat-file-bridge"]["on"] == "request"
    assert rules["bilisum-file-chooser"]["on"] == "browser"
    prefix = "file:///lzcapp/pkg/content/"
    with tarfile.open(fileobj=io.BytesIO(contents)) as content:
        files = {str(PurePosixPath(m.name)):m for m in content.getmembers() if m.isfile()}
        for rule in rules.values():
            if rule["on"] != "browser":
                continue
            for action in rule["do"]:
                assert "stable_secret" not in str(action)
                filename = action["src"].removeprefix(prefix)
                assert filename in files, filename
                assert content.extractfile(files[filename]).read()
    print(f"Verified packaged {metadata['package']} {metadata['version']}, login, OIDC and file bridge")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lpk")
    check(parser.parse_args().lpk)
