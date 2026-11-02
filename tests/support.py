"""Shared test helpers.

* Puts the project root on sys.path.
* Replaces `extensions` (which connects to MongoDB the moment it is imported) with a stub, and replaces the
  time and notification services with controllable stand-ins. No database is used anywhere.
* Provides stand-ins ONLY for third-party packages that are not installed, so the same tests run on a bare
  Python and on your full development environment.
* `load_functions` pulls individual route functions out of a file by name, so route code can be tested
  without starting the whole Flask app.
"""
import ast
import datetime as dt
import importlib
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def _missing(name):
    try:
        importlib.import_module(name)
        return False
    except ImportError:
        return True


# ---------------------------------------------------------------- third-party stand-ins (only if missing)
if _missing('bson'):
    import itertools as _it
    _counter = _it.count(1)

    class ObjectId:                                              # same validation rule as the real one
        def __init__(self, value=None):
            if value is None:
                value = '%024x' % next(_counter)             # unique, like the real generator
            if isinstance(value, ObjectId):
                value = value.value
            if not re.fullmatch(r'[0-9a-fA-F]{24}', str(value)):
                raise ValueError(f'{value!r} is not a valid ObjectId')
            self.value = str(value).lower()

        def __eq__(self, other):
            return isinstance(other, ObjectId) and other.value == self.value

        def __hash__(self):
            return hash(self.value)

        def __str__(self):
            return self.value

        __repr__ = __str__

    _module('bson', objectid=_module('bson.objectid', ObjectId=ObjectId))
if _missing('pymongo'):
    class _RD:
        BEFORE, AFTER = False, True
    _module('pymongo', ReturnDocument=_RD)
if _missing('flask_login'):
    _module('flask_login', current_user=None, login_required=lambda f: f, login_user=None, logout_user=None)
if _missing('flask_limiter'):
    _module('flask_limiter')
    _module('flask_limiter.util', get_remote_address=lambda: '127.0.0.1')
if _missing('bleach'):
    _module('bleach', clean=lambda text, **kw: text)
for _name in ('requests', 'httplib2'):
    if _missing(_name):
        _module(_name)
if _missing('googleapiclient'):
    _module('googleapiclient')
    _module('googleapiclient.discovery', build=lambda *a, **k: None)

from bson.objectid import ObjectId                               # noqa: E402  (real or stand-in)
from pymongo import ReturnDocument                               # noqa: E402

# ---------------------------------------------------------------- stub for `extensions`
class _ExtensionsStub(types.ModuleType):
    """`from extensions import users_collection` works and gives a harmless mock. Tests that need a particular
    database answer pass their own mock into the function under test."""
    DEFAULT_USER_TZ = 'Asia/Kolkata'

    def __getattr__(self, name):
        if not name.endswith('_collection'):
            raise AttributeError(name)
        collection = MagicMock(name=name)
        setattr(self, name, collection)                            # same object every time
        return collection


extensions = _ExtensionsStub('extensions')
sys.modules['extensions'] = extensions

# ---------------------------------------------------------------- controllable time + notifications
CLOCK = {'now': dt.datetime(2026, 10, 30, 12, 0), 'week_start': dt.datetime(2026, 10, 26)}
NOTIFICATIONS = []
_module('services.time_utils',
        now_ist=lambda: CLOCK['now'], now_utc=lambda: CLOCK['now'],
        get_current_week_start_utc=lambda: CLOCK['week_start'],
        get_timezone=lambda *a, **k: None, user_today_key=lambda *a, **k: None)
_module('services.notifications', create_notification=lambda *a, **k: NOTIFICATIONS.append(a))

xp = importlib.import_module('services.xp')
xp.url_for = lambda *a, **k: '/x'                                # url_for needs a Flask request context
utils = importlib.import_module('services.utils')


# ---------------------------------------------------------------- route-function loader
class _PassthroughApp:
    def route(self, *a, **k):
        return lambda f: f

    def errorhandler(self, *a, **k):
        return lambda f: f


class FakeRequest:
    """Stands in for flask.request: set .form / .payload / .args before calling a route function."""
    def __init__(self):
        self.form, self.payload, self.args, self.files = {}, None, {}, {}
        self.headers, self.is_json, self.path, self.referrer, self.host = {}, False, '/', None, 'localhost'
        self.method = 'POST'

    def get_json(self, silent=False):
        return self.payload


def route_namespace(**extra):
    """Names a route module normally imports, as harmless stand-ins. Pass extras to override or add."""
    ns = {
        'app': _PassthroughApp(), 'login_required': lambda f: f, 'admin_required': lambda f: f,
        'limiter': types.SimpleNamespace(limit=lambda *a, **k: (lambda f: f)),
        'get_user_key': lambda: 'key', 'jsonify': lambda payload=None, **kw: payload if payload is not None else kw,
        're': re, 'ObjectId': ObjectId, 'ReturnDocument': ReturnDocument,
    }
    ns.update(extra)
    return ns


def load_functions(relative_path, names, namespace):
    """Executes the named top-level functions and constants from a source file inside `namespace`."""
    path = ROOT / relative_path
    source = path.read_text(encoding='utf-8-sig')
    lines = source.splitlines()
    wanted, found = set(names), set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            exec(compile('\n' * start + '\n'.join(lines[start:node.end_lineno]), str(path), 'exec'), namespace)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            targets = {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            if targets & wanted:
                exec(compile('\n' * (node.lineno - 1) + '\n'.join(lines[node.lineno - 1:node.end_lineno]), str(path), 'exec'), namespace)
                found |= targets & wanted
    missing = wanted - found
    if missing:
        raise AssertionError(f'{relative_path}: could not find {sorted(missing)}')
    return namespace