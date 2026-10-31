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

@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': 'Too many requests. Please slow down and try again later.'}), 429
    return render_template('429.html'), 429


@app.errorhandler(413)
def handle_file_too_large(e):
    limit_mb = int(app.config.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024) // (1024 * 1024))
    message = (f'File is too large. Maximum allowed size is {limit_mb} MB. '
               'Tip: remove folders like node_modules, venv, .git or build files from your zip and try again.')
    accept = request.headers.get('Accept', '')
    wants_json = (request.is_json
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or request.path.startswith('/profile/save')
                  or ('application/json' in accept and 'text/html' not in accept))
    if wants_json:
        return jsonify({'success': False, 'error': message}), 413
    flash(message, 'error')
    # Go back to the page the user came from, but only if it is on this same site.
    referrer = request.referrer
    if referrer and urlparse(referrer).netloc == request.host:
        return redirect(referrer)
    return redirect(url_for('main_page'))


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


@app.before_request
def auto_update_streak():
    """Runs before every request — ensures streak is updated even without re-login"""
    if current_user.is_authenticated:
        today_local = user_today_key(current_user.id)
        # Store last checked date in session to avoid hitting DB on every single request
        last_checked = session.get('streak_checked_date')
        if last_checked != today_local:
            update_streak(current_user.id)
            session['streak_checked_date'] = today_local