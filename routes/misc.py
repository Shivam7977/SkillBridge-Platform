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

from extensions import (app, bcrypt, client, db, s, limiter, login_manager, scheduler, users_collection,
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


@app.route('/api/onboarding/status')
@login_required
def onboarding_status():
    """Returns onboarding checklist status for current user"""
    user = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # If already completed, return immediately
    if user.get('onboarding_complete'):
        return jsonify({'show': False})

    uid = ObjectId(current_user.id)

    # Check each step
    profile_fields = ['about_me', 'location', 'education_college', 'education_degree', 'github_url']
    profile_done = all(user.get(f) for f in profile_fields)

    project_done = projects_collection.count_documents({'created_by_id': uid}) > 0
    roadmap_done = roadmaps_collection.count_documents({'user_id': uid}) > 0

    from bson.objectid import ObjectId as OID
    community_done = communities_collection.count_documents({'members': uid}) > 0

    steps = [
        {
            'id': 'profile',
            'title': 'Complete your profile',
            'desc': 'Add bio, location, education & GitHub URL',
            'xp': '+30 XP',
            'done': profile_done,
            'link': '/profile',
            'icon': 'fa-user-circle'
        },
        {
            'id': 'project',
            'title': 'Create your first project',
            'desc': 'Share what you are building with the community',
            'xp': '+10 XP',
            'done': project_done,
            'link': '/create_project',
            'icon': 'fa-layer-group'
        },
        {
            'id': 'roadmap',
            'title': 'Generate a learning roadmap',
            'desc': 'Use AI to map out your learning path',
            'xp': '+5 XP per stage',
            'done': roadmap_done,
            'link': '/roadmap_generator',
            'icon': 'fa-magic'
        },
        {
            'id': 'community',
            'title': 'Join a community',
            'desc': 'Connect with builders who share your interests',
            'xp': '+10 XP',
            'done': community_done,
            'link': '/communities',
            'icon': 'fa-users'
        },
    ]

    all_done = all(s['done'] for s in steps)
    if all_done:
        users_collection.update_one({'_id': uid}, {'$set': {'onboarding_complete': True}})
        return jsonify({'show': False})

    return jsonify({'show': True, 'steps': steps})

@app.route('/api/onboarding/dismiss', methods=['POST'])
@login_required
def onboarding_dismiss():
    """Mark onboarding as complete (dismissed by user)"""
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'onboarding_complete': True}}
    )
    return jsonify({'success': True})

@app.route('/api/user/timezone', methods=['POST'])
@login_required
def update_user_timezone():
    """Store the browser-detected IANA timezone without asking the user."""
    data = request.get_json(silent=True) or {}
    tz_name = data.get('timezone', '').strip()
    if tz_name not in pytz.all_timezones_set:
        return jsonify({'success': False, 'error': 'Invalid timezone'}), 400
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'timezone': tz_name, 'timezone_updated_at': now_utc()}}
    )
    return jsonify({'success': True, 'timezone': tz_name})

