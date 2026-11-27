"""Validate the tested source and the shared executor's official delivery reference."""
import json
import re

from prepare import DIGEST, ROOT


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
    # The copy API preserves the staging repository name and returns a
    # content-derived 16-hex tag under the developer's LazyCat namespace.
    staged_name = proof["staging"].split("/")[1].split(":")[0]
    official_tag = rf"registry\.lazycat\.cloud/(?:[a-z0-9][a-z0-9._-]*/)+{re.escape(staged_name)}:[0-9a-f]{{16}}"
    if not re.fullmatch(official_tag, runtime):
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
    # prepare.py checked the config ID of the staged image. LazyCat records
    # provenance labels during copying, which changes its official config ID.
    print(f"Verified the tested staging digest and LazyCat delivery reference {runtime}")


if __name__ == "__main__":
    main()
