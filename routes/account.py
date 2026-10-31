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


@app.route('/settings')
@login_required
def settings():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # Gamification data for settings page
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)

    # Progress bar percentage
    if level_info['next_xp'] == "Max":
        progress_pct = 100
    else:
        progress_pct = int((xp / level_info['next_xp']) * 100)

    # Member since
    member_since = to_ist(user_data.get('created_at')).strftime('%B %Y')
    streak = user_data.get('streak_count', 1)

    ai_usage = get_ai_usage_today(current_user.id)  # ← AI daily cap data

    # Only relevant if this account is an admin — hides the self-delete button
    # when deleting it would lock everyone out of the admin panel
    is_sole_admin = is_sole_active_admin(ObjectId(current_user.id))

    return render_template(
        'settings.html',
        user=user_data,
        xp=xp,
        level_info=level_info,
        progress_pct=progress_pct,
        member_since=member_since,
        streak=streak,
        ai_usage=ai_usage,
        email_reminders_enabled=user_data.get('email_reminders_enabled', True),
        username=user_data.get('username', ''),
        is_sole_admin=is_sole_admin
    )

@app.route('/change_password', methods=['POST'])
@login_required
@limiter.limit("5 per hour")                              # ← ADDED
def change_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # Verify current password
    if not bcrypt.check_password_hash(user_data.get('password', ''), current_password):
        flash('Incorrect current password.', 'error')
        return redirect(url_for('settings'))

    # Check if new passwords match
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('settings'))

    # Validate new password strength
    password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
    if not password_pattern.match(new_password):
        flash('Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol.', 'error')
        return redirect(url_for('settings'))

    # Update password
    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'password': hashed_password}}
    )

    flash('Password updated successfully!', 'success')
    return redirect(url_for('settings'))

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    user_id = ObjectId(current_user.id)

    # Sole-admin lockout guard — this route is the regular Settings "delete my
    # account" flow, completely separate from the admin panel's ban/delete
    # buttons, so it needed its own check. If this account is the only admin,
    # trashing it would leave nobody who can log into /admin to restore it.
    if is_sole_active_admin(user_id):
        flash("You're the only active admin — deleting this account would lock everyone out of the admin panel. Promote another admin first.", "error")
        return redirect(url_for('settings'))

    purge_at = now_utc() + timedelta(days=30)
    users_collection.update_one({'_id': user_id}, {'$set': {
        'pending_deletion': True,
        'deletion_requested_at': now_utc(),
        'deletion_requested_by': 'Self',
        'purge_at': purge_at,
    }})
    sync_hidden_flag(user_id)

    logout_user()
    flash('Your account has been scheduled for deletion. It will be permanently removed in 30 days.', 'info')
    return redirect(url_for('index'))

