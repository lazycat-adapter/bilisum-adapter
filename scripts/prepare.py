"""Build the selected BiliSum image, verify its upgrade, and stage the tested image."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_GIT = "https://github.com/lycohana/BiliSum.git"
UPSTREAM_IMAGE = "docker.io/lycohana/bilisum"
PLATFORM = "linux/amd64"
VERSION = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){2}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def output(*command):
    return subprocess.check_output(command, text=True).strip()


def selected_release(verify_only):
    version = os.environ.get("LAZYCAT_SOURCE_VERSION", "")
    if version and not VERSION.fullmatch(version):
        raise SystemExit("Expected LAZYCAT_SOURCE_VERSION in X.Y.Z format")
    if verify_only:
        # Dry runs have no executor checkout. Resolve the peeled commit for the
        # same stable tag rule, optionally limited to a specific test version.
        pattern = "refs/tags/v" + version if version else "refs/tags/v*"
        refs = {}
        for line in output("git", "ls-remote", "--tags", UPSTREAM_GIT, pattern, pattern + "^{}").splitlines():
            commit, ref = line.split()
            refs[ref] = commit
        versions = [ref.removeprefix("refs/tags/v") for ref in refs if ref.startswith("refs/tags/v")]
        stable = [v for v in versions if VERSION.fullmatch(v)]
        if not stable:
            raise SystemExit(f"No matching stable BiliSum Git tag: {pattern}")
        version = max(stable, key=lambda v: tuple(map(int, v.split("."))))
        ref = f"refs/tags/v{version}"
        revision = refs.get(ref + "^{}", refs[ref])
    else:
        if not VERSION.fullmatch(version) or os.environ.get("LAZYCAT_SOURCE_REF") != f"refs/tags/v{version}":
            raise SystemExit("Expected a stable BiliSum vX.Y.Z Git tag from the executor")
        checkout = Path(os.environ["LAZYCAT_SOURCE_DIR"])
        revision = os.environ["LAZYCAT_SOURCE_REVISION"]
        if output("git", "-C", checkout, "rev-parse", "HEAD") != revision or (checkout / "VERSION").read_text().strip() != version:
            raise SystemExit("The executor checkout does not match the selected tag and version")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise SystemExit(f"Unexpected Git commit for v{version}")
    return version, revision


def platform_digest(reference):
    manifest = json.loads(output("docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest}}"))
    if "manifests" in manifest:
        matches = [m["digest"] for m in manifest["manifests"]
                   if m.get("platform", {}).get("os") == "linux" and m["platform"].get("architecture") == "amd64"]
        if len(matches) != 1:
            raise SystemExit(f"Expected exactly one linux/amd64 manifest for {reference}")
        digest = matches[0]
    else:
        digest = manifest["digest"]
    if not DIGEST.fullmatch(digest):
        raise SystemExit(f"Invalid OCI manifest digest for {reference}")
    return digest


def make_runner_space():
    if (os.environ.get("GITHUB_ACTIONS"), os.environ.get("RUNNER_ENVIRONMENT"), os.environ.get("RUNNER_OS")) != ("true", "github-hosted", "Linux"):
        return
    # The real baseline and candidate coexist while testing shared persistent data.
    # These SDKs are preinstalled on disposable Linux runners, and unused by BiliSum.
    docker_root = Path(output("docker", "info", "--format", "{{.DockerRootDir}}"))
    target = 24 * 1024**3
    for directory in ("/usr/local/lib/android", "/usr/share/dotnet", "/usr/local/.ghcup", "/opt/ghc"):
        if shutil.disk_usage(docker_root).free >= target:
            break
        sdk = Path(directory)
        if sdk.is_dir() and sdk.stat().st_dev == docker_root.stat().st_dev:
            print(f"Reclaiming {sdk} on the GitHub-hosted runner", flush=True)
            subprocess.run(["sudo", "-n", "rm", "-rf", "--one-file-system", "--", str(sdk)], check=True)
    print(f"Docker storage available: {shutil.disk_usage(docker_root).free / 1024**3:.1f} GiB", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="build and test locally without staging")
    args = parser.parse_args()
    os.chdir(ROOT)
    if os.environ.get("LAZYCAT_TARGET_PLATFORM", PLATFORM) != PLATFORM:
        raise SystemExit("BiliSum is packaged for linux/amd64")
    version, revision = selected_release(args.verify_only)
    if not args.verify_only:
        staging = os.environ.get("LAZYCAT_OUTPUT_IMAGE", "")
        fingerprint = os.environ.get("LAZYCAT_BUILD_FINGERPRINT", "")
        if not DIGEST.fullmatch(fingerprint) or staging != f"ttl.sh/lazycat-bilisum-{fingerprint[7:19]}:24h":
            raise SystemExit("Expected a staging name derived from the executor's build fingerprint")
    make_runner_space()
    ref = f"{UPSTREAM_IMAGE}:{version}"
    base = ref + "@" + platform_digest(ref)
    print(f"Building BiliSum v{version} ({revision}) using {base}", flush=True)
    # Use the daemon's own builder and image store. The base contains large
    # inference libraries that would otherwise be loaded a second time.
    candidate = "bilisum-lazycat:" + uuid4().hex
    subprocess.run([
        "docker", "buildx", "build", "--builder", "default", "--load", "--provenance=false",
        "--platform", PLATFORM, "--tag", candidate, "--build-arg", "BASE_IMAGE=" + base,
        "--build-arg", "SOURCE_VERSION=" + version, "--build-arg", "SOURCE_REVISION=" + revision, "image",
    ], check=True)
    image_id = output("docker", "image", "inspect", "--format", "{{.Id}}", candidate)
    if not DIGEST.fullmatch(image_id):
        raise SystemExit("The candidate image was not loaded into Docker")
    subprocess.run(["docker", "tag", image_id, "bilisum-lazycat:verify-" + version], check=True)
    subprocess.run(["docker", "image", "rm", candidate], check=True)
    metadata = json.loads(output("docker", "image", "inspect", image_id))[0]
    if (metadata["Os"], metadata["Architecture"]) != ("linux", "amd64") or metadata["Config"]["Labels"]["org.opencontainers.image.base.name"] != base:
        raise SystemExit("Built image platform or pinned base disagrees with the selected release")
    actual_version = output("docker", "run", "--rm", "--network=none", "--entrypoint", "cat", image_id, "/app/VERSION")
    if actual_version != version:
        raise SystemExit(f"Image version {actual_version!r} does not match selected tag v{version}")
    subprocess.run([
        "docker", "run", "--rm", "--network=none", "--tmpfs", "/data",
        "--mount", f"type=bind,source={ROOT / 'tests'},target=/tests,readonly",
        "--mount", f"type=bind,source={ROOT / 'image'},target=/image,readonly",
        "--workdir", "/tests", "--entrypoint", "python", image_id,
        "-B", "-m", "unittest", "-v", "test_auth", "test_history", "test_route_guard", "test_runtime_probe",
    ], check=True)
    subprocess.run(["python3", "scripts/test_image.py", image_id], check=True,
                   env={**os.environ, "LAZYCAT_SOURCE_VERSION": version})
    if args.verify_only:
        print(f"Verified v{version} locally: {image_id}", flush=True)
        return

    subprocess.run(["docker", "tag", image_id, staging], check=True)
    subprocess.run(["docker", "push", staging], check=True)
    digest = platform_digest(staging)
    pushed = json.loads(output("docker", "buildx", "imagetools", "inspect", "--raw", staging + "@" + digest))
    # Docker's classic image store identifies the tested image by config, while
    # its containerd store identifies this single-platform image by manifest.
    config_digest = pushed["config"]["digest"]
    if not DIGEST.fullmatch(config_digest) or image_id not in (digest, config_digest):
        raise SystemExit("The staged manifest references a different image than the upgrade tests")
    result_dir = ROOT / ".lazycat-action"
    result_dir.mkdir(exist_ok=True)
    receipt = {
        "staging": staging,
        "digest": digest,
        "image_id": config_digest,
        "application_version": os.environ["LAZYCAT_APPLICATION_VERSION"],
    }
    with NamedTemporaryFile("w", dir=result_dir, delete=False) as report:
        json.dump(receipt, report)
        report.write("\n")
    os.replace(report.name, result_dir / "bilisum-verification.json")
    print(f"Verified staging image for LazyCat delivery: {staging}@{digest}", flush=True)


if __name__ == "__main__":
    main()
