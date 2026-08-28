#!/usr/bin/env python3
"""Cross-check env vars: template vs code references."""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[2]

tmpl = (root / "deploy/systemd/env.template").read_text()
template_keys = set(re.findall(r"^([A-Z_]+)=", tmpl, re.M))

code_keys = set()
patterns = [
    r'os\.environ\.get\("([A-Z_]+)"',
    r"os\.environ\.get\('([A-Z_]+)'",
    r'os\.getenv\("([A-Z_]+)"',
    r"os\.getenv\('([A-Z_]+)'",
    r'import\.meta\.env\.([A-Z_]+)',
    r'process\.env\.([A-Z_]+)',
]
files = list((root / "server").rglob("*.py"))
files += list((root / "src").rglob("*.ts"))
files += list((root / "src").rglob("*.tsx"))
files += list((root / "scripts").rglob("*.sh"))
files.append(root / "vite.config.ts")
for f in files:
    try:
        txt = f.read_text(errors="ignore")
    except Exception:
        continue
    for p in patterns:
        code_keys.update(re.findall(p, txt))
# launchctl getenv names in scripts
for f in (root / "scripts").rglob("*.sh"):
    txt = f.read_text(errors="ignore")
    code_keys.update(re.findall(r"getenv ([A-Z_]+)", txt))

print("TEMPLATE KEYS (%d):" % len(template_keys))
for k in sorted(template_keys):
    print("  ", k)
print("\nCODE KEYS (%d):" % len(code_keys))
for k in sorted(code_keys):
    print("  ", k)
print("\n=== MISSING from template (MUST FIX) ===")
for k in sorted(code_keys - template_keys):
    print("  ", k)
print("\n=== In template but unused in code (informational) ===")
for k in sorted(template_keys - code_keys):
    print("  ", k)
