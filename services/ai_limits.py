from datetime import datetime, timedelta

from bson.objectid import ObjectId
from pymongo import ReturnDocument

from extensions import users_collection, DEFAULT_USER_TZ
from services.time_utils import get_timezone, user_today_key

INTERVIEW_SESSIONS_PER_DAY = 5
INTERVIEW_CALL_SAFETY_CAP = 250

def time_until_reset(user_id):
    """Human-readable countdown to this user's own local midnight (when
    their daily AI limits reset), computed from THEIR stored IANA timezone
    — not a hardcoded region. Works correctly no matter where the user is:
    someone in Tokyo, Lagos, or New York each get their own accurate
    countdown to their own midnight, not India's."""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)}, {'timezone': 1})
        user_tz = get_timezone((user or {}).get('timezone') or DEFAULT_USER_TZ)
        now = datetime.now(user_tz)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = next_midnight - now
        total_minutes = int(remaining.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h"
        return f"{max(minutes, 1)}m"
    except Exception as e:
        print(f"time_until_reset error: {e}")
        return "at your local midnight"



def check_ai_daily_limit(user_id, action='chat', limit=50):
    """
    Atomically check-and-increment AI usage for a user per day in their
    browser timezone, using MongoDB's find_one_and_update so concurrent
    requests (two tabs, a double-click, multiple gunicorn workers) can't
    both read the same stale count and both slip through — the limit check
    and the increment happen in ONE atomic DB operation, not a separate
    read-then-write (which had a race window before this fix).
    action: 'chat' (limit=50), 'roadmap' (limit=10), 'interview_sessions'
    (limit=5 interviews/day; counted when a new interview starts), or
    'interview' (hidden per-call abuse backstop, not shown to users).
    Returns (allowed: bool, today_count: int, limit: int).
    Auto-cleans usage entries older than 7 days.
    """
    try:
        user = users_collection.find_one(
            {'_id': ObjectId(user_id)},
            {'ai_usage': 1, 'timezone': 1}
        )
        if not user:
            return False, 0, limit

        user_tz = get_timezone(user.get('timezone') or DEFAULT_USER_TZ)
        today_key = datetime.now(user_tz).strftime('%Y-%m-%d')
        usage_key = f"{action}_{today_key}"
        mongo_key = f"ai_usage.{usage_key}"

        # Atomic: only increments if today's count is still under the limit
        # (or doesn't exist yet) — the check-and-write is a single DB op, so
        # two simultaneous requests can never both pass past the limit.
        result = users_collection.find_one_and_update(
            {
                '_id': ObjectId(user_id),
                '$or': [
                    {mongo_key: {'$exists': False}},
                    {mongo_key: {'$lt': limit}}
                ]
            },
            {'$inc': {mongo_key: 1}},
            projection={'ai_usage': 1},
            return_document=ReturnDocument.AFTER
        )

        if result is not None:
            ai_usage = result.get('ai_usage', {})
            new_count = ai_usage.get(usage_key, 1)

            # Auto-cleanup: remove entries older than 7 days (best-effort,
            # runs silently, not part of the atomic op above)
            cutoff = (datetime.now(user_tz) - timedelta(days=7)).strftime('%Y-%m-%d')
            cleaned = {k: v for k, v in ai_usage.items() if k.split('_')[-1] >= cutoff}
            if len(cleaned) < len(ai_usage):
                users_collection.update_one(
                    {'_id': ObjectId(user_id)},
                    {'$set': {'ai_usage': cleaned}}
                )

            return True, new_count, limit

        # Not allowed — limit already reached (or hit exactly this instant by
        # a concurrent request). Read current count just for the message.
        current = users_collection.find_one({'_id': ObjectId(user_id)}, {'ai_usage': 1}) or {}
        today_count = current.get('ai_usage', {}).get(usage_key, limit)
        return False, today_count, limit

    except Exception as e:
        print(f"AI limit check error: {e}")
        return True, 0, limit  # fail open — don’t block user on DB error


def get_ai_usage_today(user_id):
    """Returns today's AI usage counts for a user in their browser timezone."""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)}, {'ai_usage': 1, 'timezone': 1})
        if not user:
            return {'chat_used': 0, 'chat_limit': 50, 'roadmap_used': 0, 'roadmap_limit': 10, 'interview_sessions_used': 0, 'interview_sessions_limit': INTERVIEW_SESSIONS_PER_DAY}
        today_key = user_today_key(user_id, user)
        ai_usage = user.get('ai_usage', {})
        return {
            'chat_used':      ai_usage.get(f'chat_{today_key}', 0),
            'chat_limit':     50,
            'roadmap_used':   ai_usage.get(f'roadmap_{today_key}', 0),
            'roadmap_limit':  10,
            # Session-level counter (what settings.html displays as "X / 5")
            'interview_sessions_used': ai_usage.get(f'interview_sessions_{today_key}', 0),
            'interview_sessions_limit': INTERVIEW_SESSIONS_PER_DAY,
        }
    except Exception as e:
        print(f"get_ai_usage_today error: {e}")
        return {'chat_used': 0, 'chat_limit': 50, 'roadmap_used': 0, 'roadmap_limit': 10, 'interview_sessions_used': 0, 'interview_sessions_limit': INTERVIEW_SESSIONS_PER_DAY}


