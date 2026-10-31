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

from services.roadmap_context import build_learner_context
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

_ALLOWED_RESOURCE_DOMAINS = {
    'youtube.com', 'www.youtube.com',
    'udemy.com', 'www.udemy.com',
    'coursera.org', 'www.coursera.org',
    'edx.org', 'www.edx.org',
    'linkedin.com', 'www.linkedin.com',
    'pluralsight.com', 'www.pluralsight.com',
    'google.com', 'www.google.com',
}
@app.route('/roadmap_generator', methods=['GET', 'POST'])
@login_required
@limiter.limit("15 per hour", key_func=get_user_key)      # ← UPDATED: per-account key, VPN-proof
def roadmap_generator():
    roadmap_data = None
    goal = ""

    # Fetch XP/streak for sidebar — same logic as profile route
    _u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1})
    _xp = _u.get('xp', 0) if _u else 0
    _streak = _u.get('streak_count', 0) if _u else 0
    _level_info = get_level_info(_xp)
    _progress_pct = int((_xp / _level_info['next_xp']) * 100) if _level_info['next_xp'] != "Max" else 100
    _sidebar = dict(xp=_xp, level_info=_level_info, streak=_streak, progress_pct=_progress_pct)

    # Which parts of the user's data may be shared with the AI for this request.
    # Form posts: a ticked checkbox sends the field, an unticked one doesn't.
    # Dashboard shortcut links (?goal=...) personalise by default; add &personalize=0 to switch it off.
    if request.method == 'POST':
        _prefs = {k: request.form.get(k) is not None for k in ('use_profile', 'use_resume', 'use_projects')}
    else:
        _on = request.args.get('personalize', '1') != '0'
        _prefs = {'use_profile': _on, 'use_resume': _on, 'use_projects': _on}

    if request.method == 'POST':
        goal = request.form.get('goal', '').strip()
        if not goal:
            flash("Please enter a goal for your roadmap.", "error")
            return render_template('roadmap_generator.html', goal=goal, **_sidebar, **_prefs)
        check_limit_now = True

    else:
        goal_from_url = request.args.get('goal', '').strip()
        check_limit_now = bool(goal_from_url)
        if goal_from_url:
            goal = goal_from_url
            flash(f"Generating roadmap for '{goal}'...", 'info')

    if goal and check_limit_now:
        # ── AI DAILY CAP CHECK (browser timezone) ───────────── applies to
        # BOTH POST (form submit) and GET-with-?goal= (e.g. a dashboard
        # shortcut link) — previously this only ran for POST, so repeatedly
        # visiting /roadmap_generator?goal=X bypassed the daily cap entirely.
        allowed, used, limit = check_ai_daily_limit(current_user.id, 'roadmap', limit=10)
        if not allowed:
            flash(f'⚠️ You have used all {limit} roadmap generations for today. Resets in {time_until_reset(current_user.id)} (your local midnight). Come back tomorrow!', 'error')
            return render_template('roadmap_generator.html', goal=goal, **_sidebar, **_prefs)

    if goal:
        try:
            print(f"Calling Groq AI with FINAL prompt for '{goal}'...")
            learner_context, personalization_used = '', None
            if any(_prefs.values()):
                try:
                    learner_context, personalization_used = build_learner_context(
                        users_collection, projects_collection, current_user.id,
                        use_profile=_prefs['use_profile'], use_resume=_prefs['use_resume'],
                        use_projects=_prefs['use_projects'])
                except Exception as e_ctx:          # never let personalisation break roadmap generation
                    print(f"Learner context skipped: {e_ctx}")
                    learner_context, personalization_used = '', None
                if not learner_context:
                    personalization_used = {'empty': True}
            roadmap_data = generate_roadmap_with_ai(goal, learner_context or None)
            if roadmap_data and isinstance(roadmap_data, dict) and isinstance(roadmap_data.get('stages'), list):
                for stage in roadmap_data.get("stages", []):
                    learning_modules = stage.get("learning_modules", [])
                    if isinstance(learning_modules, list):
                        for module in learning_modules:
                             resources = module.get("resources", [])
                             if not isinstance(resources, list): continue
                             for resource in resources:
                                if isinstance(resource, dict) and resource.get("type") == "Free YouTube Playlist":
                                    query = resource.get("youtube_search_query", goal)
                                    try:
                                        url, title = find_youtube_playlist(query)
                                        resource["url"] = url if url else "#"
                                        resource["title"] = title if title else f"Playlist for: {query}"
                                    except Exception as e_yt:
                                        print(f"Error finding YouTube playlist for '{query}': {e_yt}")
                                        resource["url"] = "#"; resource["title"] = f"Error finding playlist"
                    # Paid course link — the model gives us a title/provider, never a
                    # real URL (it would just be hallucinated), so we build a search
                    # link on the named platform (or Google, if unrecognized).
                    paid_course = stage.get("paid_course_resource")
                    if isinstance(paid_course, dict) and paid_course.get("title"):
                        try:
                            paid_course["url"] = get_paid_course_link(paid_course.get("title", ""), paid_course.get("provider", ""))
                        except Exception as e_pc:
                            print(f"Error building paid course link: {e_pc}")
                            paid_course["url"] = "#"
                return render_template('roadmap_generator.html', roadmap_data=roadmap_data, goal=goal,
                                       personalization_used=personalization_used, **_sidebar, **_prefs)
            else:
                print(f"AI response invalid or missing stages for goal '{goal}'. Response: {roadmap_data}")
                flash("Sorry, the AI response was incomplete or in an unexpected format. Please try again.", "error")
        except Exception as e_ai:
            print(f"Error during roadmap generation or processing for '{goal}': {e_ai}")
            flash("Sorry, something went wrong while generating your roadmap. Please try again in a moment.", "error")
        return render_template('roadmap_generator.html', goal=goal, **_sidebar, **_prefs)
    return render_template('roadmap_generator.html', **_sidebar, **_prefs)


