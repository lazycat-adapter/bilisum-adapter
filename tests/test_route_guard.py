"""Changes to hidden history endpoints require review before shipping a new release."""
from pathlib import Path
import runpy
import unittest

from fastapi import FastAPI

verify_routes = runpy.run_path(str(Path(__file__).resolve().parents[1] / "image/install.py"))["verify_upstream_routes"]


class RouteGuardTests(unittest.TestCase):
    def test_hidden_history_route_requires_review(self):
        app = FastAPI()

        @app.get("/api/v1/videos/new-feature")
        def unrelated_feature():
            return {}

        verify_routes(app.routes, [])

        @app.post("/api/v1/knowledge/conversations/export", include_in_schema=False)
        def export_history():
            return {}

        with self.assertRaisesRegex(SystemExit, r"\+POST /api/v1/knowledge/conversations/export"):
            verify_routes(app.routes, [])
