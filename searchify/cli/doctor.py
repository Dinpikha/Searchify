"""Non-mutating runtime and connectivity diagnostics."""

import sys

from searchify.cli.output import line
from searchify.cli.workspace import local_paths
from searchify.services.health_service import HealthService


def run(args):
    db, reports = local_paths()
    snapshot = HealthService(db, reports).snapshot(offline=args.offline, auth=args.auth)
    line("SEARCHIFY DOCTOR")
    line("Python runtime: " + sys.executable)
    for dependency, available in snapshot["dependencies"].items():
        line(f"{dependency}: {'available' if available else 'missing or incompatible'}")
    line("Local database: " + snapshot["local_database"])
    for provider, value in snapshot["providers"].items():
        status = value.get("http_status")
        line(f"{provider}: {value['status']}" + (f" (HTTP {status})" if status else ""))
    line("Authentication: " + snapshot["authentication"]["status"])
    line(
        f"Embedding schema compatibility: configured {snapshot['models']['dimensions']}; required 768"
    )
    for issue in snapshot["errors"]:
        line("Issue: " + issue)
    if any(not available for available in snapshot["dependencies"].values()):
        line("Use the intended environment and run: python -m pip install -e .")
    if snapshot["providers"]["ollama"]["status"] == "unavailable":
        line(
            "Start the Ollama app or run ollama serve; verify the configured models are installed."
        )
    if not args.auth:
        line(
            "Use doctor --auth to inspect the cached keyring session without refreshing it (macOS may request Keychain access)."
        )
    auth_bad = args.auth and snapshot["authentication"].get("database") != "available"
    fatal_errors = [e for e in snapshot["errors"] if "not checked" not in e]
    return 1 if fatal_errors or auth_bad else 0
