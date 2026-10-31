import json
import os
import random
import re
import requests
import secrets
import pytz
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import bleach
import regex as re_ext
from bs4 import BeautifulSoup
from bson.objectid import ObjectId
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from flask_limiter.errors import RateLimitExceeded
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import SignatureExpired, BadTimeSignature, BadSignature
from markupsafe import Markup
from pymongo import ReturnDocument
from werkzeug.utils import secure_filename

from ai_roadmap_generator import (configure_ai, generate_roadmap_with_ai, find_youtube_playlist,
                                  get_paid_course_link, GROQ_API_URL, GROQ_MODEL_FAST)
from models import User
from extensions import (app, limiter, bcrypt, client, db, s, login_manager, scheduler, UTC_TZ,
                        DEFAULT_USER_TZ, users_collection, roadmaps_collection, projects_collection,
                        commits_collection, messages_collection, communities_collection,
                        community_messages_collection, chat_history_collection, notifications_collection,
                        xp_history_collection, comments_collection, interview_sessions_collection,
                        deleted_accounts_collection)
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

@app.route('/')
def index():
    try:
        total_users = users_collection.count_documents({})
        total_projects = projects_collection.count_documents({})
        completed_project_ids = commits_collection.distinct("project_id")
        completed_projects = len(completed_project_ids)
        completion_percent = 0
        if total_projects > 0:
            completion_percent = int((completed_projects / total_projects) * 100)
        return render_template('index.html',total_users=total_users,total_projects=total_projects,completed_projects=completed_projects,completion_percent=completion_percent)
    except Exception as e:
        print(f"Index stats error: {e}")
        return render_template('index.html',total_users=0,total_projects=0,completed_projects=0,completion_percent=0)


@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not name or not email or not password or not username:
            flash("All fields are required.", "error")
            return redirect(url_for('signup'))
        import re as _re
        if not _re.match(r'^[a-z0-9_]{3,20}$', username):
            flash("Username must be 3-20 characters, letters/numbers/underscores only.", "error")
            return redirect(url_for('signup'))
        if users_collection.find_one({'username': username, 'pending_deletion': {'$ne': True}}):
            flash("That username is already taken. Try another.", "error")
            return redirect(url_for('signup'))
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for('signup'))
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash("Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol (@$!%*?&).", "error")
            return redirect(url_for('signup'))
        if users_collection.find_one({'email': email, 'pending_deletion': {'$ne': True}}):          # ← now correctly indented inside POST
            flash("An account with this email already exists. Try logging in.", "error")
            return redirect(url_for('login'))
        otp = random.randint(100000, 999999)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        session['temp_user_data'] = {'name': name, 'email': email, 'password': hashed_password, 'username': username}
        session['otp'] = otp
        session['otp_timestamp'] = datetime.utcnow().timestamp()
        success = send_email(
            email,
            'Your SkillBridge OTP Code',
            f'Your One-Time Password (OTP) for SkillBridge is: {otp}. It expires in 10 minutes.'
        )
        if success:
            flash('An OTP has been sent to your email. Please check your inbox (and spam folder).', 'success')
            return redirect(url_for('verify'))
        else:
            flash('Failed to send verification email. Please try again later.', 'error')
            session.pop('temp_user_data', None)
            session.pop('otp', None)
            session.pop('otp_timestamp', None)
            return redirect(url_for('signup'))
    return render_template('signup.html')


@app.route('/api/check_username')
def check_username():
    """Live username availability check — called as user types in signup"""
    username = request.args.get('username', '').strip().lower()
    if not username:
        return jsonify({'available': False, 'error': 'Empty'})
    import re as _re
    if not _re.match(r'^[a-z0-9_]{3,20}$', username):
        return jsonify({'available': False, 'error': 'Username must be 3-20 chars, letters/numbers/underscores only'})
    existing = users_collection.find_one({'username': username, 'pending_deletion': {'$ne': True}}, {'_id': 1})
    return jsonify({'available': existing is None})


