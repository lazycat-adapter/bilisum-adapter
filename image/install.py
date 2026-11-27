"""Install the adapter patches in all copies of BiliSum's service package during the Docker build."""

from difflib import unified_diff
from importlib.util import find_spec
from pathlib import Path
import re
import shutil
import subprocess

ADAPTER = Path(__file__).resolve().parent


def sensitive_route(path):
    parts = set(re.findall(r"[a-z0-9]+", path.lower()))
    identity_or_history = {"auth", "oidc", "login", "logout", "session", "sessions", "conversation",
                           "conversations", "chat", "chats", "history", "message", "messages"}
    return bool(parts & identity_or_history) or ("knowledge" in parts and bool(parts & {"ask", "job", "jobs"}))


def verify_upstream_routes(routes, expected):
    from fastapi import routing
    from starlette.routing import Mount, WebSocketRoute

    # Newer FastAPI releases keep included routers nested until route resolution.
    if hasattr(routing, "iter_route_contexts"):
        routes = routing.iter_route_contexts(routes)
    actual = []
    for entry in routes:
        if not sensitive_route(entry.path):
            continue
        route = getattr(entry, "original_route", entry)
        if isinstance(route, Mount):
            actual.append(f"MOUNT {entry.path} {type(route.app).__name__}")
        elif isinstance(route, WebSocketRoute):
            actual.append(f"WEBSOCKET {entry.path}")
        else:
            actual.extend(f"{method} {entry.path}" for method in entry.methods)

    changes = list(unified_diff(expected, sorted(actual), fromfile="reviewed routes", tofile="candidate routes", lineterm=""))
    if changes:
        raise SystemExit("Review authentication and data ownership for changed BiliSum routes:\n" + "\n".join(changes))


def python_sources(directory):
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.py")}


def main():
    installed = Path(find_spec("video_sum_service").origin).resolve().parent
    source = Path("/app/apps/service/src/video_sum_service").resolve()
    if installed != source and python_sources(installed) != python_sources(source):
        raise SystemExit("Installed and bundled BiliSum service code differs; review both copies")
    from video_sum_service.app import app

    verify_upstream_routes(app.routes, (ADAPTER / "upstream-routes.txt").read_text().splitlines())
    for directory in dict.fromkeys((installed, source)):
        if not (directory / "app.py").is_file():
            raise SystemExit(f"Missing BiliSum service at {directory}")
        for feature in ("auth", "history", "runtime"):
            patch = ADAPTER / f"{feature}.patch"
            command = ["patch", "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch", "-p1", "-i", str(patch)]
            subprocess.run(command, cwd=directory, check=True)
        for feature in ("auth", "history"):
            module = f"{feature}_compat.py"
            if (directory / module).exists():
                raise SystemExit(f"Upstream now supplies {directory / module}; review the adapter")
            shutil.copyfile(ADAPTER / module, directory / module)


if __name__ == "__main__":
    main()
