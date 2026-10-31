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


app.jinja_env.globals.update(utc_iso=utc_iso)


import routes


def repair_experience_years():
    """One-time self-heal: fixes any stored experience_years that is negative, > 50,
    decimal or garbage (e.g. the -1 bug). Safe to run on every start."""
    try:
        from services.utils import safe_experience_years
        fixed = 0
        for u in users_collection.find({'experience_years': {'$exists': True}}, {'experience_years': 1}):
            good = str(safe_experience_years(u.get('experience_years')))
            if u.get('experience_years') != good:
                users_collection.update_one({'_id': u['_id']}, {'$set': {'experience_years': good}})
                fixed += 1
        if fixed:
            print(f"Repaired experience_years for {fixed} user(s).")
    except Exception as e:
        print(f"experience_years repair skipped: {e}")


def repair_social_links():
    """Blanks any previously stored social link that is not a real http(s) link on its own site."""
    try:
        from services.utils import clean_social_url
        fields = {f'{p}_url': p for p in ('linkedin', 'github', 'instagram', 'facebook')}
        fixed = 0
        for u in users_collection.find({'$or': [{f: {'$exists': True, '$ne': ''}} for f in fields]}, dict.fromkeys(fields, 1)):
            changes = {}
            for field, platform in fields.items():
                old = u.get(field)
                if old:
                    good, err = clean_social_url(old, platform)
                    new = '' if err else good
                    if new != old:
                        changes[field] = new
            if changes:
                users_collection.update_one({'_id': u['_id']}, {'$set': changes})
                fixed += 1
        if fixed:
            print(f"Cleaned unsafe social links for {fixed} user(s).")
    except Exception as e:
        print(f"social link repair skipped: {e}")


repair_experience_years()
repair_social_links()


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'profile_pics'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_img'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_assets'), exist_ok=True)
    print(f"Ensured upload folder exists at: {app.config['UPLOAD_FOLDER']}")
    try:
        app.run(debug=False)
    finally:
        scheduler.shutdown()
        print("Scheduler shut down cleanly.")