@app.route('/verify', methods=['GET', 'POST'])
@limiter.limit("10 per 15 minutes")                       # ← ADDED
def verify():
    if 'temp_user_data' not in session or 'otp' not in session or 'otp_timestamp' not in session:
        flash('Verification session expired or invalid. Please sign up again.', 'warning')
        return redirect(url_for('signup'))
    otp_age = datetime.utcnow().timestamp() - session.get('otp_timestamp', 0)
    if otp_age > 600:
        session.pop('temp_user_data', None); session.pop('otp', None); session.pop('otp_timestamp', None)
        flash('Your OTP has expired. Please sign up again.', 'error')
        return redirect(url_for('signup'))
    if request.method == 'POST':
        user_otp_str = request.form.get('otp')
        try:
            user_otp = int(user_otp_str)
            stored_otp = session.get('otp')
            if stored_otp is not None and user_otp == stored_otp:
                user_data = session.pop('temp_user_data', None)
                session.pop('otp', None); session.pop('otp_timestamp', None)
                if user_data:
                    user_data['profile_pic'] = 'default.jpg'
                    user_data['github_url'] = ''; user_data['linkedin_url'] = ''
                    user_data['known_skills'] = []; user_data['learning_skills'] = []
                    user_data['xp'] = 0  # INIT XP on account creation
                    user_data['username'] = user_data.get('username', '')
                    user_data['created_at'] = now_ist()
                    users_collection.insert_one(user_data)
                    flash('Email verified successfully! Please log in.', 'success'); return redirect(url_for('login'))
                else:
                    flash('Session error retrieving user data. Please sign up again.', 'error'); return redirect(url_for('signup'))
            else:
                flash('Invalid OTP. Please try again.', 'error'); return redirect(url_for('verify'))
        except (ValueError, TypeError):
             flash('Invalid OTP format. Please enter numbers only.', 'error'); return redirect(url_for('verify'))
    remaining_time = max(0, 600 - int(otp_age))
    return render_template('verify.html', remaining_minutes=remaining_time // 60, remaining_seconds=remaining_time % 60)


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per hour", key_func=get_login_key)     # ← UPDATED: IP+email key
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        remember = bool(request.form.get('remember'))
        if not email or not password:
             flash("Email and password are required.", "error"); return redirect(url_for('login'))
        user_data = users_collection.find_one({'email': email})
        if user_data and bcrypt.check_password_hash(user_data.get('password', ''), password):
            if user_data.get('is_banned'):
                flash("Your account has been suspended. Contact support if you think this is a mistake.", "error")
                return redirect(url_for('login'))
            if user_data.get('pending_deletion'):
                flash("This account is scheduled for deletion and can no longer be accessed.", "error")
                return redirect(url_for('login'))
            user = User(user_data)
            login_user(user, remember=remember)
            update_streak(str(user_data['_id']))  # STREAK TRACKING on every login
            next_page = request.args.get('next')
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(next_page or url_for('main_page'))
        else:
            flash("Invalid email or password. Please try again.", "error"); return redirect(url_for('login'))
    return render_template('login.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if email and users_collection.find_one({'email': email}, {'_id': 1}):
            token = s.dumps(email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            success = send_email(
                email,
                'Password Reset Request for SkillBridge',
                f'Click the following link to reset your password: {reset_url}\n\nThis link will expire in 1 hour.'
            )
            if success:
                flash('A password reset link has been sent to your email.', 'success')
            else:
                flash('Could not send email. Please check your email address or try again later.', 'error')
            return redirect(url_for('login'))
        flash('If an account exists for that email, a password reset link has been sent.', 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
@limiter.limit("5 per hour")                              # ← ADDED
def reset_password(token):
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=3600)
    except (SignatureExpired, BadTimeSignature):
        flash('The password reset link is invalid or has expired.', 'error'); return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if password != confirm_password:
            flash('Passwords do not match.', 'error'); return render_template('reset_password.html', token=token)
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash('Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol.', 'error'); return render_template('reset_password.html', token=token)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        result = users_collection.update_one({'email': email}, {'$set': {'password': hashed_password}})
        if result.modified_count > 0:
            flash('Your password has been updated successfully! Please log in with your new password.', 'success'); return redirect(url_for('login'))
        else:
            flash('Could not update password. Please try again.', 'error'); return redirect(url_for('forgot_password'))
    return render_template('reset_password.html', token=token)


@app.route('/mainpage')
@login_required
def main_page():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    user_name = current_user.name
    roadmap_count = roadmaps_collection.count_documents({'user_id': ObjectId(current_user.id)})
    project_count = projects_collection.count_documents({'created_by_id': ObjectId(current_user.id)})
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
    return render_template('mainpage.html',
        user_name=user_name,
        roadmap_count=roadmap_count,
        project_count=project_count,
        xp=xp,
        level_info=level_info,
        streak=streak,
        progress_pct=progress_pct
    )


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('index'))

