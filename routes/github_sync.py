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


@app.route('/api/github/fetch', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def github_fetch():
    try:
        data = request.get_json()
        username = data.get('username', '').strip().lstrip('@')

        if not username:
            return jsonify({'success': False, 'error': 'Username is required'}), 400

        token = os.getenv("GITHUB_TOKEN")
        headers = {'User-Agent': 'SkillBridge-App/1.0'}
        if token:
            headers['Authorization'] = f'token {token}'

        profile_res = requests.get(f'https://api.github.com/users/{username}', headers=headers, timeout=10)

        if profile_res.status_code == 404:
            return jsonify({'success': False, 'error': f'GitHub user "{username}" not found'}), 404

        profile = profile_res.json()

        repos_res = requests.get(f'https://api.github.com/users/{username}/repos?per_page=100&sort=pushed', headers=headers, timeout=10)
        repos_raw = repos_res.json()

        repos_raw = [r for r in repos_raw if not r.get('fork')]
        repos_raw.sort(key=lambda r: r.get('stargazers_count', 0), reverse=True)
        top_repos = repos_raw[:6]

        lang_count = {}
        for r in repos_raw[:20]:
            lang = r.get('language')
            if lang:
                lang_count[lang] = lang_count.get(lang, 0) + 1

        top_langs = sorted(lang_count, key=lang_count.get, reverse=True)[:8]

        repos_out = [{
            'id':          r['id'],
            'name':        r['name'],
            'description': r.get('description') or 'No description',
            'url':         r['html_url'],
            'stars':       r.get('stargazers_count', 0),
            'forks':       r.get('forks_count', 0),
            'language':    r.get('language') or 'Mixed',
            'updated':     (r.get('pushed_at') or 'Unknown')[:10]
        } for r in top_repos]

        result = {
            'username':     profile.get('login', ''),
            'name':         profile.get('name') or username,
            'avatar':       profile.get('avatar_url', ''),
            'bio':          profile.get('bio', ''),
            'github_url':   profile.get('html_url', ''),
            'followers':    profile.get('followers', 0),
            'following':    profile.get('following', 0),
            'public_repos': profile.get('public_repos', 0),
            'repos':        repos_out,
            'top_languages': top_langs
        }
        return jsonify({'success': True, 'data': result})

    except Exception as e:
        print(f"GitHub fetch error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/github/sync', methods=['POST'])
@login_required
def github_sync():
    try:
        data = request.get_json()
        github_data = data.get('github_data', {})
        merge_skills = data.get('merge_skills', False)
        import_repos = data.get('import_repos', False)
        username = github_data.get('username', '')

        if not username:
            return jsonify({'success': False, 'error': 'No GitHub data to save'}), 400

        update_fields = {
            'github_username':    username,
            'github_avatar':      github_data.get('avatar', ''),
            'github_url':         github_data.get('github_url', ''),
            'github_repos':       github_data.get('repos', []),
            'github_langs':       github_data.get('top_languages', []),
            'github_followers':   github_data.get('followers', 0),
            'github_public_repos': github_data.get('public_repos', 0),
            'github_synced_at':   now_ist()
        }

        # Merge languages into skills if checkbox checked
        if merge_skills:
            langs = github_data.get('top_languages', [])
            if langs:
                user = users_collection.find_one({'_id': ObjectId(current_user.id)})
                existing = user.get('skills_known', '')
                existing_list = [s.strip() for s in existing.split(',') if s.strip()]
                merged = list(dict.fromkeys(existing_list + langs))  # no duplicates
                update_fields['skills_known'] = ', '.join(merged)

        # Import top repos as projects if checkbox checked
        if import_repos:
            repos = github_data.get('repos', [])
            for repo in repos[:6]:
                existing = projects_collection.find_one({
                    'created_by_id': ObjectId(current_user.id),
                    '$or': [
                        {'github_repo_id': repo.get('id')},
                        {'title': repo.get('name', ''), 'source': 'github'}
                    ]
                })
                if not existing:
                    projects_collection.insert_one({
                        'created_by_id': ObjectId(current_user.id),
                        'title': repo.get('name', 'Untitled'),
                        'description': repo.get('description', 'Imported from GitHub'),
                        'tech_stack': repo.get('language', ''),
                        'github_link': repo.get('url', ''),
                        'github_repo_id': repo.get('id'),
                        'source': 'github',
                        'status': 'completed',
                        'created_at': now_ist()
                    })
                    add_xp(current_user.id, 10, "Project imported from GitHub")

        users_collection.update_one(
            {'_id': ObjectId(current_user.id)},
            {'$set': update_fields}
        )

        # One-time XP reward for connecting GitHub
        user = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user.get('github_reward_claimed'):
            add_xp(current_user.id, 25, "Connected GitHub profile")
            users_collection.update_one(
                {'_id': ObjectId(current_user.id)},
                {'$set': {'github_reward_claimed': True}}
            )

        return jsonify({'success': True})
    except Exception as e:
        print(f"GitHub sync error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/github/disconnect', methods=['POST'])
@login_required
def github_disconnect():
    try:
        users_collection.update_one(
            {'_id': ObjectId(current_user.id)},
            {'$unset': {
                'github_username': '',
                'github_avatar': '',
                'github_repos': '',
                'github_langs': '',
                'github_followers': '',
                'github_public_repos': '',
                'github_synced_at': ''
            }}
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

