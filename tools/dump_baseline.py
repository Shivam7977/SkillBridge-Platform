import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from app import app, scheduler


def registration_counts():
    before_request = list(app.before_request_funcs.get(None, ()))
    errorhandlers = {
        str(code): sorted(getattr(func, '__name__', repr(func)) for func in funcs.values())
        for code, funcs in sorted(app.error_handler_spec.get(None, {}).items(), key=lambda item: str(item[0]))
    }
    globals_map = app.jinja_env.globals
    filters_map = app.jinja_env.filters
    context_processors = list(app.template_context_processors.get(None, ()))
    return {
        'before_request': {'count': len(before_request), 'names': sorted(getattr(func, '__name__', repr(func)) for func in before_request)},
        'errorhandlers': {'count': sum(len(funcs) for funcs in app.error_handler_spec.get(None, {}).values()), 'names': errorhandlers},
        'jinja_globals': {'count': len(globals_map), 'names': sorted(globals_map)},
        'jinja_filters': {'count': len(filters_map), 'names': sorted(filters_map)},
        'context_processors': {'count': len(context_processors), 'names': sorted(getattr(func, '__name__', repr(func)) for func in context_processors)},
    }


rules = sorted(
    (rule.rule, rule.endpoint, sorted(rule.methods or ()))
    for rule in app.url_map.iter_rules()
)
baseline = {
    'url_map': rules,
    'url_map_count': len(rules),
    'registrations': registration_counts(),
    'root_path': app.root_path,
    'template_folder': app.template_folder,
    'static_folder': app.static_folder,
    'scheduler_job_ids': sorted(job.id for job in scheduler.get_jobs()),
}
Path('baseline.json').write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print(json.dumps({
    'url_map_count': baseline['url_map_count'],
    'registrations': baseline['registrations'],
    'root_path': baseline['root_path'],
    'template_folder': baseline['template_folder'],
    'static_folder': baseline['static_folder'],
    'scheduler_job_ids': baseline['scheduler_job_ids'],
}, indent=2, ensure_ascii=False))
