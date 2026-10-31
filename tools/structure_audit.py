import hashlib, os, re, subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path.cwd()
SKIP = {"venv", ".venv", ".git", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}
LOG = []

def P(s=""):
    print(s)
    LOG.append(str(s))

def head(t):
    P()
    P("=== " + t + " ===")

def walk():
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP]
        for f in fn:
            yield Path(dp) / f

FILES = sorted(walk())

def rel(p):
    return p.relative_to(ROOT).as_posix()

def kb(p):
    return p.stat().st_size / 1024

def git(*a):
    r = subprocess.run(["git", "-c", "core.quotepath=off", *a], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return [l for l in r.stdout.splitlines() if l.strip()]

def read(p):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def grp(path):
    parts = path.split("/")
    if len(parts) == 1:
        return "(root files)"
    if parts[0] in ("static", "templates") and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]

tracked = set(git("ls-files"))
P(f"Files on disk (excl. venv/.git/caches): {len(FILES)} | tracked by git: {len(tracked)}")
P("Legend: T = tracked by git, - = not tracked")

head("1. FOLDERS: files / size / tracked count")
g = defaultdict(lambda: [0, 0.0, 0])
for p in FILES:
    r = rel(p)
    k = grp(r)
    g[k][0] += 1
    g[k][1] += kb(p)
    g[k][2] += (r in tracked)
for k, (n, s, t) in sorted(g.items()):
    P(f"{k:38} {n:5} files {s:10,.0f} KB  tracked {t}")

head("2. ROOT FILES")
for p in FILES:
    r = rel(p)
    if "/" not in r:
        P(f"{'T' if r in tracked else '-'} {kb(p):9,.1f} KB  {r}")

head("3. UNTRACKED (not ignored): these show up in git status")
for l in git("ls-files", "--others", "--exclude-standard"):
    P(l)

head("4. TRACKED BUT MATCHES .gitignore (needs git rm --cached)")
for l in git("ls-files", "-ci", "--exclude-standard"):
    P(l)

head("5. IGNORED FILES ON DISK (grouped)")
ig = defaultdict(int)
for l in git("ls-files", "--others", "--ignored", "--exclude-standard"):
    if l.split("/")[0] in SKIP or "__pycache__" in l:
        continue
    ig[grp(l)] += 1
for k, v in sorted(ig.items()):
    P(f"{k:38} {v} files")

head("6. SUSPICIOUS NAMES (test / tmp / backup / copy / scratch / check)")
pat = re.compile(r"(^|[_\-. (])(test|tests|tmp|temp|old|backup|bak|copy|scratch|check|debug|untitled|draft)([_\-. )]|$)", re.I)
for p in FILES:
    n = p.name.lower()
    if pat.search(p.stem) or n in ("desktop.ini", "thumbs.db", ".ds_store") or p.suffix.lower() in (".log", ".orig", ".bak", ".tmp", ".pyc"):
        P(f"{'T' if rel(p) in tracked else '-'} {kb(p):9,.1f} KB  {rel(p)}")

head("7. BIGGEST 15 FILES")
for p in sorted(FILES, key=lambda x: -x.stat().st_size)[:15]:
    P(f"{'T' if rel(p) in tracked else '-'} {kb(p):10,.0f} KB  {rel(p)}")

head("8. DUPLICATE CONTENT (identical files)")
h = defaultdict(list)
for p in FILES:
    if p.stat().st_size > 0 and p.name != "__init__.py":
        h[hashlib.md5(p.read_bytes()).hexdigest()].append(rel(p))
dup = [v for v in h.values() if len(v) > 1]
for v in dup:
    P(" == ".join(v))
if not dup:
    P("none")

head("9. PYTHON MODULES NOT REFERENCED BY ANY OTHER FILE (tools/ and tests/ are expected here)")
pys = [p for p in FILES if p.suffix == ".py"]
texts = {p: read(p) for p in pys}
extra = "".join(read(p) for p in FILES if p.name in ("Procfile", "render.yaml") or p.suffix in (".yml", ".yaml", ".toml", ".ini", ".cfg"))
for p in pys:
    m = p.stem
    if m in ("__init__", "app", "wsgi", "conftest") or m.startswith("test_"):
        continue
    rx = re.compile(r"\b" + re.escape(m) + r"\b")
    if not any(rx.search(t) for q, t in texts.items() if q != p) and not rx.search(extra):
        P(f"{'T' if rel(p) in tracked else '-'} {rel(p)}")

head("10. TEMPLATES NOT REFERENCED ANYWHERE (verify manually; portfolio_templates are loaded dynamically)")
corpus = "".join(texts.values()) + "".join(read(p) for p in FILES if p.suffix in (".html", ".js"))
for p in FILES:
    r = rel(p)
    if r.startswith("templates/") and p.suffix == ".html" and "portfolio_templates" not in r:
        if r[len("templates/"):] not in corpus and p.name not in corpus:
            P(f"{'T' if r in tracked else '-'} {r}")

Path("structure_files.txt").write_text("\n".join(f"{'T' if rel(p) in tracked else '-'} {kb(p):10,.1f} KB  {rel(p)}" for p in FILES), encoding="utf-8")
Path("structure_report.txt").write_text("\n".join(LOG), encoding="utf-8")
print("\nWrote structure_report.txt (summary) and structure_files.txt (every file)")
