"""Compare Flask route and registration source to the checked-in runtime baseline without importing the app."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "baseline.json").read_text(encoding="utf-8"))


def source_files():
    files = [ROOT / "app.py", ROOT / "extensions.py", ROOT / "models.py"]
    for package in (ROOT / "routes", ROOT / "services"):
        if package.exists():
            files.extend(sorted(package.rglob("*.py")))
    return [path for path in files if path.is_file()]


def call_name(node):
    if isinstance(node, ast.Attribute):
        return f"{call_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def literal(node, default=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default


def collect_source():
    routes = []
    before_request = []
    errorhandlers = []
    job_ids = []
    jinja_utc_iso = False
    login_manager_setup = False
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Attribute) and call_name(decorator) == "app.before_request":
                        before_request.append(node.name)
                        continue
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = call_name(decorator.func)
                    if name == "app.route" and decorator.args:
                        rule = literal(decorator.args[0])
                        methods = ["GET"]
                        for keyword in decorator.keywords:
                            if keyword.arg == "methods":
                                methods = literal(keyword.value, methods)
                        methods = {str(method).upper() for method in methods}
                        if "GET" in methods:
                            methods.add("HEAD")
                        methods.add("OPTIONS")
                        routes.append((rule, node.name, sorted(methods)))
                    elif name == "app.before_request":
                        before_request.append(node.name)
                    elif name == "app.errorhandler":
                        code_node = decorator.args[0] if decorator.args else None
                        code = literal(code_node)
                        if isinstance(code_node, ast.Name) and code_node.id == "RateLimitExceeded":
                            code = 429
                        errorhandlers.append((str(code), node.name))
            elif isinstance(node, ast.Call):
                name = call_name(node.func)
                if name == "scheduler.add_job":
                    for keyword in node.keywords:
                        if keyword.arg == "id":
                            job_ids.append(literal(keyword.value))
                elif name == "app.jinja_env.globals.update":
                    jinja_utc_iso = jinja_utc_iso or any(key.arg == "utc_iso" for key in node.keywords)
                elif name == "login_manager.init_app":
                    login_manager_setup = True

    # Flask registers this route automatically for its static folder.
    routes.append(("/static/<path:filename>", "static", ["GET", "HEAD", "OPTIONS"]))
    return {
        "url_map": sorted((rule, endpoint, methods) for rule, endpoint, methods in routes),
        "before_request": sorted(before_request),
        "errorhandlers": sorted(errorhandlers),
        "job_ids": sorted(job_ids),
        "jinja_utc_iso": jinja_utc_iso,
        "login_manager_setup": login_manager_setup,
    }


actual = collect_source()
expected_routes = [tuple(item[:2]) + (item[2],) for item in BASELINE["url_map"]]
expected_before = sorted(name for name in BASELINE["registrations"]["before_request"]["names"] if name != "_check_request_limit")
expected_errors = sorted(
    (code, name)
    for code, names in BASELINE["registrations"]["errorhandlers"]["names"].items()
    for name in names
)
expected_jobs = sorted(BASELINE["scheduler_job_ids"])
checks = {
    "url_map": (actual["url_map"], expected_routes),
    "before_request_functions": (actual["before_request"], expected_before),
    "errorhandlers": (actual["errorhandlers"], expected_errors),
    "scheduler_job_ids": (actual["job_ids"], expected_jobs),
    "utc_iso_jinja_global": (actual["jinja_utc_iso"], "utc_iso" in BASELINE["registrations"]["jinja_globals"]["names"]),
    "Flask-Login_context_processor_setup": (actual["login_manager_setup"], "_user_context_processor" in BASELINE["registrations"]["context_processors"]["names"]),
}
failed = False
for name, (found, expected) in checks.items():
    if found == expected:
        print(f"PASS {name}")
    else:
        failed = True
        print(f"FAIL {name}\n  expected: {expected}\n  found:    {found}")
if failed:
    raise SystemExit(1)
