"""Extract Batch C route functions from app.py without changing their bodies.

Run from any directory with the project's virtual environment Python. This is
an editing utility; it does not import the application or connect to MongoDB.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
ROUTES_DIR = ROOT / "routes"
INIT_PATH = ROUTES_DIR / "__init__.py"

ASSIGNMENTS = {
    "messaging.py": {
        "view_user_profile",
        "messages_list",
        "chat",
        "api_dm_messages",
    },
    "communities.py": {
        "view_community",
        "react_to_message",
        "request_to_join_community",
        "leave_community",
        "owner_delete_community",
        "approve_member",
        "make_admin",
        "decline_member",
        "remove_member",
        "remove_admin",
        "find_communities",
        "api_communities",
        "get_community_messages",
        "send_community_message_api",
        "api_community_members",
    },
    "github_sync.py": {"github_fetch", "github_sync", "github_disconnect"},
    "account.py": {"settings", "change_password", "delete_account"},
    "gamification.py": {
        "leaderboard",
        "api_recalculate_xp",
        "xp_history",
        "check_levelup",
        "api_ai_usage",
        "xp_status",
    },
    "misc.py": {
        "onboarding_status",
        "onboarding_dismiss",
        "update_user_timezone",
        "update_email_preferences",
        "unsubscribe_email_reminders",
        "get_notifications",
        "mark_notification_read",
        "mark_all_read",
        "search",
        "api_search_users",
        "health",
    },
    "admin.py": {
        "admin_required",
        "admin_panel",
        "admin_approve_commit",
        "admin_reject_commit",
        "admin_adjust_xp",
        "admin_ban_user",
        "admin_unban_user",
        "admin_toggle_email_reminders",
        "admin_delete_user",
        "admin_restore_account",
        "admin_delete_project",
        "admin_delete_roadmap",
        "admin_delete_community",
        "admin_project_comments",
        "admin_user_detail",
        "admin_project_detail",
        "admin_roadmap_detail",
        "admin_community_detail",
    },
}

ADMIN_USER_FUNCTIONS = {
    "admin_required",
    "admin_panel",
    "admin_approve_commit",
    "admin_reject_commit",
    "admin_adjust_xp",
    "admin_ban_user",
    "admin_unban_user",
    "admin_toggle_email_reminders",
    "admin_delete_user",
    "admin_restore_account",
    "admin_user_detail",
}


def block_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", ())
    return min([node.lineno, *(decorator.lineno for decorator in decorators)]) - 1


def exact_block(lines: list[str], start: int, end: int) -> str:
    return "".join(lines[start:end]).rstrip() + "\n\n"


def import_block(tree: ast.Module, lines: list[str]) -> str:
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (
            isinstance(node, ast.Import)
            and any(alias.name == "routes" for alias in node.names)
        )
    ]
    nodes.sort(key=lambda node: node.lineno)
    chunks = [exact_block(lines, node.lineno - 1, node.end_lineno) for node in nodes]
    if not chunks:
        raise RuntimeError("No top-level imports found in app.py")
    return "".join(chunks).rstrip() + "\n\n\n"


def is_allowed_app_statement(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.If)):
        if isinstance(node, ast.If):
            return isinstance(node.test, ast.Compare) and "__name__" in ast.unparse(node.test)
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "update"
            and "jinja_env.globals" in ast.unparse(call.func.value)
            and "utc_iso" in ast.unparse(call)
        )
    return False


def newline_style(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def main() -> None:
    raw = APP_PATH.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    source = raw.decode("utf-8-sig")
    newline = newline_style(source)
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source, filename=str(APP_PATH))

    # Refuse to overwrite unrelated files or to run twice against an already
    # extracted app.py.
    destinations = [ROUTES_DIR / name for name in ASSIGNMENTS]
    existing = [str(path.relative_to(ROOT)) for path in destinations if path.exists()]
    if existing:
        raise RuntimeError(
            "Refusing to overwrite existing Batch C modules: " + ", ".join(existing)
        )

    import_text = import_block(tree, lines)
    found: dict[str, str] = {}
    blocks: dict[str, list[tuple[int, str]]] = {name: [] for name in ASSIGNMENTS}
    remove_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for filename, names in ASSIGNMENTS.items():
            if node.name not in names:
                continue
            if node.name in found:
                raise RuntimeError(f"Duplicate top-level function: {node.name}")
            start = block_start(node)
            end = node.end_lineno
            blocks[filename].append((start, exact_block(lines, start, end)))
            remove_ranges.append((start, end))
            found[node.name] = filename

    expected = set().union(*ASSIGNMENTS.values())
    missing = expected - found.keys()
    if missing:
        raise RuntimeError("Functions not found in app.py: " + ", ".join(sorted(missing)))

    # Any top-level executable/helper definition outside the moved functions
    # would otherwise be silently lost from the requested minimal app.py.
    moved_lines = {
        line_index
        for start, end in remove_ranges
        for line_index in range(start, end)
    }
    unsupported = []
    main_node: ast.If | None = None
    for node in tree.body:
        if all(line_index in moved_lines for line_index in range(node.lineno - 1, node.end_lineno)):
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and "__name__" in ast.unparse(node.test):
            if main_node is not None:
                unsupported.append(f"second __main__ block at line {node.lineno}")
            main_node = node
            continue
        if not is_allowed_app_statement(node):
            unsupported.append(f"{type(node).__name__} at line {node.lineno}: {ast.unparse(node)[:100]}")
    if unsupported:
        raise RuntimeError(
            "Unplaced top-level app.py statements (no files written): "
            + "; ".join(unsupported)
        )
    if main_node is None:
        raise RuntimeError("Could not find the app.py __main__ block")

    # Keep all original app imports in the new route modules. This preserves
    # the exact imported names/aliases used by the original route bodies,
    # including re_ext and third-party helpers.
    modules: dict[str, str] = {}
    for filename, entries in blocks.items():
        content = import_text + "".join(block for _, block in sorted(entries))
        modules[filename] = content

    admin_lines = modules["admin.py"].splitlines()
    if len(admin_lines) > 700:
        # Keep the same exact function blocks, split by ownership. A shared
        # admin_required helper remains alongside the admin user routes.
        user_blocks = [entry for entry in blocks["admin.py"] if any(
            line.startswith(("def " + name + "(", "def " + name + ":"))
            for name in ADMIN_USER_FUNCTIONS
            for line in entry[1].splitlines()
        )]
        content_blocks = [entry for entry in blocks["admin.py"] if entry not in user_blocks]
        user_text = import_text + "".join(block for _, block in sorted(user_blocks))
        content_text = import_text + "from .admin_users import admin_required\n\n" + "".join(
            block for _, block in sorted(content_blocks)
        )
        if len(user_text.splitlines()) > 700 or len(content_text.splitlines()) > 700:
            raise RuntimeError("Admin split still exceeds ~700 lines; no files written")
        modules.pop("admin.py")
        modules["admin_users.py"] = user_text
        modules["admin_content.py"] = content_text

    # Build the small entry point while retaining the original import block,
    # the utc_iso registration, routes import, and exact __main__ body.
    registration_nodes = [
        node for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "update"
        and "jinja_env.globals" in ast.unparse(node.value.func.value)
        and "utc_iso" in ast.unparse(node.value)
    ]
    if len(registration_nodes) != 1:
        raise RuntimeError("Expected exactly one utc_iso jinja global registration")
    registration = exact_block(lines, registration_nodes[0].lineno - 1, registration_nodes[0].end_lineno)
    main_block = exact_block(lines, main_node.lineno - 1, main_node.end_lineno)
    new_app = import_text + registration + "\nimport routes\n\n\n" + main_block

    # Preserve the existing routes imports and add only the new Batch C files.
    init_source = INIT_PATH.read_text(encoding="utf-8-sig")
    module_names = [Path(name).stem for name in modules]
    additions = "".join(f"from . import {name}{newline}" for name in module_names)
    if any(f"{name}" in init_source for name in module_names):
        raise RuntimeError("A Batch C module is already listed in routes/__init__.py")
    new_init = init_source.rstrip("\r\n") + newline + additions

    # All validation is complete before the first write.
    for filename, content in modules.items():
        path = ROUTES_DIR / filename
        normalized = content.replace("\r\n", "\n").replace("\n", newline)
        path.write_bytes(normalized.encode("utf-8"))
    APP_PATH.write_bytes((b"\xef\xbb\xbf" if had_bom else b"") + new_app.replace("\r\n", "\n").replace("\n", newline).encode("utf-8"))
    INIT_PATH.write_text(new_init, encoding="utf-8", newline="")

    print("Extracted Batch C route modules (bodies and decorators copied verbatim).")
    for filename, entries in blocks.items():
        dest = "admin_users.py/admin_content.py" if filename == "admin.py" and "admin.py" not in modules else filename
        print(f"routes/{dest}: {len(entries)} functions")
    print("app.py retained its imports, utc_iso jinja registration, import routes, and __main__ block.")
    print("No Python application imports or database connections were performed by this utility.")


if __name__ == "__main__":
    main()
