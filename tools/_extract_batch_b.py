from pathlib import Path
import ast

root = Path(__file__).resolve().parents[1]
app_path = root / "app.py"
data = app_path.read_bytes()
had_bom = data.startswith(b"\xef\xbb\xbf")
source = data.decode("utf-8-sig")
lines = source.splitlines(keepends=True)
tree = ast.parse(source, filename=str(app_path))

assignments = {
    "routes/projects.py": {"projects", "api_projects", "my_projects", "create_project", "toggle_complete", "edit_project", "delete_project", "view_project", "upload_version", "download_project"},
    "routes/project_social.py": {"toggle_like", "toggle_bookmark", "post_comment", "post_reply", "toggle_comment_like", "delete_comment", "get_comments_sorted"},
    "routes/resume_portfolio.py": {"resume_builder", "resume_pdf", "portfolio_builder", "serve_portfolio_assets", "serve_portfolio_images", "list_templates", "get_template_details"},
    "routes/chatbot.py": {"chatbot", "chatbot_history", "chatbot_clear"},
    "routes/interview.py": {"compute_adaptive_difficulty", "call_interview_ai", "get_interview_profile_context", "build_interview_question_prompt", "build_interview_eval_prompt", "interview_room", "interview_history", "interview_ask", "interview_save", "interview_complete"},
}

common_header = (root / "routes" / "auth.py").read_text(encoding="utf-8-sig").split("@app.route('/')", 1)[0]
found = {}
blocks = {relative: [] for relative in assignments}
remove_ranges = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for relative, names in assignments.items():
            if node.name in names:
                if node.name in found:
                    raise RuntimeError(f"duplicate function definition: {node.name}")
                start_line = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
                start = start_line - 1
                end = node.end_lineno
                block = "".join(lines[start:end]).rstrip() + "\n\n"
                found[node.name] = relative
                blocks[relative].append((start, block))
                remove_ranges.append((start, end))
    elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "INTERVIEW_DIFF_LADDER" for target in node.targets):
        start = node.lineno - 1
        end = node.end_lineno
        block = "".join(lines[start:end]).rstrip() + "\n\n"
        blocks["routes/interview.py"].append((start, block))
        remove_ranges.append((start, end))

expected = set().union(*assignments.values())
missing = expected - found.keys()
if missing:
    raise RuntimeError(f"functions not found in app.py: {sorted(missing)}")
if not any("INTERVIEW_DIFF_LADDER" in block for _, block in blocks["routes/interview.py"]):
    raise RuntimeError("INTERVIEW_DIFF_LADDER constant not found")

for relative, entries in blocks.items():
    module_header = common_header
    if relative == "routes/projects.py":
        module_header += "from recommendation_engine import get_recommended_projects\n"
    module_text = module_header + "\n".join(block for _, block in sorted(entries, key=lambda item: item[0]))
    module_text = module_text.replace("\r\n", "\n").replace("\n", "\r\n")
    (root / relative).write_bytes(module_text.encode("utf-8"))

remove_lines = set()
for start, end in remove_ranges:
    remove_lines.update(range(start, end))
remaining = "".join(line for index, line in enumerate(lines) if index not in remove_lines)
remaining = remaining.replace("\r\n", "\n").replace("\n", "\r\n")
app_path.write_bytes((b"\xef\xbb\xbf" if had_bom else b"") + remaining.encode("utf-8"))

init_path = root / "routes" / "__init__.py"
init_text = init_path.read_text(encoding="utf-8-sig")
init_text += "from . import projects, project_social, resume_portfolio, chatbot, interview\r\n"
init_path.write_bytes(init_text.replace("\n", "\r\n").encode("utf-8"))

print("Moved Batch B route groups and interview helpers.")
for relative, names in assignments.items():
    print(f"{relative}: {', '.join(sorted(names))}")
print("routes/interview.py: INTERVIEW_DIFF_LADDER")
