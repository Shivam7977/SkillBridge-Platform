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

@app.route('/resume_builder')
@login_required
def resume_builder():
    try:
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        # FIX: Only completed projects in resume
        user_projects = list(projects_collection.find({'created_by_id': ObjectId(current_user.id), 'is_completed': True}).sort('created_at', -1))
        certificates = user_data.get("certificates", []) if user_data else []
        github_repos = user_data.get("github_repos", []) if user_data else []
        github_langs = user_data.get("github_langs", []) if user_data else []
        work_experience = user_data.get('work_experience', [])
        education = user_data.get('education', [])
        # ── Sidebar XP / Level / Streak (same as mainpage) ──
        xp = user_data.get('xp', 0) if user_data else 0
        level_info = get_level_info(xp)
        streak = user_data.get('streak_count', 0) if user_data else 0
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
        return render_template("resume_builder.html",
            user=user_data,
            projects=user_projects,
            certificates=certificates,
            github_repos=github_repos,
            github_langs=github_langs,
            work_experience=work_experience,
            education=education,
            xp=xp,
            level_info=level_info,
            streak=streak,
            progress_pct=progress_pct,
        )
    except Exception as e:
        print(f"Error loading resume builder: {e}")
        flash("Could not load your resume data.", "error")
        return redirect(url_for('main_page'))


@app.route('/resume_pdf')
@login_required
def resume_pdf():
    try:
        template_num = int(request.args.get('t', 1))
        if template_num < 1 or template_num > 18: template_num = 1
    except (ValueError, TypeError):
        template_num = 1
    template_name = TEMPLATE_NAMES.get(template_num, "Classic Tech")
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}) or {}
    # FIX: Only completed projects in PDF
    user_projects = list(projects_collection.find({'created_by_id': ObjectId(current_user.id), 'is_completed': True}).sort('created_at', -1))
    user_data['known_skills'] = parse_skills(user_data.get('known_skills'))
    user_data['learning_skills'] = parse_skills(user_data.get('learning_skills'))
    for p in user_projects:
        p['skills_needed'] = parse_skills(p.get('skills_needed'))
    work_experience = user_data.get('work_experience', [])
    education = user_data.get('education', [])
    return render_template('resume_pdf.html', user=user_data, projects=user_projects, template_num=template_num, template_name=template_name, work_experience=work_experience, education=education)


@app.route('/portfolio_builder')
@login_required
def portfolio_builder():
    try:
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}) or {}
        # FIX: Only completed projects in portfolio
        user_projects = list(projects_collection.find({'created_by_id': ObjectId(current_user.id), 'is_completed': True}).sort('created_at', -1))
        user_data['known_skills'] = parse_skills(user_data.get('known_skills'))
        user_data['learning_skills'] = parse_skills(user_data.get('learning_skills'))
        for p in user_projects:
            p['skills_needed'] = parse_skills(p.get('skills_needed'))
        certificates = user_data.get('certificates', [])
        github_repos = user_data.get('github_repos', [])
        github_langs = user_data.get('github_langs', [])
        work_experience = user_data.get('work_experience', [])
        education = user_data.get('education', [])
        # FIX: Pass XP, level_info, streak to template
        xp = user_data.get('xp', 0)
        level_info = get_level_info(xp)
        streak = user_data.get('streak_count', 0)
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
        return render_template('portfolio_builder.html',
            user=user_data,
            projects=user_projects,
            certificates=certificates,
            github_repos=github_repos,
            github_langs=github_langs,
            work_experience=work_experience,
            education=education,
            xp=xp,
            level_info=level_info,
            streak=streak,
            progress_pct=progress_pct
        )
    except Exception as e:
        print(f"Error loading portfolio builder: {e}")
        flash("Could not load your portfolio data.", "error")
        return redirect(url_for('main_page'))


