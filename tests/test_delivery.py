"""Offline regression checks for the packaging entry point used by lzc-build.yml."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_delivery


class DeliveryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.proof = {
            "staging": "ttl.sh/lazycat-bilisum-0123456789ab:24h",
            "digest": "sha256:" + "1" * 64,
            "image_id": "sha256:" + "2" * 64,
            "application_version": "1.21.3",
        }
        self.runtime = "registry.lazycat.cloud/peter/lazycat-bilisum-0123456789ab:3456789abcdef012"
        self.manifest = (
            "services:\n  bilisum:\n"
            f"    # upstream: {self.proof['staging']}@{self.proof['digest']}\n"
            f"    image: {self.runtime}\n"
        )
        self.package = "package: cloud.lazycat.app.bilisum1\nversion: 1.21.3\n"

    def test_official_copy_proof_works_without_registry_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".lazycat-action").mkdir()
            (root / ".lazycat-action/bilisum-verification.json").write_text(json.dumps(self.proof))
            (root / "lzc-manifest.yml").write_text(self.manifest)
            (root / "package.yml").write_text(self.package)
            with patch.object(verify_delivery, "ROOT", root), redirect_stdout(io.StringIO()), \
                 patch("subprocess.Popen", side_effect=AssertionError("Unexpected registry subprocess")), \
                 patch("socket.create_connection", side_effect=AssertionError("Unexpected network connection")):
                verify_delivery.main()

    def test_substituted_digest_runtime_or_package_is_rejected(self):
        for manifest, package in (
            (self.manifest.replace(self.proof["digest"], "sha256:" + "3" * 64), self.package),
            (self.manifest.replace(self.runtime, self.proof["staging"]), self.package),
            (self.manifest.replace(self.runtime, self.runtime.replace("0123456789ab:", "111111111111:")), self.package),
            (self.manifest.replace(self.runtime, self.runtime.replace("3456789abcdef012", "latest")), self.package),
            (self.manifest, self.package.replace("1.21.3", "1.21.4")),
        ):
            with self.subTest(manifest=manifest, package=package), self.assertRaises(ValueError):
                verify_delivery.check(manifest, package, self.proof)


if __name__ == "__main__":
    unittest.main()
