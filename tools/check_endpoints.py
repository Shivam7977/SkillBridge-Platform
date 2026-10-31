"""Check literal url_for endpoints in templates and project Python files."""

import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as app_entry


registered = {rule.endpoint for rule in app_entry.app.url_map.iter_rules()}
pattern = re.compile(r"\burl_for\s*\(\s*(['\"])([^'\"]+)\1")

files = list((ROOT / "templates").glob("*.html"))
for path in ROOT.rglob("*.py"):
    relative = path.relative_to(ROOT)
    if any(part in {".git", "venv", ".venv", "__pycache__"} for part in relative.parts):
        continue
    files.append(path)

missing = {}
for path in sorted(set(files)):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    for match in pattern.finditer(text):
        endpoint = match.group(2)
        if endpoint != "static" and endpoint not in registered:
            missing.setdefault(endpoint, set()).add(str(path.relative_to(ROOT)))

if missing:
    print("Missing url_for endpoints:")
    for endpoint, paths in sorted(missing.items()):
        print(f"  {endpoint}: {', '.join(sorted(paths))}")
    raise SystemExit(1)

print(f"PASS: every literal url_for endpoint exists ({len(registered)} registered endpoints)")