@app.route('/api/user/email_preferences', methods=['POST'])
@login_required
def update_email_preferences():
    """Update optional email reminder preferences; in-app notifications stay enabled."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('email_reminders_enabled', False))
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'email_reminders_enabled': enabled, 'email_preferences_updated_at': now_utc()}}
    )
    return jsonify({'success': True, 'email_reminders_enabled': enabled})

@app.route('/unsubscribe/<token>')
def unsubscribe_email_reminders(token):
    """One-click unsubscribe from roadmap reminder emails via signed token."""
    try:
        payload = s.loads(token, salt='email-reminders-unsubscribe')
        user_id = payload.get('user_id')
        obj_id = ObjectId(user_id)
    except (BadTimeSignature, BadSignature, ValueError, TypeError):
        return render_template('unsubscribe.html', success=False), 400

    result = users_collection.update_one(
        {'_id': obj_id},
        {'$set': {'email_reminders_enabled': False, 'email_preferences_updated_at': now_utc()}}
    )
    return render_template('unsubscribe.html', success=result.matched_count > 0)

@app.route('/api/notifications')
@login_required
def get_notifications():
    """Get all notifications for current user"""
    notifs = list(notifications_collection.find(
        {'user_id': ObjectId(current_user.id)}
    ).sort('created_at', -1).limit(20))
    result = []
    for n in notifs:
        result.append({
            'id': str(n['_id']),
            'type': n.get('type', 'info'),
            'message': n.get('message', ''),
            'link': n.get('link', '#'),
            'is_read': n.get('is_read', False),
            'created_at': fmt_utc(n.get('created_at'), '%b %d, %I:%M %p') + ' UTC',
            'created_at_iso': utc_iso(n.get('created_at'))
        })
    unread_count = notifications_collection.count_documents(
        {'user_id': ObjectId(current_user.id), 'is_read': False}
    )
    return jsonify({'notifications': result, 'unread_count': unread_count})

@app.route('/api/notifications/read/<notif_id>', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    """Mark a single notification as read"""
    notifications_collection.update_one(
        {'_id': ObjectId(notif_id), 'user_id': ObjectId(current_user.id)},
        {'$set': {'is_read': True}}
    )
    return jsonify({'success': True})

@app.route('/api/notifications/read_all', methods=['POST'])
@login_required
def mark_all_read():
    """Mark all notifications as read"""
    notifications_collection.update_many(
        {'user_id': ObjectId(current_user.id), 'is_read': False},
        {'$set': {'is_read': True}}
    )
    return jsonify({'success': True})

@app.route('/search')
@login_required
def search():
    """Search users by name or @username"""
    query = request.args.get('q', '').strip()
    results = []
    if query:
        search_query = query.lstrip('@')
        users = list(users_collection.find(
            {'$or': [
                {'name': {'$regex': search_query, '$options': 'i'}},
                {'username': {'$regex': search_query, '$options': 'i'}},
            ], 'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}},
            {'name': 1, 'username': 1, 'title': 1, 'profile_pic': 1,
             'xp': 1, 'badge': 1, 'level': 1, 'known_skills': 1}
        ).limit(20))
        for u in users:
            if str(u['_id']) != current_user.id:  # exclude self
                results.append({
                    '_id': str(u['_id']),
                    'name': u.get('name', 'User'),
                    'username': u.get('username', ''),
                    'title': u.get('title', 'SkillBridge Member'),
                    'profile_pic': u.get('profile_pic', 'default.jpg'),
                    'xp': u.get('xp', 0),
                    'badge': u.get('badge', 'Bronze Builder'),
                    'known_skills': u.get('known_skills', [])[:4],
                })
    _sxp = 0; _sli = get_level_info(0); _sstreak = 0; _sppct = 0
    if current_user.is_authenticated:
        _su = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1}) or {}
        _sxp = _su.get('xp', 0); _sli = get_level_info(_sxp)
        _sstreak = _su.get('streak_count', 0)
        _sppct = int((_sxp / _sli['next_xp']) * 100) if _sli['next_xp'] != "Max" else 100
    return render_template('search.html', results=results, query=query, xp=_sxp, level_info=_sli, streak=_sstreak, progress_pct=_sppct)

@app.route('/api/search_users')
@login_required
def api_search_users():
    """Live search API — called by dashboard search bar dropdown"""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})
    search_q = query.lstrip('@')
    users = list(users_collection.find(
        {'$or': [
            {'name': {'$regex': search_q, '$options': 'i'}},
            {'username': {'$regex': search_q, '$options': 'i'}},
        ], 'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}},
        {'name': 1, 'username': 1, 'title': 1, 'profile_pic': 1, 'badge': 1, 'xp': 1}
    ).limit(8))
    results = []
    for u in users:
        if str(u['_id']) != current_user.id:
            results.append({
                '_id': str(u['_id']),
                'name': u.get('name', 'User'),
                'username': u.get('username', ''),
                'title': u.get('title', 'SkillBridge Member'),
                'profile_pic': u.get('profile_pic', 'default.jpg'),
                'badge': u.get('badge', 'Bronze Builder'),
                'xp': u.get('xp', 0),
            })
    return jsonify({'results': results})

@app.route('/health')
def health():
    try:
        db.command("ping")
        return {"status": "ok", "db": "connected"}, 200
    except Exception as e:
        return {"status": "degraded", "db": str(e)}, 503

