"""Check the executor selected and copied exactly the tested image before building an LPK."""
import json
import re

from prepare import DIGEST, ROOT, output, platform_digest


def one_field(text, name):
    values = re.findall(r"^\s*" + re.escape(name) + r":\s*['\"]?([^\s'\"#]+)['\"]?\s*$", text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"Expected exactly one {name} in the final manifest or package")
    return values[0]


def check(manifest, package, proof):
    digest = proof["digest"]
    if not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(proof["image_id"]):
        raise ValueError("Invalid verification receipt")
    if not re.fullmatch(r"ttl\.sh/lazycat-bilisum-[0-9a-f]{12}:24h", proof["staging"]):
        raise ValueError("Unexpected staged image")
    upstream = re.findall(r"^\s*# upstream:\s+(\S+)\s*$", manifest, re.MULTILINE)
    if upstream != [proof["staging"] + "@" + digest]:
        raise ValueError("The executor selected a different staging digest from the upgrade test")
    runtime = one_field(manifest, "image")
    if not re.fullmatch(r"registry\.lazycat\.cloud/[A-Za-z0-9._:/@-]+", runtime):
        raise ValueError("The runtime image has not been copied to the official registry")
    if (one_field(package, "package") != "cloud.lazycat.app.bilisum1"
            or one_field(package, "version") != proof["application_version"]):
        raise ValueError("Package/version mismatch with the upgrade receipt")
    return runtime


def main():
    receipt = ROOT / ".lazycat-action/bilisum-verification.json"
    if not receipt.is_file():
        raise SystemExit("Missing upgrade and image-copy receipt. Build the release through lazycat-action.")
    proof = json.loads(receipt.read_text())
    runtime = check((ROOT / "lzc-manifest.yml").read_text(), (ROOT / "package.yml").read_text(), proof)
    repository = runtime.split("@", 1)[0]
    manifest = json.loads(output("docker", "buildx", "imagetools", "inspect", "--raw",
                                 repository + "@" + platform_digest(runtime)))
    if manifest["config"]["digest"] != proof["image_id"]:
        raise SystemExit("The official registry image differs from the tested local image")
    print("Verified the LazyCat registry image against the candidate that passed the upgrade test")


if __name__ == "__main__":
    main()
