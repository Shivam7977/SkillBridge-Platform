$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "."

& .\venv\Scripts\ruff.exe check --select F821,F811 .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& .\venv\Scripts\python.exe -c 'import app; from extensions import scheduler; print(dict(rules=len(list(app.app.url_map.iter_rules())), before_request=sorted(f.__name__ for f in app.app.before_request_funcs.get(None, [])), jobs=sorted(job.id for job in scheduler.get_jobs())))'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& .\venv\Scripts\python.exe .\tools\compare_baseline.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& .\venv\Scripts\python.exe .\tools\check_endpoints.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