def _is_safe_resource_url(url):
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme == 'https' and parsed.netloc in _ALLOWED_RESOURCE_DOMAINS


def sanitize_roadmap_urls(roadmap_data):
    """Defense-in-depth for /save_roadmap: roadmap_content arrives as a raw
    JSON blob from a hidden form field, so a tampered request could smuggle a
    non-http(s) or unexpected-domain URL (e.g. javascript:...) into a
    resource/paid_course_resource 'url' field — which later renders inside an
    <a href="..."> in view_roadmap.html. Strips any URL that isn't https://
    on one of our known resource domains, replacing it with '#'."""
    if not isinstance(roadmap_data, dict):
        return roadmap_data
    for stage in roadmap_data.get('stages', []) or []:
        if not isinstance(stage, dict):
            continue
        for module in stage.get('learning_modules', []) or []:
            if not isinstance(module, dict):
                continue
            for resource in module.get('resources', []) or []:
                if isinstance(resource, dict) and not _is_safe_resource_url(resource.get('url')):
                    resource['url'] = '#'
        paid_course = stage.get('paid_course_resource')
        if isinstance(paid_course, dict) and not _is_safe_resource_url(paid_course.get('url')):
            paid_course['url'] = '#'
    return roadmap_data


@app.route('/save_roadmap', methods=['POST'])
@login_required
def save_roadmap():
    goal = request.form.get('goal')
    roadmap_content_str = request.form.get('roadmap_content')
    if goal and roadmap_content_str:
        try:
            roadmap_data = json.loads(roadmap_content_str)
            if isinstance(roadmap_data, dict) and 'stages' in roadmap_data:
                roadmap_data = sanitize_roadmap_urls(roadmap_data)
                roadmaps_collection.insert_one({
                    'user_id': ObjectId(current_user.id),
                    'goal': goal,
                    'roadmap_content': roadmap_data,
                    'created_at': now_utc(),
                    'last_activity': now_utc(),
                    'reminder_stage': 0
                })
                flash('Roadmap saved successfully!', 'success')
                return redirect(url_for('my_roadmaps'))
            else:
                flash('Invalid roadmap data format received from the form.', 'error')
        except json.JSONDecodeError:
             print("Error decoding roadmap JSON from form.")
             flash('Internal error processing roadmap data. Could not save.', 'error')
        except Exception as e:
             print(f"Error saving roadmap to DB: {e}")
             flash('An unexpected error occurred while saving the roadmap.', 'error')
    else:
        flash('Could not save roadmap. Goal or content was missing from the request.', 'error')
    return redirect(url_for('roadmap_generator'))


