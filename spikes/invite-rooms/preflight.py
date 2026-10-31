"""Fail-closed, no-launch preflight for the Invite-only Rooms spike.

This module only provisions disposable files and runs read-only native probes in
short-lived children. It never starts a gateway, binds a listener, calls a model,
or contacts a relay.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
from typing import Any, Mapping

SPIKE_ROOT = Path("/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930")
APPROVED_RUNTIME_ROOT = SPIKE_ROOT / "preflight-homes"
HERMES_AGENT_ROOT = Path("/Users/albi/.hermes/hermes-agent")
MANAGED_PYTHON = HERMES_AGENT_ROOT / "venv/bin/python"
INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
GATEWAY_ID_RE = re.compile(r"^install:[0-9a-f]{32}$")
DENIED_ENV_PREFIXES = ("ROOM_", "PREFLIGHT_")
DENIED_ENV_NAMES = {
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "HERMES_PROFILE", "HERMES_AUTH", "HERMES_SOUL", "HONCHO_HOME", "MEMORY_HOME",
    "HERMES_CONTEXT", "OPENROUTER_API_KEY", "API_SERVER_KEY",
}
SAFE_PASSTHROUGH_ENV = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "PYTHONUTF8"}
_PROBE_TIMEOUT = 120


class PreflightBlocked(RuntimeError):
    """A required isolation or native contract could not be proven."""


def _resolved_contained(candidate: Path, root: Path) -> Path:
    """Resolve an existing or not-yet-created child without touching the candidate."""
    raw_parts = candidate.expanduser().parts
    if ".." in raw_parts:
        raise PreflightBlocked("path traversal component is forbidden")
    root_resolved = root.expanduser().resolve(strict=True)
    candidate_resolved = candidate.expanduser().resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PreflightBlocked(f"home escapes approved root: {candidate_resolved}") from exc
    if candidate_resolved == root_resolved:
        raise PreflightBlocked("approved root itself is not a scoped home")
    return candidate_resolved


def validate_scoped_home(home: str | Path, *, approved_root: Path = APPROVED_RUNTIME_ROOT) -> Path:
    """Validate exact resolved containment before any mkdir or write."""
    root = approved_root.expanduser().resolve(strict=True)
    candidate = Path(home)
    return _resolved_contained(candidate, root)


def _path_inside(path: Path, home: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(home.resolve(strict=True))
        return True
    except ValueError:
        return False


def isolated_child_env(home: Path) -> dict[str, str]:
    """Return an exact child environment; never mutate or copy arbitrary caller keys."""
    home = home.resolve(strict=True)
    runtime, tmp = home / "runtime", home / "tmp"
    if not _path_inside(runtime, home) or not _path_inside(tmp, home):
        raise PreflightBlocked("runtime paths are outside scoped home")
    env = {key: value for key, value in os.environ.items() if key in SAFE_PASSTHROUGH_ENV and key != "PATH"}
    # Do not inherit an operator's arbitrary executable search path.
    env["PATH"] = os.defpath
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "HERMES_RUNTIME_DIR": str(runtime),
        "TMPDIR": str(tmp),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        # Every lab child (probes AND native gateway/TUI processes) runs on the checkout's
        # interpreter. Without this, core treats each fresh scratch HERMES_HOME as a new
        # data root and downloads a ~140 MB isolated runtime into it first: network-bound,
        # it raced between gateway and TUI ("an update is still running") and timed out
        # the s2g native gate, and it is where the per-home GBs came from. Same bridge var
        # the core hermetic tests use.
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
    })
    for key in env:
        if key.startswith(DENIED_ENV_PREFIXES) or key in DENIED_ENV_NAMES:
            raise PreflightBlocked(f"denied environment key survived: {key}")
    for key in ("HOME", "HERMES_HOME", "HERMES_RUNTIME_DIR", "TMPDIR"):
        if not _path_inside(Path(env[key]), home):
            raise PreflightBlocked(f"environment path escapes scoped home: {key}")
    return env


_NATIVE_PROBE = r'''
import json
from hermes_cli.install_identity import get_install_id
from gateway.hosted_rooms import local_authority_gateway_id
print(json.dumps({"install_id": get_install_id(), "gateway_id": local_authority_gateway_id()}))
'''

_POLICY_PROBE = r'''
import json
from gateway.hosted_room_execution_policy import execution_policy_mapping
print(json.dumps(execution_policy_mapping(target_profile="default"), sort_keys=True))
'''


def _native_probe(home: Path, script: str) -> dict[str, Any]:
    env = isolated_child_env(home)
    if not MANAGED_PYTHON.is_file() or not os.access(MANAGED_PYTHON, os.X_OK):
        raise PreflightBlocked(f"managed interpreter is unavailable: {MANAGED_PYTHON}")
    try:
        # Own process group: on timeout the whole probe tree dies, never orphaned
        # grandchildren that keep downloading into (or holding) a scratch home.
        child = subprocess.Popen(
            [str(MANAGED_PYTHON), "-c", script], cwd=str(HERMES_AGENT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
    except OSError as exc:
        raise PreflightBlocked(f"native probe did not complete: {exc}") from exc
    try:
        stdout, stderr = child.communicate(timeout=_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise PreflightBlocked(f"native probe did not complete: {exc}") from exc
    result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    if result.returncode != 0:
        raise PreflightBlocked(f"native probe failed: {result.stderr.strip() or 'no diagnostic'}")
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise PreflightBlocked("native probe returned non-JSON output") from exc
    if not isinstance(payload, dict):
        raise PreflightBlocked("native probe returned invalid payload")
    return payload


def native_identity(home: Path) -> dict[str, str]:
    """Resolve the actual native identity under this child HERMES_HOME."""
    payload = _native_probe(home, _NATIVE_PROBE)
    install_id = payload.get("install_id")
    gateway_id = payload.get("gateway_id")
    if (not isinstance(install_id, str) or not INSTALL_ID_RE.fullmatch(install_id)
            or not isinstance(gateway_id, str) or not GATEWAY_ID_RE.fullmatch(gateway_id)
            or gateway_id != f"install:{install_id}"):
        raise PreflightBlocked("native installation identity has an invalid native format")
    return {"install_id": install_id, "gateway_id": gateway_id}


def effective_native_policy(home: Path) -> dict[str, Any]:
    """Read back the effective native policy from the scoped config, not constants."""
    config_path = home / "config.yaml"
    if not config_path.is_file() or not config_path.read_text(encoding="utf-8").strip():
        raise PreflightBlocked("empty or missing isolated config is not safe")
    payload = _native_probe(home, _POLICY_PROBE)
    if set(payload) != {"approval_mode", "enabled_toolsets", "max_iterations", "policy_digest", "target_profile", "version"}:
        raise PreflightBlocked("native policy shape is unknown")
    if payload["approval_mode"] != "manual":
        raise PreflightBlocked("native approvals are not manual")
    if (payload["version"] != 1 or payload["target_profile"] != "default"
            or not isinstance(payload["enabled_toolsets"], list)
            or any(not isinstance(item, str) for item in payload["enabled_toolsets"])
            or not isinstance(payload["max_iterations"], int)
            or isinstance(payload["max_iterations"], bool)
            or not isinstance(payload["policy_digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["policy_digest"])):
        raise PreflightBlocked("effective native policy has malformed values")
    if payload["enabled_toolsets"] != ["bot_room"]:
        raise PreflightBlocked(f"effective toolsets are not text-only: {payload['enabled_toolsets']!r}")
    if payload["max_iterations"] != 1:
        raise PreflightBlocked("native turn limit is not bounded to one iteration")
    return payload


def check_port_available(port: int) -> dict[str, Any]:
    """Measure availability only; never label an unstarted service ready or hold a socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return {"port": port, "available": True, "reserved": False, "capability_ready": False}
    except OSError as exc:
        return {"port": port, "available": False, "reserved": False, "capability_ready": False, "error": str(exc)}
    finally:
        sock.close()


