import json
from datetime import timezone

from apscheduler.triggers.cron import CronTrigger
from bson.objectid import ObjectId

from extensions import (scheduler, UTC_TZ, users_collection, roadmaps_collection, notifications_collection)
from services.account_service import purge_expired_accounts
from services.email_service import send_reminder_digest_email
from services.notifications import create_notification
from services.time_utils import now_utc, as_utc
def reset_weekly_xp():
    """
    Runs every Monday at 12:00 AM UTC.
    Resets weekly_xp to 0 for ALL users — clean slate every week.
    Prevents stale XP data from accumulating in MongoDB.
    """
    try:
        result = users_collection.update_many(
            {},
            {'$set': {'weekly_xp': 0, 'weekly_xp_updated': None}}
        )
        print(f"✅ Weekly XP Reset complete — {result.modified_count} users reset to 0")
    except Exception as e:
        print(f"❌ Weekly XP Reset failed: {e}")


def check_inactive_roadmaps():
    """Runs daily (7 PM IST). Tracks per-roadmap inactivity but sends one digest per user."""
    try:
        thresholds = [(3, 1), (7, 2), (14, 3)]
        now = now_utc()
        candidates = roadmaps_collection.find({
            '$or': [
                {'reminder_stage': {'$lt': 3}},
                {'reminder_stage': {'$exists': False}}
            ]
        })
        skip_user_ids = set(u['_id'] for u in users_collection.find(
            {'$or': [{'is_banned': True}, {'pending_deletion': True}]}, {'_id': 1}
        ))

        user_reminders = {}
        for rm in candidates:
            # Skip banned or trashed users — they can't use the platform, no point nudging them
            if rm.get('user_id') in skip_user_ids:
                continue

            # Skip roadmaps that are already 100% complete — nothing to "resume"
            content = rm.get('roadmap_content', {})
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}
            stages = content.get('stages', []) if isinstance(content, dict) else []
            if stages and all(isinstance(s, dict) and s.get('completed') for s in stages):
                continue

            last_activity = rm.get('last_activity') or rm.get('created_at')
            if not last_activity:
                continue
            last_activity = as_utc(last_activity)
            days_inactive = (now - last_activity).days
            current_stage = rm.get('reminder_stage', 0)

            target_stage, target_days = None, None
            for days, stage in thresholds:
                if days_inactive >= days and stage > current_stage:
                    target_stage, target_days = stage, days

            if target_stage is None:
                continue

            uid = str(rm.get('user_id'))
            user_reminders.setdefault(uid, []).append((rm, target_stage, target_days))

        sent_count = 0
        for uid, items in user_reminders.items():
            try:
                user = users_collection.find_one({'_id': ObjectId(uid)})
            except Exception:
                continue
            if not user:
                continue

            if len(items) == 1:
                rm, _, days = items[0]
                goal = rm.get('goal', 'your roadmap')
                message = f'Your "{goal}" roadmap is waiting - {days} days no activity!'
                link = f"/roadmap/{rm['_id']}"
            else:
                names = ", ".join(rm.get('goal', 'a roadmap') for rm, _, _ in items)
                message = f'{len(items)} of your roadmaps need attention: {names}'
                link = "/my_roadmaps"

            create_notification(uid, 'info', message, link)

            if user.get('email') and user.get('email_reminders_enabled', True):
                send_reminder_digest_email(user['email'], user.get('name', 'there'), items, uid)

            for rm, target_stage, _ in items:
                roadmaps_collection.update_one(
                    {'_id': rm['_id']},
                    {'$set': {'reminder_stage': target_stage, 'last_reminder_sent_at': now}}
                )
            sent_count += 1

        print(f"✅ Roadmap inactivity check done — {sent_count} users notified")
    except Exception as e:
        print(f"❌ Roadmap inactivity check failed: {e}")

scheduler.add_job(
    reset_weekly_xp,
    CronTrigger(day_of_week='sun', hour=18, minute=30, timezone=UTC_TZ),
    id='weekly_xp_reset',
    replace_existing=True
)
scheduler.start()
print("✅ Weekly XP reset scheduler started — resets every Monday 12:00 AM UTC")

scheduler.add_job(
    check_inactive_roadmaps,
    CronTrigger(hour=13, minute=30, timezone=UTC_TZ),
    id='roadmap_inactivity_check',
    replace_existing=True
)
print("✅ Roadmap inactivity reminder scheduler started — runs daily at 7 PM IST")

scheduler.add_job(
    purge_expired_accounts,
    CronTrigger(hour=18, minute=30, timezone=UTC_TZ),
    id='purge_expired_accounts',
    replace_existing=True
)
print("✅ Trash purge scheduler started — runs daily at midnight IST")