@app.route('/my_roadmaps')
@login_required
def my_roadmaps():
    try:
        user_roadmaps = list(roadmaps_collection.find({'user_id': ObjectId(current_user.id)}).sort('created_at', -1))
        _u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1}) or {}
        xp = _u.get('xp', 0); level_info = get_level_info(xp)
        streak = _u.get('streak_count', 0)
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
        return render_template('my_roadmaps.html', roadmaps=user_roadmaps, xp=xp, level_info=level_info, streak=streak, progress_pct=progress_pct)
    except Exception as e:
        print(f"Error fetching roadmaps for user {current_user.id}: {e}")
        flash("Could not load your saved roadmaps.", "error")
        _fl = get_level_info(0)
        return render_template('my_roadmaps.html', roadmaps=[], xp=0, level_info=_fl, streak=0, progress_pct=0)


@app.route('/roadmap/delete/<roadmap_id>', methods=['POST'])
@login_required
def delete_roadmap(roadmap_id):
    """Delete a saved roadmap and deduct XP for completed stages"""
    try:
        obj_id = ObjectId(roadmap_id)
        roadmap = roadmaps_collection.find_one({'_id': obj_id, 'user_id': ObjectId(current_user.id)})
        if not roadmap:
            return jsonify({'success': False, 'error': 'Roadmap not found'}), 404
        # Count completed stages to deduct XP fairly
        content = roadmap.get('roadmap_content', {})
        if isinstance(content, str):
            import json as _json
            try: content = _json.loads(content)
            except: content = {}
        stages = content.get('stages', []) if isinstance(content, dict) else []
        completed_stages = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
        is_pre = roadmap.get('pre_gamification', False)
        roadmaps_collection.delete_one({'_id': obj_id})
        deducted = 0
        if not is_pre and completed_stages > 0:
            deduct_xp(current_user.id, completed_stages * 5, f"Deleted roadmap ({completed_stages} completed stages)")
            deducted = completed_stages * 5
        return jsonify({'success': True, 'deducted': deducted})
    except Exception as e:
        print(f"Delete roadmap error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/roadmap/<roadmap_id>')
@login_required
def view_roadmap(roadmap_id):
    try:
        obj_id = ObjectId(roadmap_id)
    except Exception:
        flash("Invalid roadmap ID format.", "error"); return redirect(url_for('my_roadmaps'))
    roadmap = roadmaps_collection.find_one({'_id': obj_id})
    if not roadmap or str(roadmap.get('user_id')) != current_user.id:
        flash("Roadmap not found or permission denied.", "error"); return redirect(url_for('my_roadmaps'))
    roadmaps_collection.update_one(
        {'_id': obj_id},
        {'$set': {'last_activity': now_utc(), 'reminder_stage': 0}}
    )
    content = roadmap.get('roadmap_content')
    if isinstance(content, str):
        try: content = json.loads(content)
        except Exception: print("Failed to parse roadmap_content string"); content = {}
    if isinstance(content, str):
        try: content = json.loads(content)
        except: pass
    roadmap['roadmap_content'] = content if isinstance(content, dict) else {}
    progress_percentage = 0
    try:
        stages = roadmap['roadmap_content'].get('stages', [])
        if isinstance(stages, list) and len(stages) > 0:
            completed_count = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
            progress_percentage = (completed_count / len(stages)) * 100
    except Exception as e:
        print(f"Progress calculation error: {e}")
    return render_template('view_roadmap.html', roadmap=roadmap, progress_percentage=progress_percentage)


@app.route('/complete_stage/<roadmap_id>/<int:stage_index>')
@login_required
def complete_stage(roadmap_id, stage_index):
    try:
        obj_id = ObjectId(roadmap_id)
        roadmap = roadmaps_collection.find_one({'_id': obj_id, 'user_id': ObjectId(current_user.id)})
        if not roadmap: return redirect(url_for('my_roadmaps'))
        content = roadmap.get('roadmap_content')
        if isinstance(content, str): content = json.loads(content)
        if 0 <= stage_index < len(content['stages']):
            # Only award XP if stage not already completed (prevent re-clicking exploit)
            if not content['stages'][stage_index].get('completed'):
                content['stages'][stage_index]['completed'] = True
                roadmaps_collection.update_one(
                    {'_id': obj_id},
                    {'$set': {'roadmap_content': content, 'last_activity': now_utc(), 'reminder_stage': 0}}
                )
                add_xp(current_user.id, 5, "Completed Roadmap Stage")
                flash('Stage marked as complete! +5 XP 🎉', 'success')
            else:
                roadmaps_collection.update_one({'_id': obj_id}, {'$set': {'roadmap_content': content}})
                flash('Stage already completed.', 'info')
        return redirect(url_for('view_roadmap', roadmap_id=roadmap_id))
    except Exception as e:
        print(f"Update error: {e}"); return redirect(url_for('my_roadmaps'))