def _write_safe_config(home: Path) -> None:
    # This is deliberately explicit: an absent/empty config would resolve platform defaults.
    config = """platform_toolsets:\n  api_server: []\napprovals:\n  mode: manual\nagent:\n  max_turns: 1\n  disabled_toolsets:\n    - terminal\n    - file\n    - web\n    - browser\n    - vision\n    - image_gen\n    - skills\n    - cronjob\n    - messaging\n    - mcp\n"""
    target = home / "config.yaml"
    if not _path_inside(target, home):
        raise PreflightBlocked("config target escapes scoped home")
    target.write_text(config, encoding="utf-8")


def run_preflight(home: str | Path, *, port: int = 0) -> dict[str, Any]:
    """Provision and check one isolated scope, with no gateway/model/network launch."""
    if not APPROVED_RUNTIME_ROOT.exists():
        raise PreflightBlocked("approved runtime root must already exist")
    scoped = validate_scoped_home(home)
    port_result = check_port_available(port)
    if not port_result["available"]:
        raise PreflightBlocked(f"requested lab port is unavailable: {port_result.get('error', 'unknown error')}")
    # Validation is complete before the first mkdir/write.
    scoped.mkdir(parents=True, exist_ok=True)
    for name in ("runtime", "tmp"):
        (scoped / name).mkdir()
    _write_safe_config(scoped)
    env = isolated_child_env(scoped)
    identity_a = native_identity(scoped)
    second = validate_scoped_home(scoped / "second")
    second.mkdir(parents=True, exist_ok=True)
    (second / "runtime").mkdir()
    (second / "tmp").mkdir()
    _write_safe_config(second)
    identity_b = native_identity(second)
    if identity_a["gateway_id"] == identity_b["gateway_id"]:
        raise PreflightBlocked("native gateway identities are not distinct")
    policy = effective_native_policy(scoped)
    return {
        "preflight_ok": True,
        "home": str(scoped),
        "env_keys": sorted(env),
        "identity": {"gateway_id": identity_a["gateway_id"], "second_gateway_id": identity_b["gateway_id"]},
        "effective_policy": policy,
        "port": port_result,
        "startup_contract": "hermes serve --isolated --host 127.0.0.1 --port 0 --skip-build (documented only; not launched)",
        "execution": {"gateway_started": False, "model_called": False, "listener_held": False, "relay_started": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    try:
        print(json.dumps(run_preflight(args.home, port=args.port), indent=2, sort_keys=True))
        return 0
    except (OSError, PreflightBlocked) as exc:
        print(json.dumps({"preflight_ok": False, "blocker": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
