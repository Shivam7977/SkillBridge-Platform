import json

import os

import random

import re

import re as re_ext

import requests

import secrets

import pytz

from datetime import datetime, timezone, timedelta

from urllib.parse import urlparse

import bleach

from bs4 import BeautifulSoup

from bson.objectid import ObjectId

from flask import render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory

from flask_limiter.errors import RateLimitExceeded

from flask_login import login_user, logout_user, login_required, current_user

from itsdangerous import SignatureExpired, BadTimeSignature, BadSignature

from markupsafe import Markup

from pymongo import ReturnDocument

from werkzeug.utils import secure_filename

import resend

from ai_roadmap_generator import (generate_roadmap_with_ai, find_youtube_playlist,
                                  get_paid_course_link, GROQ_API_URL, GROQ_MODEL_FAST)

from recommendation_engine import get_recommended_projects

from extensions import (app, bcrypt, client, db, limiter, login_manager, scheduler, users_collection,
                        roadmaps_collection, projects_collection, commits_collection, messages_collection,
                        communities_collection, community_messages_collection, chat_history_collection,
                        notifications_collection, xp_history_collection, comments_collection,
                        interview_sessions_collection, deleted_accounts_collection)

from models import User, load_user

from services.time_utils import (now_ist, now_utc, get_current_week_start_utc, get_next_weekly_reset_utc,
                                 as_utc, utc_iso, fmt_utc, get_timezone, user_timezone_name,
                                 user_today_key, to_ist, fmt_ist)

from services.utils import (get_login_key, get_user_key, sanitize, flatten_data, TEMPLATE_NAMES, parse_skills)

from services.email_service import (app_base_url, make_unsubscribe_token, unsubscribe_url_for,
                                    send_email, send_reminder_digest_email)

from services.xp import (get_level_info, add_xp, check_and_award_profile_complete, recalculate_xp,
                         deduct_xp, update_streak)

from services.notifications import create_notification

from services.ai_limits import (INTERVIEW_SESSIONS_PER_DAY, INTERVIEW_CALL_SAFETY_CAP, time_until_reset,
                                check_ai_daily_limit, get_ai_usage_today)

from services.account_service import (is_sole_active_admin, sync_hidden_flag, log_account_deletion,
                                      purge_expired_accounts)

from services.jobs import reset_weekly_xp, check_inactive_roadmaps

