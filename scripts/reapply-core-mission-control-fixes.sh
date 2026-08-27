#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # paths are runtime-computed via SCRIPT_DIR
source "$SCRIPT_DIR/lib/env.sh"
source "$SCRIPT_DIR/lib/restart-services.sh"
load_mission_control_env
MC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes/hermes-agent}"
HERMES_ROOT="${1:-$DEFAULT_HERMES_ROOT}"

WEB_SERVER="$HERMES_ROOT/hermes_cli/web_server.py"
VITE_CONFIG="$MC_ROOT/vite.config.ts"
SMOKE_SCRIPT="$MC_ROOT/scripts/smoke-upgrade.sh"

log() { echo "[mission-control-align] $*"; }
fail() { echo "[mission-control-align][FAIL] $*" >&2; exit 1; }

[[ -f "$WEB_SERVER" ]] || fail "Missing web_server.py at $WEB_SERVER"
[[ -f "$VITE_CONFIG" ]] || fail "Missing vite.config.ts at $VITE_CONFIG"
[[ -f "$SMOKE_SCRIPT" ]] || fail "Missing smoke script at $SMOKE_SCRIPT"

log "Aligning Mission Control against Hermes dashboard backend in: $HERMES_ROOT"

python3 - "$WEB_SERVER" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
original = text

old_require = '''def _require_token(request: Request) -> None:
    """Validate the ephemeral session token.  Raises 401 on mismatch.

    Uses ``hmac.compare_digest`` to prevent timing side-channels.
    """
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    if not hmac.compare_digest(auth.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")
'''
new_auth = '''def _accepted_bearer_tokens() -> List[str]:
    """Return bearer tokens accepted by the dashboard API.

    The built-in dashboard SPA receives the ephemeral session token injected
    into index.html. Mission Control can also run as a standalone Vite app, so
    it needs a stable operator-provided token. Prefer MISSION_CONTROL_TOKEN;
    fall back to API_SERVER_KEY for compatibility with the gateway API server.
    """
    tokens = [_SESSION_TOKEN]
    for env_name in ("MISSION_CONTROL_TOKEN", "API_SERVER_KEY"):
        value = os.getenv(env_name, "").strip()
        if value:
            tokens.append(value)
    return tokens


def _is_authorized_request(request: Request) -> bool:
    """Validate bearer auth using constant-time comparisons."""
    auth = request.headers.get("authorization", "")
    for token in _accepted_bearer_tokens():
        expected = f"Bearer {token}"
        if hmac.compare_digest(auth.encode(), expected.encode()):
            return True
    return False


def _require_token(request: Request) -> None:
    """Validate dashboard bearer auth. Raises 401 on mismatch."""
    if not _is_authorized_request(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
'''

if "def _is_authorized_request(request: Request) -> bool:" not in text:
    if old_require not in text:
        raise SystemExit("Could not find legacy _require_token block to patch")
    text = text.replace(old_require, new_auth)

old_middleware = '''        auth = request.headers.get("authorization", "")
        expected = f"Bearer {_SESSION_TOKEN}"
        if not hmac.compare_digest(auth.encode(), expected.encode()):
            return JSONResponse(
'''
new_middleware = '''        if not _is_authorized_request(request):
            return JSONResponse(
'''
if old_middleware in text:
    text = text.replace(old_middleware, new_middleware)

if "from typing import Any, Dict, List, Optional" not in text and "List" not in text.split("\n", 80)[0:80]:
    raise SystemExit("web_server.py typing imports do not expose List; inspect manually")

old_allowed_roots = '''    allowed_roots = [
        (Path.home() / "Documents" / "Hermes").resolve(),
        (Path.home() / ".hermes" / "memories").resolve(),
        (Path.home() / ".hermes" / "hermes-agent").resolve(),
    ]
'''
new_allowed_roots = '''    allowed_roots = [
        (Path.home() / "Documents" / "Hermes").resolve(),
        (Path.home() / ".hermes").resolve(),
    ]
'''
if old_allowed_roots in text:
    text = text.replace(old_allowed_roots, new_allowed_roots)

old_core_docs = '''    memories = home / ".hermes" / "memories"
    agent_root = PROJECT_ROOT

    core_candidates = [
        memories / "MEMORY.md",
        memories / "USER.md",
        agent_root / "AGENTS.md",
    ]
'''
new_core_docs = '''    hermes_home = home / ".hermes"

    core_candidates = [
        hermes_home / "SOUL.md",
        hermes_home / "USER.md",
        hermes_home / "AGENTS.md",
        hermes_home / "memories" / "MEMORY.md",
    ]
'''
if old_core_docs in text:
    text = text.replace(old_core_docs, new_core_docs)

if 'hermes_home / "SOUL.md"' not in text or 'hermes_home / "USER.md"' not in text or 'hermes_home / "AGENTS.md"' not in text:
    raise SystemExit("web_server.py Knowledge core docs are not aligned to ~/.hermes/SOUL.md, USER.md, AGENTS.md")
if '(Path.home() / ".hermes").resolve()' not in text:
    raise SystemExit("web_server.py Knowledge file reader does not allow ~/.hermes root")

if text != original:
    path.write_text(text, encoding="utf-8")
    print("PATCHED web_server.py")
else:
    print("web_server.py already aligned")
PY

if ! grep -q "127.0.0.1:9119" "$VITE_CONFIG"; then
  fail "Mission Control Vite proxy is not pointed at dashboard backend 9119"
fi
if ! grep -q "'/api/local'" "$VITE_CONFIG"; then
  fail "Mission Control Vite proxy is missing /api/local before /api"
fi

log "Running syntax checks"
python3 -m py_compile "$WEB_SERVER"
bash -n "$SMOKE_SCRIPT" "$SCRIPT_DIR/run-dashboard-api.sh" "$SCRIPT_DIR/run-local-telemetry.sh"

restart_job() {
  local label="$1"
  mc_restart_service "$label"
}

restart_job ai.hermes.dashboard-api
restart_job ai.hermes.mission-control-telemetry
restart_job ai.hermes.mission-control

log "Smoke checking current Mission Control stack"
"$SMOKE_SCRIPT" http://127.0.0.1:9119/api
"$SMOKE_SCRIPT" http://127.0.0.1:5174/api

log "Done. Mission Control is aligned to dashboard backend 9119 and verified through Vite 5174."
