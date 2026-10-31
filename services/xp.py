from datetime import datetime, timezone
import json

from bson.objectid import ObjectId
from flask import url_for
from pymongo import ReturnDocument

from extensions import (users_collection, projects_collection, commits_collection, roadmaps_collection,
                        xp_history_collection, comments_collection, communities_collection,
                        community_messages_collection, chat_history_collection, notifications_collection,
                        deleted_accounts_collection, interview_sessions_collection)
from services.notifications import create_notification
from services.time_utils import now_ist, now_utc, get_current_week_start_utc, get_timezone, user_today_key
from extensions import DEFAULT_USER_TZ

def get_level_info(xp):
    """XP ke hisaab se Level aur Badge return karega"""
    if xp < 200:
        return {"level": 1, "badge": "Bronze Builder", "icon": "🥉", "next_xp": 200}
    elif xp < 500:
        return {"level": 2, "badge": "Silver Coder", "icon": "🥈", "next_xp": 500}
    elif xp < 1000:
        return {"level": 3, "badge": "Gold Developer", "icon": "🥇", "next_xp": 1000}
    elif xp < 2000:
        return {"level": 4, "badge": "Platinum Architect", "icon": "💎", "next_xp": 2000}
    else:
        return {"level": 5, "badge": "Diamond Founder", "icon": "🚀", "next_xp": "Max"}


def _ensure_numeric_xp_fields(uid):
    """Repairs missing / null / text xp & weekly_xp so that $inc can be used safely."""
    for field in ('xp', 'weekly_xp'):
        users_collection.update_one({'_id': uid, field: {'$not': {'$type': 'number'}}}, {'$set': {field: 0}})


def add_xp(user_id, points, reason="Action"):
    """Adds XP to a user. Uses atomic $inc, so two XP events at the same moment can never overwrite each other."""
    try:
        if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
            return False
        uid = ObjectId(user_id)
        if not users_collection.find_one({'_id': uid}, {'_id': 1}):
            return False
        _ensure_numeric_xp_fields(uid)

        after = users_collection.find_one_and_update(
            {'_id': uid}, {'$inc': {'xp': points}}, return_document=ReturnDocument.AFTER)
        if not after:
            return False
        new_xp = max(0, int(after.get('xp', 0)))
        old_level = get_level_info(max(0, new_xp - points))['level']
        new_level_info = get_level_info(new_xp)
        users_collection.update_one({'_id': uid}, {'$set': {'level': new_level_info['level'], 'badge': new_level_info['badge']}})

        # --- WEEKLY XP: reset on the first gain of a new week, otherwise $inc (both atomic) ---
        now_time = now_utc()
        week_start = get_current_week_start_utc()
        reset = users_collection.update_one(
            {'_id': uid, '$or': [{'weekly_xp_updated': {'$exists': False}}, {'weekly_xp_updated': None},
                                 {'weekly_xp_updated': {'$lt': week_start}}]},
            {'$set': {'weekly_xp': points, 'weekly_xp_updated': now_time}})
        if not reset.matched_count:
            users_collection.update_one(
                {'_id': uid}, {'$inc': {'weekly_xp': points}, '$set': {'weekly_xp_updated': now_time}})

        # Log XP history
        xp_history_collection.insert_one({
            'user_id': uid, 'points': points, 'reason': reason, 'type': 'earn', 'timestamp': now_ist()
        })

        if new_level_info['level'] > old_level:
            print(f"🎉 LEVEL UP! User {after.get('name')} is now Level {new_level_info['level']} ({new_level_info['badge']})")
            users_collection.update_one({'_id': uid}, {'$set': {'pending_levelup': new_level_info['badge']}})
            create_notification(str(user_id), 'levelup', f'🎉 You reached Level {new_level_info["level"]}: {new_level_info["badge"]}!', url_for('settings'))

        return True
    except Exception as e:
        print(f"Error adding XP: {e}")
        return False



