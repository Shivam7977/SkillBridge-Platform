from datetime import datetime, timedelta, timezone

import pytz
from bson.objectid import ObjectId

from extensions import DEFAULT_USER_TZ, users_collection

def now_ist():
    """Legacy name kept for old call sites; timestamps are now stored as UTC."""
    return now_utc()


def now_utc():
    """Current UTC datetime stored as naive UTC for MongoDB consistency."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_current_week_start_utc():
    """Current week ka start — Monday 00:00 IST (= Sunday 18:30 UTC). IST mein DST nahi hota, isliye ye offset hamesha fixed rehta hai — single source of truth for weekly reset boundary (scheduler, add_xp, leaderboard sab isi ko use karte hain)."""
    now = now_utc()
    days_since_sunday = (now.weekday() + 1) % 7  # Mon=0..Sun=6 -> days since last Sunday
    candidate = (now - timedelta(days=days_since_sunday)).replace(hour=18, minute=30, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def get_next_weekly_reset_utc():
    """Agla weekly reset kab hoga (UTC mein, naive) — Sunday 18:30 UTC = Monday 00:00 IST."""
    return get_current_week_start_utc() + timedelta(days=7)


def as_utc(dt):
    """Return a naive UTC datetime from a datetime/string value."""
    if dt is None:
        return now_utc()
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return now_utc()
    if not isinstance(dt, datetime):
        return now_utc()
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_iso(dt):
    """ISO-8601 UTC timestamp for browser-side local timezone rendering."""
    return as_utc(dt).isoformat(timespec='microseconds') + 'Z'


def fmt_utc(dt, fmt='%I:%M %p'):
    return as_utc(dt).strftime(fmt)


def get_timezone(tz_name):
    if tz_name in pytz.all_timezones_set:
        return pytz.timezone(tz_name)
    return pytz.timezone(DEFAULT_USER_TZ)


def user_timezone_name(user_id):
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)}, {'timezone': 1}) or {}
        return user.get('timezone') or DEFAULT_USER_TZ
    except Exception:
        return DEFAULT_USER_TZ


def user_today_key(user_id, user_doc=None):
    tz_name = (user_doc or {}).get('timezone') or user_timezone_name(user_id)
    return datetime.now(get_timezone(tz_name)).strftime('%Y-%m-%d')


def to_ist(dt):
    """Backward-compatible display helper; prefer utc_iso + browser rendering."""
    return as_utc(dt)


def fmt_ist(dt, fmt='%I:%M %p'):
    """Backward-compatible formatter; now formats UTC until the browser localizes it."""
    return fmt_utc(dt, fmt)

