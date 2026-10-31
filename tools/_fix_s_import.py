from pathlib import Path
for f in ["routes/admin_users.py", "routes/admin_content.py"]:
    p = Path(f)
    b = p.read_bytes()
    assert b.count(b"db, s, limiter") == 1, f
    p.write_bytes(b.replace(b"db, s, limiter", b"db, limiter"))
print("done")