def check_and_award_profile_complete(user_id):
    """Re-checks ALL mandatory fields across every profile card (not just the one
    just saved) and awards the one-time +30 XP 'Profile 100% Complete' bonus the
    moment the very last required card becomes complete. Safe to call after any
    individual card save — it's a no-op if already awarded or still incomplete."""
    try:
        user_data = users_collection.find_one({'_id': ObjectId(user_id)})
        if not user_data or user_data.get('profile_complete_reward'):
            return False

        # Core Identity — Name, Job Title, Location mandatory (Bio/Pic are user's choice)
        if not (user_data.get('name','').strip() and user_data.get('title','').strip() and user_data.get('location','').strip()):
            return False

        # Mastery & Learning — at least one skill on each side
        if not user_data.get('known_skills') or not user_data.get('learning_skills'):
            return False

        # Career Parameters — Current Status drives the rest of the profile
        current_status = user_data.get('current_status','').strip()
        if not current_status:
            return False

        # Education — at least one entry, every field on every entry filled
        # (To Year is exempt when "Currently studying" is checked)
        education = user_data.get('education', [])
        if not education:
            return False
        for edu in education:
            if not (edu.get('institution') and edu.get('degree') and edu.get('field')
                    and edu.get('grade_type') and edu.get('grade_value') and edu.get('from_year')
                    and (edu.get('is_current') or edu.get('to_year'))):
                return False

        # Work Experience — only required for non-Students; achievements stay optional
        if current_status != 'Student':
            work_experience = user_data.get('work_experience', [])
            if not work_experience:
                return False
            for exp in work_experience:
                if not (exp.get('company') and exp.get('role') and exp.get('from_year')
                        and (exp.get('is_current') or exp.get('to_year'))):
                    return False

        # Claim the one-time reward atomically BEFORE paying it: a double-click or two tabs can't pay twice.
        claimed = users_collection.find_one_and_update(
            {'_id': ObjectId(user_id), 'profile_complete_reward': {'$ne': True}},
            {'$set': {'profile_complete_reward': True}})
        if not claimed:
            return False
        if not add_xp(user_id, 30, "Profile 100% Complete"):
            users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': {'profile_complete_reward': False}})
            return False
        return True
    except Exception as e:
        print(f"Error checking profile completeness: {e}")
        return False



def recalculate_xp(user_id):
    """Recalculates XP from scratch based on actual user data in DB.
    Fixes any double-counting or incorrect XP values."""
    try:
        uid = ObjectId(user_id)
        user = users_collection.find_one({'_id': uid})
        if not user:
            return False

        xp = 0

        # 1. GitHub connected — +25 (one-time)
        if user.get('github_reward_claimed'):
            xp += 25

        # 2. Profile 100% complete — +30 (one-time)
        if user.get('profile_complete_reward'):
            xp += 30

        # 3. Projects created — +10 each
        project_count = projects_collection.count_documents({'created_by_id': uid})
        xp += project_count * 10

        # 4. Project versions uploaded — +50 each
        upload_count = commits_collection.count_documents({'user_id': uid, 'xp_status': 'approved'})   # pending/rejected never count
        xp += upload_count * 50

        # 5. Certificates — +15 each
        cert_count = len(user.get('certificates', []))
        xp += cert_count * 15

        # 6. Completed roadmap stages — +5 each
        user_roadmaps = list(roadmaps_collection.find({'user_id': uid}))
        completed_stages = 0
        for roadmap in user_roadmaps:
            content_data = roadmap.get('roadmap_content', {})
            if isinstance(content_data, str):
                import json as _json
                try: content_data = _json.loads(content_data)
                except: content_data = {}
            stages = content_data.get('stages', []) if isinstance(content_data, dict) else []
            completed_stages += sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
        xp += completed_stages * 5

        # 7. Streak bonuses already earned — keep them
        # These are already baked into XP from add_xp calls, but we track separately
        xp += user.get('streak_bonus_xp', 0)

        # This tally can only ever LOWER XP (to fix double-counting). If it comes out higher than the stored XP,
        # it is missing some XP source, so it must never be used to give a user more XP.
        try:
            stored_xp = max(0, int(user.get('xp', 0) or 0))
        except (TypeError, ValueError):
            stored_xp = 0
        xp = min(xp, stored_xp)

        # Update DB with correct XP
        level_info = get_level_info(xp)
        users_collection.update_one(
            {'_id': uid},
            {'$set': {
                'xp': xp,
                'level': level_info['level'],
                'badge': level_info['badge']
            }}
        )
        print(f"✅ Recalculated XP for {user.get('name')}: {xp} XP ({level_info['badge']})")
        return xp
    except Exception as e:
        print(f"Recalculate XP error: {e}")
        return False