@app.route('/portfolio_assets/<path:filename>')
def serve_portfolio_assets(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename: return "Invalid filename", 400
    try:
        directory = os.path.join(app.root_path, 'static', 'portfolio_assets')
        return send_from_directory(directory, safe_filename)
    except FileNotFoundError:
         print(f"Portfolio asset not found: {filename}"); return "Asset not found", 404


@app.route('/portfolio_img/<path:filename>')
def serve_portfolio_images(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename: return "Invalid filename", 400
    try:
        directory = os.path.join(app.root_path, 'static', 'portfolio_img')
        return send_from_directory(directory, safe_filename)
    except FileNotFoundError:
        print(f"Portfolio image not found: {filename}"); return "Image not found", 404
    except Exception as e:
        print(f"Error serving portfolio image {filename}: {e}"); return "Error serving image", 500


@app.route("/api/templates")
@login_required
def list_templates():
    templates = []
    template_html_dir = os.path.join(app.root_path, 'templates', 'portfolio_templates')
    static_img_dir = os.path.join(app.root_path, 'static', 'portfolio_img')
    if not os.path.isdir(template_html_dir):
        print(f"Error: Portfolio templates directory not found at {template_html_dir}")
        return jsonify({"error": "Portfolio templates directory not found"}), 500
    try:
        for filename in os.listdir(template_html_dir):
            if filename.endswith(".html"):
                template_id = filename.replace('.html', '$set')
                safe_template_id = secure_filename(template_id)
                thumb_filename = f"{safe_template_id}_thumb.png"
                thumb_path = os.path.join(static_img_dir, thumb_filename)
                if os.path.isfile(thumb_path):
                    templates.append({"id": template_id, "name": template_id.replace('_', ' ').title(), "thumbnail": url_for('serve_portfolio_images', filename=thumb_filename)})
                else:
                     print(f"Warning: Thumbnail not found for template {template_id} at {thumb_path}")
        return jsonify(templates)
    except Exception as e:
        print(f"Error listing portfolio templates: {e}"); return jsonify({"error": "Failed to list templates"}), 500


@app.route("/api/template/<template_id>")
@login_required
def get_template_details(template_id):
    safe_template_id = secure_filename(template_id)
    try:
        filepath = os.path.join(app.root_path, 'templates', 'portfolio_templates', f"{safe_template_id}.html")
        if not os.path.isfile(filepath): return jsonify({"error": "Template not found"}), 404
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user_data: return jsonify({"error": "User data not found"}), 500
        # FIX: Only completed projects in template details
        user_projects = list(projects_collection.find({'created_by_id': ObjectId(current_user.id), 'is_completed': True}))
        live_portfolio_data = {
            "header_name": user_data.get('name', 'Your Name'), "header_title": user_data.get('title', 'Web Developer'),
            "header_location": user_data.get('location', 'Your Location'), "contact_email": user_data.get('email', ''),
            "contact_linkedin": user_data.get('linkedin_url', ''), "contact_github": user_data.get('github_url', ''),
            "about_description": user_data.get('about_me', "A passionate developer building the future."),
            "edu_college": user_data.get('education_college', 'Your University'), "edu_degree": user_data.get('education_degree', 'Your Degree'),
            "skills_list": ", ".join(user_data.get('known_skills', []))
        }
        formatted_projects = []
        for p in user_projects:
            formatted_projects.append({"title": p.get("title", "Untitled Project"), "description": p.get("description", "Project description goes here."), "skills_needed": p.get("skills_needed", [])})
        defaults_flat = flatten_data(live_portfolio_data)
        soup = BeautifulSoup(content, 'html.parser')
        form_fields = []
        for img_tag in soup.find_all('img'):
            src = img_tag.get('src')
            if src and not src.startswith(('http', '/', 'data:', '{{')):
                img_tag['src'] = url_for('serve_portfolio_assets', filename=src)
        for link_tag in soup.find_all('link', rel='stylesheet'):
            href = link_tag.get('href')
            if href and not href.startswith(('http', '/', 'data:')):
                href_filename = os.path.basename(href)
                link_tag['href'] = url_for('serve_portfolio_assets', filename=href_filename)
        for script_tag in soup.find_all('script', src=True):
            src = script_tag.get('src')
            if src and not src.startswith(('http', '/', 'data:')):
                script_tag['src'] = url_for('serve_portfolio_assets', filename=src)
        script_tag_pattern = re_ext.compile(r'const\s+portfolioData\s*=')
        script_to_remove = soup.find('script', string=script_tag_pattern)
        if script_to_remove: script_to_remove.decompose()
        for element in soup.find_all(attrs={'data-content': True}):
            field_id = element['data-content']
            target_attr = 'innerText'
            if element.name == 'a': target_attr = 'href'
            elif element.name == 'img': target_attr = 'src'
            if target_attr == 'innerText': element['contenteditable'] = 'true'
            val = live_portfolio_data.get(field_id, "")
            form_fields.append({"id": field_id, "targetAttribute": target_attr, "defaultValue": str(val)})
            element['data-binding'] = field_id
            del element['data-content']
        return jsonify({"config": {"fields": form_fields}, "list_data": {"projects": formatted_projects}, "htmlContent": str(soup)})
    except Exception as e:
        print(f"Error serving template: {e}"); return jsonify({"error": str(e)}), 500

