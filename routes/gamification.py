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


@app.route('/leaderboard')
@login_required
def leaderboard():

    # ── ALL TIME: top 50 by total XP ──
    visible_filter = {'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}}
    all_time = list(users_collection.find(
        visible_filter, {'name': 1, 'xp': 1, 'level': 1, 'badge': 1, 'streak_count': 1, 'profile_pic': 1}
    ).sort('xp', -1).limit(50))

    # ── WEEKLY: top 50 by weekly_xp, reset every Monday 00:00 IST ──
    week_start = get_current_week_start_utc()
    next_reset = get_next_weekly_reset_utc()

    weekly = list(users_collection.find(
        {**visible_filter, 'weekly_xp_updated': {'$gte': week_start}},
        {'name': 1, 'weekly_xp': 1, 'level': 1, 'badge': 1, 'streak_count': 1, 'profile_pic': 1}
    ).sort('weekly_xp', -1).limit(50))

    # Compute current user rank in all-time
    current_user_xp = 0
    current_user_rank = None
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1})
    if user_data:
        current_user_xp = user_data.get('xp', 0)
        current_user_rank = users_collection.count_documents({'xp': {'$gt': current_user_xp}}) + 1

    # Add profile pic URLs and stringify IDs
    for u in all_time:
        u['_id'] = str(u['_id'])
        u['xp'] = u.get('xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"

    for u in weekly:
        u['_id'] = str(u['_id'])
        u['weekly_xp'] = u.get('weekly_xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"

    # Fallback text for before browser JS localizes it (same corrected helper, no drift)
    fallback_hours = max(0, int((next_reset - now_utc()).total_seconds() // 3600))

    # Sidebar widget data
    level_info = get_level_info(current_user_xp)
    progress_pct = int((current_user_xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
    user_full = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'streak_count': 1})
    streak = user_full.get('streak_count', 1) if user_full else 1

    return render_template('leaderboard.html',
        all_time=all_time,
        weekly=weekly,
        current_user_rank=current_user_rank,
        current_user_xp=current_user_xp,
        next_reset_iso=utc_iso(next_reset),
        fallback_hours=fallback_hours,
        week_start_iso=utc_iso(week_start),
        level_info=level_info,
        progress_pct=progress_pct,
        streak=streak
    )

@app.route('/api/recalculate_xp', methods=['POST'])      # POST only: a link/image tag can no longer trigger it
@login_required
def api_recalculate_xp():
    """Recalculates current user XP from scratch — fixes any double counting"""
    new_xp = recalculate_xp(current_user.id)
    if new_xp is not False:
        level_info = get_level_info(new_xp)
        return jsonify({'success': True, 'xp': new_xp, 'badge': level_info['badge']})
    return jsonify({'success': False}), 500

@app.route('/api/xp_history')
@login_required
def xp_history():
    """Returns full XP transaction history for current user"""
    history = list(xp_history_collection.find(
        {'user_id': ObjectId(current_user.id)}
    ).sort('timestamp', -1))

    user_tz = get_timezone(user_timezone_name(current_user.id))

    result = []
    for h in history:
        ts = h.get('timestamp')
        local_dt = pytz.utc.localize(as_utc(ts)).astimezone(user_tz) if ts else None
        result.append({
            'points': h.get('points', 0),
            'reason': h.get('reason', 'Unknown'),
            'type': h.get('type', 'earn'),
            'timestamp_iso': utc_iso(ts),
            'day_key': local_dt.strftime('%Y-%m-%d') if local_dt else '',
        })

    # Build daily chart data (last 30 days) — bucketed by user's own local day, not UTC
    from collections import defaultdict
    daily = defaultdict(int)
    for h in result:
        if h['type'] == 'earn':
            daily[h['day_key']] += h['points']

    # Sort by date
    daily_sorted = sorted(daily.items())
    chart_data = [{'date': d, 'xp': x} for d, x in daily_sorted]

    return jsonify({
        'history': result,
        'chart_data': chart_data,
        'total_earned': sum(h['points'] for h in result if h['type'] == 'earn'),
        'total_deducted': abs(sum(h['points'] for h in result if h['type'] == 'deduct')),
        'total_transactions': len(result)
    })

@app.route('/api/check_levelup')
@login_required
def check_levelup():
    """Called by frontend to check if a level-up toast should be shown"""
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    pending = user_data.get('pending_levelup')
    if pending:
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$unset': {'pending_levelup': '$set'}})
        return jsonify({'levelup': True, 'badge': pending})
    return jsonify({'levelup': False})

@app.route('/api/ai_usage')
@login_required
def api_ai_usage():
    """Returns today's AI usage for the current user in their browser timezone."""
    usage = get_ai_usage_today(current_user.id)
    return jsonify(usage)

@app.route('/api/xp_status')
@login_required
def xp_status():
    """Returns current XP, level, streak for sidebar/dashboard live display"""
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
    return jsonify({
        'xp': xp,
        'level': level_info['level'],
        'badge': level_info['badge'],
        'icon': level_info['icon'],
        'next_xp': level_info['next_xp'],
        'progress_pct': progress_pct,
        'streak': streak
    })