def deduct_xp(user_id, points, reason="Action Reversed"):
    """Takes XP away (abuse prevention). Atomic, and XP / weekly XP can never go below 0."""
    try:
        if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
            return False
        uid = ObjectId(user_id)
        if not users_collection.find_one({'_id': uid}, {'_id': 1}):
            return False
        _ensure_numeric_xp_fields(uid)

        after = users_collection.find_one_and_update(
            {'_id': uid, 'xp': {'$gte': points}}, {'$inc': {'xp': -points}}, return_document=ReturnDocument.AFTER)
        if not after:        # not enough XP to subtract the full amount -> floor at 0
            after = users_collection.find_one_and_update(
                {'_id': uid, 'xp': {'$lt': points}}, {'$set': {'xp': 0}}, return_document=ReturnDocument.AFTER)
        if not after:
            return False
        if not users_collection.update_one({'_id': uid, 'weekly_xp': {'$gte': points}}, {'$inc': {'weekly_xp': -points}}).matched_count:
            users_collection.update_one({'_id': uid, 'weekly_xp': {'$lt': points}}, {'$set': {'weekly_xp': 0}})

        new_level_info = get_level_info(max(0, int(after.get('xp', 0))))
        users_collection.update_one({'_id': uid}, {'$set': {'level': new_level_info['level'], 'badge': new_level_info['badge']}})
        print(f"📉 XP DEDUCTED: -{points} from user {after.get('name')} for: {reason}")
        xp_history_collection.insert_one({
            'user_id': uid, 'points': -points, 'reason': reason, 'type': 'deduct', 'timestamp': now_ist()
        })
        return True
    except Exception as e:
        print(f"Error deducting XP: {e}")
        return False


def update_streak(user_id):
    """Login par activity streak track karne ka function"""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)}) or {}
        user_tz = get_timezone(user.get('timezone') or DEFAULT_USER_TZ)
        today = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        last_login = user.get('last_activity_date')
        streak = user.get('streak_count', 0)

        if last_login:
            if isinstance(last_login, str):
                last_login = datetime.fromisoformat(last_login.replace("Z", "+00:00"))
            if last_login.tzinfo is None:
                last_login = last_login.replace(tzinfo=timezone.utc)
            # Convert last_login to the user's browser timezone before comparing
            last_login = last_login.astimezone(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
            diff = (today - last_login).days

            if diff == 0:  # Same day login — keep streak, just update timestamp
                pass
            elif diff == 1:  # Consecutive day — increment
                streak += 1
                if streak == 7:
                    add_xp(user_id, 35, "7-day activity streak")
                    users_collection.update_one({'_id': ObjectId(user_id)}, {'$inc': {'streak_bonus_xp': 35}})
                    print(f"🔥 7-DAY STREAK! User {user.get('name')} earned 35 XP")
                if streak == 30:
                    add_xp(user_id, 100, "30-day activity streak")
                    users_collection.update_one({'_id': ObjectId(user_id)}, {'$inc': {'streak_bonus_xp': 100}})
                    print(f"🔥🔥 30-DAY STREAK! User {user.get('name')} earned 100 XP")
            else:  # Streak broken
                streak = 1
        else:
            streak = 1  # First ever login — set to 1 immediately

        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'streak_count': streak, 'last_activity_date': now_ist()}}
        )
    except Exception as e:
        print(f"Streak update error: {e}")