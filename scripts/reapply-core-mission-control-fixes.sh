#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
HERMES_ROOT="${1:-${HERMES_ROOT:-$DEFAULT_ROOT}}"

API_SERVER="$HERMES_ROOT/gateway/platforms/api_server.py"
MODEL_TOOLS="$HERMES_ROOT/model_tools.py"
SKILLS_TOOL="$HERMES_ROOT/tools/skills_tool.py"

log() { echo "[mission-control-fix] $*"; }
fail() { echo "[mission-control-fix][FAIL] $*" >&2; exit 1; }

[[ -f "$API_SERVER" ]] || fail "Missing api_server.py at $API_SERVER"
[[ -f "$MODEL_TOOLS" ]] || fail "Missing model_tools.py at $MODEL_TOOLS"
[[ -f "$SKILLS_TOOL" ]] || fail "Missing skills_tool.py at $SKILLS_TOOL"

log "Applying idempotent core patches in: $HERMES_ROOT"

python3 - "$HERMES_ROOT" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
api = root / "gateway/platforms/api_server.py"
model = root / "model_tools.py"
skills = root / "tools/skills_tool.py"

modified = []


def write_if_changed(path: Path, content: str) -> None:
    old = path.read_text(encoding="utf-8")
    if old != content:
        path.write_text(content, encoding="utf-8")
        modified.append(str(path))


def ensure_replace_all(text: str, old: str, new: str) -> str:
    if old in text:
        return text.replace(old, new)
    return text


# 1) api_server.py patches
api_text = api.read_text(encoding="utf-8")

api_text = ensure_replace_all(
    api_text,
    'return {"success": True, "toolset": match, "toolCatalog": tool_catalog, "availableToolsets": items}',
    'return {"success": True, "available": True, "toolset": match, "toolCatalog": tool_catalog, "availableToolsets": items}',
)

api_text = ensure_replace_all(
    api_text,
    'return {\n            "success": True,\n            "toolsets": items,',
    'return {\n            "success": True,\n            "available": True,\n            "toolsets": items,',
)

api_text = ensure_replace_all(
    api_text,
    'return {\n            "success": True,\n            "skills": _sanitize_path_payload(normalized_skills),',
    'return {\n            "success": True,\n            "available": True,\n            "skills": _sanitize_path_payload(normalized_skills),',
)

api_text = ensure_replace_all(
    api_text,
    'return {\n            "success": True,\n            "skills": _sanitize_path_payload(skills_payload.get("skills", [])),',
    'return {\n            "success": True,\n            "available": True,\n            "skills": _sanitize_path_payload(skills_payload.get("skills", [])),',
)

api_text = ensure_replace_all(
    api_text,
    '    return {\n        "path": _redact_home_path(str(path)),',
    '    return {\n        "available": True,\n        "path": _redact_home_path(str(path)),',
)

write_if_changed(api, api_text)


# 2) model_tools.py compatibility shim
model_text = model.read_text(encoding="utf-8")
if "import inspect" not in model_text:
    model_text = model_text.replace("import threading\n", "import threading\nimport inspect\n")

shim = '''\n\ndef get_tool_source_path(tool_name: str) -> Optional[str]:\n    """Best-effort source file path for a registered tool handler."""\n    entry = registry._tools.get(tool_name)  # Backward-compat shim for Mission Control\n    if not entry:\n        return None\n    try:\n        source_path = inspect.getsourcefile(entry.handler)\n        return source_path\n    except Exception:\n        return None\n'''

if "def get_tool_source_path(tool_name: str) -> Optional[str]:" not in model_text:
    marker = "\ndef check_toolset_requirements() -> Dict[str, bool]:\n"
    if marker not in model_text:
        raise RuntimeError("Could not find insertion point in model_tools.py")
    model_text = model_text.replace(marker, shim + marker)

write_if_changed(model, model_text)


# 3) skills_tool.py compatibility shim
skills_text = skills.read_text(encoding="utf-8")

skills_shim = '''\n\ndef skills_categories(task_id: str = None) -> str:\n    """List available skill categories with counts and optional descriptions."""\n    try:\n        if not SKILLS_DIR.exists():\n            return json.dumps(\n                {\n                    "success": True,\n                    "categories": [],\n                    "count": 0,\n                    "hint": "No skills directory found yet.",\n                },\n                ensure_ascii=False,\n            )\n\n        all_skills = _find_all_skills()\n        buckets: Dict[str, Dict[str, Any]] = {}\n        for skill in all_skills:\n            category_name = skill.get("category") or "uncategorized"\n            bucket = buckets.setdefault(\n                category_name,\n                {\n                    "name": category_name,\n                    "count": 0,\n                    "description": None,\n                },\n            )\n            bucket["count"] += 1\n\n        for category_name, bucket in buckets.items():\n            if category_name == "uncategorized":\n                continue\n            category_dir = SKILLS_DIR / category_name\n            if category_dir.exists() and category_dir.is_dir():\n                bucket["description"] = _load_category_description(category_dir)\n\n        categories = sorted(buckets.values(), key=lambda c: c["name"])\n        return json.dumps(\n            {\n                "success": True,\n                "categories": categories,\n                "count": len(categories),\n                "hint": "Use skills_list(category=...) for skills in a category",\n            },\n            ensure_ascii=False,\n        )\n\n    except Exception as e:\n        return tool_error(str(e), success=False)\n'''

if "def skills_categories(task_id: str = None) -> str:" not in skills_text:
    marker = "\ndef skills_list(category: str = None, task_id: str = None) -> str:\n"
    if marker not in skills_text:
        raise RuntimeError("Could not find insertion point in skills_tool.py")
    skills_text = skills_text.replace(marker, skills_shim + marker)

write_if_changed(skills, skills_text)

print("PATCHED_FILES")
if modified:
    for item in modified:
        print(item)
else:
    print("none")
PY

log "Running syntax checks"
python3 -m py_compile "$API_SERVER" "$MODEL_TOOLS" "$SKILLS_TOOL"

log "Restarting Hermes gateway"
launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway"

log "Smoke checking Mission Control endpoints"
python3 - <<'PY'
import json
import time
import urllib.request
import urllib.error

bases = ["http://127.0.0.1:8642", "http://127.0.0.1:5174"]
paths = ["tools", "skills", "config"]

def fetch_with_retry(url: str, attempts: int = 12, delay: float = 1.0):
    last_err = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError) as e:
            last_err = e
            time.sleep(delay)
    raise SystemExit(f"{url} unreachable after retries: {last_err}")

for base in bases:
    for p in paths:
        url = f"{base}/api/mission-control/{p}"
        data = fetch_with_retry(url)
        if not data.get("success"):
            raise SystemExit(f"{url} -> success=false")
        if data.get("available") is not True:
            raise SystemExit(f"{url} -> available is not True (got {data.get('available')!r})")
        print(f"OK {url} available={data.get('available')}")
PY

log "Done. Core Mission Control compatibility fixes are applied and verified."