from functools import wraps


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in.", "error")
            return redirect(url_for('login'))
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user_data or not user_data.get('is_admin'):
            flash("Access denied. Admins only.", "error")
            return redirect(url_for('main_page'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    # Users — pending-deletion accounts live in the Trash tab, not here
    users = list(users_collection.find({'pending_deletion': {'$ne': True}}, {
        'name': 1, 'email': 1, 'xp': 1, 'level': 1, 'badge': 1,
        'streak_count': 1, 'is_admin': 1, 'is_banned': 1, 'banned_at': 1, 'banned_by': 1,
        'created_at': 1, 'profile_pic': 1, 'ai_usage': 1, 'timezone': 1, 'username': 1,
        'current_status': 1, 'work_experience': 1, 'experience_years': 1, 'last_activity_date': 1
    }).sort('xp', -1))

    for u in users:
        u['_id'] = str(u['_id'])
        u['xp'] = u.get('xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        u['username'] = u.get('username', '')
        u['last_login'] = u.get('last_activity_date')
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"
        ai = u.get('ai_usage', {})
        today_key = user_today_key(u['_id'], u)
        u['ai_chat_today'] = ai.get(f'chat_{today_key}', 0)
        u['ai_roadmap_today'] = ai.get(f'roadmap_{today_key}', 0)

    # Projects — include views, likes, and comment counts
    projects = list(projects_collection.find().sort('created_at', -1))
    all_project_ids = [p['_id'] for p in projects]

    # Single aggregation for comment counts across all projects
    cmt_agg = comments_collection.aggregate([
        {'$match': {'project_id': {'$in': all_project_ids}, 'is_deleted': {'$ne': True}}},
        {'$group': {'_id': '$project_id', 'count': {'$sum': 1}}}
    ])
    cmt_counts = {str(doc['_id']): doc['count'] for doc in cmt_agg}

    for p in projects:
        p['_id'] = str(p['_id'])
        # Fetch username BEFORE converting created_by_id to string
        _creator_oid = p.get('created_by_id')
        creator = users_collection.find_one({'_id': _creator_oid}, {'username': 1}) if _creator_oid else None
        p['created_by_username'] = creator.get('username', '') if creator else ''
        p['created_by_id'] = str(_creator_oid or '')
        p['views'] = p.get('views', 0)
        p['like_count'] = len(p.get('likes', []))
        p['comment_count'] = cmt_counts.get(p['_id'], 0)
        p['bookmark_count'] = p.get('bookmark_count', 0)
        # Expose community_id so admin UI can offer "also delete community" option
        raw_cid = p.get('community_id')
        p['community_id'] = str(raw_cid) if raw_cid else None

    # Pending version approvals. Use this exact result for both the badge and
    # table so they cannot disagree when an upload is added during the request.
    pending_commits = list(commits_collection.find({'xp_status': 'pending'}).sort('timestamp', -1))
    for c in pending_commits:
        c['_id'] = str(c['_id'])
        _proj_oid = c.get('project_id')
        _user_oid = c.get('user_id')
        if _proj_oid and not isinstance(_proj_oid, ObjectId):
            try:
                _proj_oid = ObjectId(str(_proj_oid))
            except Exception:
                _proj_oid = None
        if _user_oid and not isinstance(_user_oid, ObjectId):
            try:
                _user_oid = ObjectId(str(_user_oid))
            except Exception:
                _user_oid = None
        c['project_id'] = str(_proj_oid or '')
        c['user_id'] = str(_user_oid or '')
        proj = projects_collection.find_one({'_id': _proj_oid}) if _proj_oid else None
        c['project_title'] = proj.get('title', 'Unknown') if proj else 'Unknown'
        # Ensure a pending upload still has a visible row when legacy records
        # are missing their optional display fields.
        c['user_name'] = c.get('user_name') or 'Unknown uploader'
        c['message'] = c.get('message') or 'Project version upload'
        if not hasattr(c.get('timestamp'), 'strftime'):
            c['timestamp'] = None
        uploader = users_collection.find_one({'_id': _user_oid}, {'username': 1}) if _user_oid else None
        c['user_username'] = uploader.get('username', '') if uploader else ''

    # Communities
    communities = list(communities_collection.find().sort('created_at', -1))
    for c in communities:
        c['_id'] = str(c['_id'])
        c['owner_id'] = str(c.get('owner_id', ''))
        c['member_count'] = len(c.get('members', []))
        _owner = users_collection.find_one({'_id': c.get('owner_id') if not isinstance(c.get('owner_id'), str) else ObjectId(c['owner_id'])}, {'username': 1}) if c.get('owner_id') else None
        c['owner_username'] = _owner.get('username', '') if _owner else ''

    # Roadmaps
    roadmaps = list(roadmaps_collection.find().sort('created_at', -1))
    for r in roadmaps:
        r['_id'] = str(r['_id'])
        r['user_id'] = str(r.get('user_id', ''))
        # Get user name
        user = users_collection.find_one({'_id': ObjectId(r['user_id'])}, {'name': 1, 'username': 1}) if r['user_id'] else None
        r['user_name'] = user.get('name', 'Unknown') if user else 'Unknown'
        r['username'] = user.get('username', '') if user else ''
        content = r.get('roadmap_content', {})
        if isinstance(content, str):
            try:
                import json as _json
                content = _json.loads(content)
            except:
                content = {}
        stages = content.get('stages', []) if isinstance(content, dict) else []
        r['stage_count'] = len(stages)
        r['completed_stages'] = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
        r['is_fully_complete'] = bool(stages) and r['completed_stages'] == r['stage_count']
        r['last_visited'] = r.get('last_activity')
        r['last_reminder_sent_at'] = r.get('last_reminder_sent_at')

    # Comments — fetch all, enrich with project title and user info
    raw_comments = list(comments_collection.find().sort('created_at', -1))
    # Build project_id -> title map for all referenced projects
    comment_project_ids = list({c.get('project_id') for c in raw_comments if c.get('project_id')})
    proj_map = {str(p['_id']): p.get('title', '—') for p in projects_collection.find({'_id': {'$in': comment_project_ids}}, {'title': 1})}

    all_comments = []
    for c in raw_comments:
        c['_id'] = str(c['_id'])
        c['project_id'] = str(c.get('project_id', ''))
        c['user_id'] = str(c.get('user_id', ''))
        c['parent_id'] = str(c.get('parent_id', '')) if c.get('parent_id') else None
        c['like_count'] = len(c.get('likes', []))
        c['project_title'] = proj_map.get(c['project_id'], '—')
        dt = c.get('created_at')
        c['created_at_iso'] = utc_iso(dt) if dt and hasattr(dt, 'strftime') else ''
        c['created_at_str'] = dt.strftime('%b %d, %Y') if dt and hasattr(dt, 'strftime') else '—'
        all_comments.append(c)

    total_comments = comments_collection.count_documents({})

    # Trash — accounts currently in their 30-day grace period, restorable
    trash_accounts = list(users_collection.find(
        {'pending_deletion': True},
        {'name': 1, 'email': 1, 'username': 1, 'deletion_requested_at': 1, 'deletion_requested_by': 1, 'purge_at': 1, 'is_banned': 1}
    ).sort('purge_at', 1))
    for t in trash_accounts:
        t['_id'] = str(t['_id'])
        purge_at = t.get('purge_at')
        t['days_left'] = max(0, (purge_at - now_utc()).days) if purge_at else 0

    # Deleted accounts (audit log — written only when the 30-day grace period actually expires)
    deleted_accounts = list(deleted_accounts_collection.find().sort('deleted_at', -1))
    for d in deleted_accounts:
        d['_id'] = str(d['_id'])

    # Stats
    all_projects_stats = list(projects_collection.find({}, {'views': 1, 'likes': 1}))
    stats = {
        'total_users': users_collection.count_documents({'pending_deletion': {'$ne': True}}),
        'total_projects': projects_collection.count_documents({}),
        'total_roadmaps': roadmaps_collection.count_documents({}),
        'total_communities': communities_collection.count_documents({}),
        'pending_approvals': len(pending_commits),
        'banned_users': users_collection.count_documents({'is_banned': True}),
        'trash_count': len(trash_accounts),
        'deleted_accounts': len(deleted_accounts),
        'total_xp_awarded': sum(u.get('xp', 0) for u in users_collection.find({}, {'xp': 1})),
        'total_views': sum(p.get('views', 0) for p in all_projects_stats),
        'total_likes': sum(len(p.get('likes', [])) for p in all_projects_stats),
        'total_comments': total_comments,
    }

    return render_template('admin.html',
        users=users,
        projects=projects,
        pending_commits=pending_commits,
        communities=communities,
        roadmaps=roadmaps,
        all_comments=all_comments,
        deleted_accounts=deleted_accounts,
        trash_accounts=trash_accounts,
        stats=stats
    )

@app.route('/admin/approve_commit/<commit_id>', methods=['POST'])
@login_required
@admin_required
def admin_approve_commit(commit_id):
    """Admin approves a project version upload — user gets +50 XP (exactly once)."""
    try:
        cid = ObjectId(commit_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid commit id'}), 400
    # Atomic claim: only ONE request can move pending -> approved, so XP can never be paid twice
    # (double click, two admin tabs, two admins at the same moment).
    commit = commits_collection.find_one_and_update(
        {'_id': cid, 'xp_status': 'pending'},
        {'$set': {'xp_status': 'approved', 'reviewed_at': now_utc()}},
        return_document=ReturnDocument.BEFORE)
    if not commit:
        existing = commits_collection.find_one({'_id': cid}, {'xp_status': 1})
        if not existing:
            return jsonify({'success': False, 'error': 'Commit not found'}), 404
        return jsonify({'success': False, 'error': f"Already {existing.get('xp_status', 'reviewed')} — nothing to approve."}), 409
    if not add_xp(str(commit['user_id']), 50, "Project version approved by admin"):
        # XP could not be paid (e.g. the user's account is gone) — undo, so the commit stays reviewable
        commits_collection.update_one({'_id': cid, 'xp_status': 'approved'}, {'$set': {'xp_status': 'pending'}, '$unset': {'reviewed_at': ''}})
        return jsonify({'success': False, 'error': 'Could not award XP to this user. The commit was left pending.'}), 500
    create_notification(
        str(commit['user_id']), 'approved',
        f'Your project upload "{commit.get("message", "version")}" was approved! +50 XP 🎉',
        url_for('view_project', project_id=str(commit['project_id']))
    )
    return jsonify({'success': True})

@app.route('/admin/reject_commit/<commit_id>', methods=['POST'])
@login_required
@admin_required
def admin_reject_commit(commit_id):
    """Admin rejects a PENDING project version upload — no XP awarded."""
    try:
        cid = ObjectId(commit_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid commit id'}), 400
    commit = commits_collection.find_one_and_update(
        {'_id': cid, 'xp_status': 'pending'},
        {'$set': {'xp_status': 'rejected', 'reviewed_at': now_utc()}},
        return_document=ReturnDocument.BEFORE)
    if not commit:
        existing = commits_collection.find_one({'_id': cid}, {'xp_status': 1})
        if not existing:
            return jsonify({'success': False, 'error': 'Commit not found'}), 404
        return jsonify({'success': False, 'error': f"Already {existing.get('xp_status', 'reviewed')} — only pending uploads can be rejected."}), 409
    create_notification(
        str(commit['user_id']), 'declined',
        f'Your project upload "{commit.get("message", "version")}" was rejected. No XP awarded.',
        url_for('view_project', project_id=str(commit['project_id']))
    )
    return jsonify({'success': True})

ADMIN_MAX_XP_ADJUST = 10000

@app.route('/admin/adjust_xp/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_adjust_xp(user_id):
    """Admin manually adjusts a user's XP (whole number, -10000 to +10000, user must exist)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Send JSON like {"points": 50, "reason": "..."}'}), 400
    raw_points = data.get('points', 0)
    if isinstance(raw_points, bool) or not re.fullmatch(r'-?\d{1,6}', str(raw_points).strip()):
        return jsonify({'success': False, 'error': f'Points must be a whole number between -{ADMIN_MAX_XP_ADJUST} and {ADMIN_MAX_XP_ADJUST}.'}), 400
    points = int(str(raw_points).strip())
    if points == 0:
        return jsonify({'success': False, 'error': 'Points cannot be 0'}), 400
    if abs(points) > ADMIN_MAX_XP_ADJUST:
        return jsonify({'success': False, 'error': f'Points must be between -{ADMIN_MAX_XP_ADJUST} and {ADMIN_MAX_XP_ADJUST} (you entered {points}).'}), 400
    reason = sanitize(str(data.get('reason') or 'Admin adjustment').strip())[:100] or 'Admin adjustment'
    try:
        target = ObjectId(user_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid user id'}), 400
    if not users_collection.find_one({'_id': target}, {'_id': 1}):
        return jsonify({'success': False, 'error': 'User not found'}), 404
    ok = add_xp(user_id, points, f"Admin: {reason}") if points > 0 else deduct_xp(user_id, abs(points), f"Admin: {reason}")
    if not ok:
        return jsonify({'success': False, 'error': 'XP could not be updated.'}), 500
    action = f"+{points}" if points > 0 else str(points)
    create_notification(
        user_id,
        'approved' if points > 0 else 'declined',
        f'An admin adjusted your XP: {action} XP. Reason: {reason}',
        url_for('settings')
    )
    user = users_collection.find_one({'_id': target}, {'xp': 1, 'badge': 1})
    return jsonify({'success': True, 'new_xp': user.get('xp', 0), 'badge': user.get('badge', '')})

@app.route('/admin/ban_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_ban_user(user_id):
    if user_id == current_user.id:
        return jsonify({'success': False, 'error': "You can't ban your own account."}), 400
    admin_doc = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'name': 1})
    admin_name = admin_doc.get('name', 'Admin') if admin_doc else 'Admin'
    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {'is_banned': True, 'banned_at': now_utc(), 'banned_by': admin_name}}
    )
    sync_hidden_flag(ObjectId(user_id))
    create_notification(
        user_id, 'declined',
        'Your account has been suspended by an admin. Contact support if you think this is a mistake.',
        '#'
    )
    return jsonify({'success': True})

@app.route('/admin/unban_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_unban_user(user_id):
    """Unban a user"""
    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$unset': {'is_banned': '', 'banned_at': '', 'banned_by': ''}}
    )
    sync_hidden_flag(ObjectId(user_id))
    return jsonify({'success': True})

@app.route('/admin/toggle_email_reminders/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_email_reminders(user_id):
    """Admin flips a user's roadmap-reminder email preference (e.g. on support request)."""
    user = users_collection.find_one({'_id': ObjectId(user_id)}, {'email_reminders_enabled': 1})
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    new_val = not user.get('email_reminders_enabled', True)
    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {'email_reminders_enabled': new_val, 'email_preferences_updated_at': now_utc()}}
    )
    return jsonify({'success': True, 'email_reminders_enabled': new_val})

@app.route('/admin/delete_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    """Admin schedules a user for deletion — moved to Trash for 30 days, not deleted immediately."""
    if user_id == current_user.id:
        return jsonify({'success': False, 'error': "You can't delete your own account."}), 400
    uid = ObjectId(user_id)
    admin_doc = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'name': 1})
    admin_name = admin_doc.get('name', 'Admin') if admin_doc else 'Admin'
    purge_at = now_utc() + timedelta(days=30)
    users_collection.update_one({'_id': uid}, {'$set': {
        'pending_deletion': True,
        'deletion_requested_at': now_utc(),
        'deletion_requested_by': admin_name,
        'purge_at': purge_at,
    }})
    sync_hidden_flag(uid)
    return jsonify({'success': True})

@app.route('/admin/restore_account/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_restore_account(user_id):
    """Pull a user out of the 30-day trash — undoes delete-request completely."""
    uid = ObjectId(user_id)
    users_collection.update_one(
        {'_id': uid},
        {'$unset': {'pending_deletion': '', 'deletion_requested_at': '', 'deletion_requested_by': '', 'purge_at': ''}}
    )
    sync_hidden_flag(uid)
    return jsonify({'success': True})

@app.route('/admin/user/<user_id>')
@login_required
@admin_required
def admin_user_detail(user_id):
    """Full detail view for a single user"""
    uid = ObjectId(user_id)
    user = users_collection.find_one({'_id': uid})
    if not user:
        flash("User not found.", "error")
        return redirect(url_for('admin_panel'))

    # Profile pic
    pic = user.get('profile_pic', 'default.jpg')
    user['profile_pic_url'] = f"/static/profile_pics/{pic}"
    user['_id'] = str(user['_id'])

    # XP info
    xp = user.get('xp', 0)
    level_info = get_level_info(xp)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
    user['xp'] = xp
    user['level_info'] = level_info
    user['progress_pct'] = progress_pct
    user['streak_count'] = user.get('streak_count', 0)
    user['member_since'] = to_ist(user.get('created_at')).strftime('%B %Y')
    user['last_login'] = user.get('last_activity_date')
    user['email_reminders_enabled'] = user.get('email_reminders_enabled', True)
    user['banned_at'] = user.get('banned_at')
    user['banned_by'] = user.get('banned_by')
    user['pending_deletion'] = user.get('pending_deletion', False)
    user['deletion_requested_at'] = user.get('deletion_requested_at')
    user['deletion_requested_by'] = user.get('deletion_requested_by')
    purge_at = user.get('purge_at')
    user['purge_at'] = purge_at
    user['days_left'] = max(0, (purge_at - now_utc()).days) if purge_at else 0

    # Projects
    projects = list(projects_collection.find({'created_by_id': uid}).sort('created_at', -1))
    for p in projects:
        p['_id'] = str(p['_id'])

    # Roadmaps
    roadmaps = list(roadmaps_collection.find({'user_id': uid}).sort('created_at', -1))
    for r in roadmaps:
        r['_id'] = str(r['_id'])
        content = r.get('roadmap_content', {})
        if isinstance(content, str):
            try:
                import json as _json
                content = _json.loads(content)
            except:
                content = {}
        stages = content.get('stages', []) if isinstance(content, dict) else []
        r['stage_count'] = len(stages)
        r['completed_stages'] = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
        r['is_fully_complete'] = bool(stages) and r['completed_stages'] == r['stage_count']
        r['last_visited'] = r.get('last_activity')
        r['last_reminder_sent_at'] = r.get('last_reminder_sent_at')

    # Certificates
    certificates = user.get('certificates', [])

    # Work Experience
    work_experience = user.get('work_experience', [])

    # Communities
    user_communities = list(communities_collection.find({'members': uid}))
    for c in user_communities:
        c['_id'] = str(c['_id'])
        c['is_owner'] = c.get('owner_id') == uid
        c['member_count'] = len(c.get('members', []))

    # XP History
    xp_history = list(xp_history_collection.find(
        {'user_id': uid}
    ).sort('timestamp', -1).limit(50))

    return render_template('admin_user.html',
        user=user,
        projects=projects,
        roadmaps=roadmaps,
        certificates=certificates,
        communities=user_communities,
        xp_history=xp_history,
        work_experience=work_experience
    )