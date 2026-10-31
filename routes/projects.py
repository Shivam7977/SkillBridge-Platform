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

from recommendation_engine import get_recommended_projects
@app.route('/projects')
def projects():
    try:
        PER_PAGE = 10
        page = request.args.get('page', 1, type=int)
        recommended_projects = []
        profile_incomplete = False
        is_authenticated = current_user.is_authenticated

        # Total count of ALL projects for display
        not_hidden = {'hidden': {'$ne': True}}
        total = projects_collection.count_documents(not_hidden)

        if is_authenticated:
            recommended_projects = get_recommended_projects(current_user.id, users_collection, projects_collection)
            recommended_projects = [p for p in recommended_projects if not p.get('hidden')]
            rec_ids = [p['_id'] for p in recommended_projects]

            # other_projects = all projects NOT in recommended
            other_query = {'_id': {'$nin': rec_ids}, **not_hidden} if rec_ids else dict(not_hidden)
            other_total = projects_collection.count_documents(other_query)
            other_projects = list(projects_collection.find(other_query)
                .sort('created_at', -1)
                .skip((page - 1) * PER_PAGE)
                .limit(PER_PAGE))

            user_data = users_collection.find_one(
                {'_id': ObjectId(current_user.id)},
                {'known_skills': 1, 'learning_skills': 1}
            )
            if not user_data.get('known_skills') and not user_data.get('learning_skills'):
                profile_incomplete = True
        else:
            other_total = total
            other_projects = list(projects_collection.find(not_hidden)
                .sort('created_at', -1)
                .skip((page - 1) * PER_PAGE)
                .limit(PER_PAGE))

        total_pages = max(1, (other_total + PER_PAGE - 1) // PER_PAGE)
        has_more = page < total_pages

        # Pass user's bookmark IDs so template can show filled/empty bookmark icon
        user_bookmark_ids = set()
        if is_authenticated:
            u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'bookmarks': 1})
            user_bookmark_ids = {str(b) for b in u.get('bookmarks', [])} if u else set()

        # Attach comment counts — single aggregation for all visible projects
        all_visible = recommended_projects + other_projects
        all_visible_ids = [p['_id'] for p in all_visible if '_id' in p]
        cmt_agg = comments_collection.aggregate([
            {'$match': {'project_id': {'$in': all_visible_ids}, 'is_deleted': {'$ne': True}}},
            {'$group': {'_id': '$project_id', 'count': {'$sum': 1}}}
        ])
        cmt_counts = {str(doc['_id']): doc['count'] for doc in cmt_agg}

        for p in all_visible:
            p['comment_count'] = cmt_counts.get(str(p['_id']), 0)
            p['like_count'] = len(p.get('likes', []))
            p['view_count'] = p.get('views', 0)

        # XP / level / streak for navbar
        xp, level_info, streak, progress_pct = 0, get_level_info(0), 0, 0
        if is_authenticated:
            _u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1})
            if _u:
                xp = _u.get('xp', 0)
                level_info = get_level_info(xp)
                streak = _u.get('streak_count', 0)
                progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100

        return render_template('projects.html',
            recommended_projects=recommended_projects,
            other_projects=other_projects,
            profile_incomplete=profile_incomplete,
            is_authenticated=is_authenticated,
            page=page,
            total_pages=total_pages,
            total=total,
            other_total=other_total,
            has_more=has_more,
            user_bookmark_ids=user_bookmark_ids,
            xp=xp,
            level_info=level_info,
            streak=streak,
            progress_pct=progress_pct)
    except Exception as e:
        print(f"Error fetching community projects: {e}")
        flash("Could not load community projects at this time.", "error")
        _fallback_level = get_level_info(0)
        return render_template('projects.html', recommended_projects=[], other_projects=[],
            profile_incomplete=False, is_authenticated=current_user.is_authenticated,
            page=1, total_pages=1, total=0, other_total=0, has_more=False,
            xp=0, level_info=_fallback_level, streak=0, progress_pct=0)


@app.route('/api/projects')
def api_projects():
    """JSON endpoint for Load More button on community projects page"""
    try:
        PER_PAGE = 10
        page = request.args.get('page', 1, type=int)
        is_authenticated = current_user.is_authenticated

        not_hidden = {'hidden': {'$ne': True}}
        if is_authenticated:
            recommended_ids = [p['_id'] for p in get_recommended_projects(current_user.id, users_collection, projects_collection)]
            query = {'_id': {'$nin': recommended_ids}, **not_hidden}
        else:
            query = dict(not_hidden)

        total = projects_collection.count_documents(query)
        projects_list = list(projects_collection.find(query)
            .sort('created_at', -1)
            .skip((page - 1) * PER_PAGE)
            .limit(PER_PAGE))

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

        result = []
        for p in projects_list:
            result.append({
                '_id': str(p['_id']),
                'title': p.get('title', ''),
                'description': p.get('description', ''),
                'created_by_name': p.get('created_by_name', 'Unknown'),
                'created_by_id': str(p.get('created_by_id', '')),
                'skills_needed': p.get('skills_needed', []),
                'views': p.get('views', 0),
                'like_count': len(p.get('likes', [])),
            })

        return jsonify({
            'projects': result,
            'page': page,
            'total_pages': total_pages,
            'has_more': page < total_pages
        })
    except Exception as e:
        print(f"API projects error: {e}")
        return jsonify({'projects': [], 'has_more': False}), 500


@app.route('/my_projects')
@login_required
def my_projects():
    try:
        PER_PAGE = 10
        page = request.args.get('page', 1, type=int)
        query = {'created_by_id': ObjectId(current_user.id)}
        total = projects_collection.count_documents(query)
        my_projects_list = list(projects_collection.find(query)
            .sort('created_at', -1)
            .skip((page - 1) * PER_PAGE)
            .limit(PER_PAGE))

        # Single aggregation — comment counts for all projects at once
        project_ids = [p['_id'] for p in my_projects_list]
        comment_agg = comments_collection.aggregate([
            {'$match': {
                'project_id': {'$in': project_ids},
                'is_deleted': {'$ne': True}
            }},
            {'$group': {'_id': '$project_id', 'count': {'$sum': 1}}}
        ])
        comment_counts = {str(doc['_id']): doc['count'] for doc in comment_agg}

        for p in my_projects_list:
            pid = str(p['_id'])
            p['_id'] = pid
            p['like_count'] = len(p.get('likes', []))
            p['view_count'] = p.get('views', 0)
            p['comment_count'] = comment_counts.get(pid, 0)
            p['skills_needed'] = p.get('skills_needed', [])
            # Expose community_id as string so template can offer community-delete option
            raw_cid = p.get('community_id')
            p['community_id'] = str(raw_cid) if raw_cid else None

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

        # ── BOOKMARKED PROJECTS ──────────────────────────────
        user_data = users_collection.find_one(
            {'_id': ObjectId(current_user.id)}, {'bookmarks': 1, 'xp': 1, 'streak_count': 1}
        )
        bookmark_ids = user_data.get('bookmarks', []) if user_data else []
        bookmarked_projects = []
        if bookmark_ids:
            raw_bookmarks = list(projects_collection.find(
                {'_id': {'$in': bookmark_ids}}
            ).sort('created_at', -1))
            for p in raw_bookmarks:
                p['_id'] = str(p['_id'])
                p['created_by_id'] = str(p.get('created_by_id', ''))
                p['like_count'] = len(p.get('likes', []))
                p['view_count'] = p.get('views', 0)
                p['skills_needed'] = p.get('skills_needed', [])
                p['comment_count'] = p.get('comment_count', 0)
                bookmarked_projects.append(p)

        xp = user_data.get('xp', 0) if user_data else 0
        streak = user_data.get('streak_count', 0) if user_data else 0
        level_info = get_level_info(xp)
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100

        return render_template('my_projects.html',
            projects=my_projects_list,
            page=page,
            total_pages=total_pages,
            total=total,
            bookmarked_projects=bookmarked_projects,
            xp=xp,
            level_info=level_info,
            streak=streak,
            progress_pct=progress_pct)
    except Exception as e:
        print(f"Error fetching user projects for {current_user.id}: {e}")
        flash("Could not load your projects.", "error")
        xp = 0
        level_info = get_level_info(xp)
        progress_pct = 0
        return render_template('my_projects.html', projects=[], page=1, total_pages=1, total=0,
            bookmarked_projects=[], xp=xp, level_info=level_info, streak=0, progress_pct=progress_pct)


@app.route('/create_project', methods=['GET', 'POST'])
@login_required
@limiter.limit("20 per hour")                             # ← ADDED
def create_project():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        skills_str = request.form.get('skills', '')
        skills = [s.strip() for s in skills_str.split(',') if s.strip()]
        if not title or not description:
            flash("Title and Description are required.", "error")
            return render_template("create_project.html")
        project_id = projects_collection.insert_one({
            "title": title, "description": description, "skills_needed": skills,
            "created_by_id": ObjectId(current_user.id), "created_by_name": current_user.name,
            "is_completed": False,
            "created_at": now_ist()
        }).inserted_id

        # XP for creating a project (+10)
        add_xp(current_user.id, 10, "Created a Project")

        create_community = request.form.get('create_community')
        if create_community == "yes":
            primary = request.form.get('community_skill_primary', '').strip()
            if not primary:
                flash("Primary skill required for community.", "error")
                return render_template("create_project.html")
            secondary = request.form.get('community_skill_secondary', '').strip()
            tool = request.form.get('community_skill_tool', '').strip()
            domain = request.form.get('community_skill_domain', '').strip()
            optional = request.form.get('community_skill_optional', '').strip()
            visibility = request.form.get('community_visibility', 'public')
            skills_required = [s for s in [primary, secondary, tool, domain, optional] if s]
            community_id = communities_collection.insert_one({
                "project_id": project_id, "project_title": title, "skills_required": skills_required,
                "visibility": visibility, "owner_id": ObjectId(current_user.id), "owner_name": current_user.name,
                "members": [ObjectId(current_user.id)], "admins": [], "pending_requests": [],
                "created_at": now_ist()
            }).inserted_id
            projects_collection.update_one({"_id": project_id}, {"$set": {"community_id": community_id}})
            flash("Project and community created successfully! +10 XP 🎉", "success")
            return redirect(url_for("find_communities"))
        flash("Project created successfully! +10 XP 🎉", "success")
        return redirect(url_for("my_projects"))
    return render_template("create_project.html")


@app.route('/projects/toggle_complete/<project_id>', methods=['POST'])
@login_required
def toggle_complete(project_id):
    try:
        data = request.get_json()
        is_completed = data.get('is_completed', False)
        projects_collection.update_one(
            {'_id': ObjectId(project_id), 'created_by_id': ObjectId(current_user.id)},
            {'$set': {'is_completed': is_completed}}
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/projects/edit/<project_id>', methods=['POST'])
@login_required
def edit_project(project_id):
    try:
        data = request.get_json()
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        skills = [s.strip() for s in data.get('skills', '').split(',') if s.strip()]
        if not title or not description:
            return jsonify({'success': False, 'error': 'Title and description are required'}), 400
        projects_collection.update_one(
            {'_id': ObjectId(project_id), 'created_by_id': ObjectId(current_user.id)},
            {'$set': {'title': title, 'description': description, 'skills_needed': skills}}
        )
        return jsonify({'success': True, 'title': title, 'description': description, 'skills': skills})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/projects/delete/<project_id>', methods=['POST'])
@login_required
def delete_project(project_id):
    """Delete a project and deduct XP to prevent create-delete abuse.
    Optional: also delete linked community if delete_community=true in JSON body."""
    try:
        project = projects_collection.find_one(
            {'_id': ObjectId(project_id), 'created_by_id': ObjectId(current_user.id)}
        )
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        # Check if caller wants community deleted too
        data = request.get_json(silent=True) or {}
        also_delete_community = data.get('delete_community', False)

        # Delete linked community if requested and it exists
        community_deleted = False
        community_id = project.get('community_id')
        if also_delete_community and community_id:
            community = communities_collection.find_one({'_id': ObjectId(str(community_id))})
            if community:
                comm_title = community.get('project_title', 'a community')
                owner_id_obj = community.get('owner_id')
                # Notify all members (exclude owner — they initiated this)
                for mid in community.get('members', []):
                    if mid != owner_id_obj:
                        create_notification(
                            str(mid), 'declined',
                            f'The community "{comm_title}" was deleted by the owner.',
                            url_for('find_communities')
                        )
                communities_collection.delete_one({'_id': ObjectId(str(community_id))})
                community_messages_collection.delete_many({'community_id': ObjectId(str(community_id))})
                community_deleted = True

        # Count approved versions BEFORE deleting them: each gave +50 XP, so deleting the
        # project must take that XP back too (otherwise upload -> approve -> delete = free XP).
        approved_versions = commits_collection.count_documents(
            {'project_id': ObjectId(project_id), 'xp_status': 'approved'})

        # Delete the project itself
        projects_collection.delete_one({'_id': ObjectId(project_id)})
        commits_collection.delete_many({'project_id': ObjectId(project_id)})
        comments_collection.delete_many({'project_id': ObjectId(project_id)})
        users_collection.update_many({}, {'$pull': {'bookmarks': ObjectId(project_id)}})

        if not project.get('pre_gamification'):
            deduct_xp(current_user.id, 10, "Project Deleted")
        if approved_versions:
            deduct_xp(current_user.id, approved_versions * 50,
                      f"Project deleted ({approved_versions} approved version(s) reversed)")

        return jsonify({'success': True, 'community_deleted': community_deleted})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/project/<project_id>')
def view_project(project_id):
    try:
        obj_id = ObjectId(project_id)
    except Exception:
        flash("Invalid project ID format.", "error"); return redirect(url_for('projects'))
    project = projects_collection.find_one({'_id': obj_id})
    if not project:
        flash('Project not found.', 'error'); return redirect(url_for('projects'))

    if project.get('hidden'):
        is_admin_viewer = False
        if current_user.is_authenticated:
            me = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
            is_admin_viewer = bool(me and me.get('is_admin'))
        if not is_admin_viewer:
            flash('This project is no longer available.', 'error'); return redirect(url_for('projects'))

    is_owner = False
    if current_user.is_authenticated and str(project.get('created_by_id')) == current_user.id:
        is_owner = True

    # ── TRACK VIEW (only count non-owner views) ──────────
    if not is_owner:
        projects_collection.update_one(
            {'_id': obj_id},
            {'$inc': {'views': 1}}
        )
        project['views'] = project.get('views', 0) + 1

    # ── LIKE STATUS for current user ──────────────────────
    user_liked = False
    if current_user.is_authenticated:
        user_liked = ObjectId(current_user.id) in project.get('likes', [])
    like_count = len(project.get('likes', []))

    try:
        commits = list(commits_collection.find({'project_id': obj_id}).sort('timestamp', -1))
    except Exception as e:
        print(f"Error fetching commits for project {project_id}: {e}")
        flash("Could not load project history.", "error"); commits = []
    creator_name = project.get('created_by_name', 'Unknown User')

    # ── Stringify project ObjectId fields for template ────────
    project['_id'] = str(project['_id'])
    project['created_by_id'] = str(project.get('created_by_id', ''))

    # ── LOAD COMMENTS ────────────────────────────────────────
    is_admin = False
    if current_user.is_authenticated:
        user_doc = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
        is_admin = user_doc.get('is_admin', False) if user_doc else False

    raw_top = list(comments_collection.find(
        {'project_id': obj_id, 'parent_id': None, 'hidden': {'$ne': True}}
    ).sort('created_at', -1))

    def fmt_date(dt):
        if dt and hasattr(dt, 'strftime'):
            return fmt_utc(dt, '%b %d, %Y') + ' UTC'
        return ''

    comments = []
    for c in raw_top:
        c_id_obj = c['_id']           # keep ObjectId for reply query below
        c['_id'] = str(c_id_obj)
        c['user_id'] = str(c.get('user_id', ''))
        c['like_count'] = len(c.get('likes', []))
        c['user_liked'] = (
            current_user.is_authenticated and
            ObjectId(current_user.id) in c.get('likes', [])
        )
        c['is_owner_comment'] = c['user_id'] == project['created_by_id']
        c['can_delete'] = (
            current_user.is_authenticated and
            (c['user_id'] == current_user.id or is_admin)
        )
        c['created_at_str'] = fmt_date(c.get('created_at'))
        c['created_at_iso'] = utc_iso(c.get('created_at'))
        # Fetch replies (1 level only)
        replies = list(comments_collection.find(
            {'parent_id': c_id_obj, 'hidden': {'$ne': True}}
        ).sort('created_at', 1))
        for r in replies:
            r['_id'] = str(r['_id'])
            r['user_id'] = str(r.get('user_id', ''))
            r['like_count'] = len(r.get('likes', []))
            r['user_liked'] = (
                current_user.is_authenticated and
                ObjectId(current_user.id) in r.get('likes', [])
            )
            r['is_owner_comment'] = r['user_id'] == project['created_by_id']
            r['can_delete'] = (
                current_user.is_authenticated and
                (r['user_id'] == current_user.id or is_admin)
            )
            r['created_at_str'] = fmt_date(r.get('created_at'))
            r['created_at_iso'] = utc_iso(r.get('created_at'))
        c['replies'] = replies
        comments.append(c)

    comment_count = comments_collection.count_documents(
        {'project_id': obj_id, 'is_deleted': {'$ne': True}}
    )

    # Bookmark status
    user_bookmarked = False
    if current_user.is_authenticated:
        u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'bookmarks': 1})
        user_bookmarked = obj_id in u.get('bookmarks', []) if u else False
    bookmark_count = project.get('bookmark_count', 0)

    _xp = 0; _level_info = get_level_info(0); _streak = 0; _ppct = 0
    if current_user.is_authenticated:
        _pu = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1}) or {}
        _xp = _pu.get('xp', 0); _level_info = get_level_info(_xp)
        _streak = _pu.get('streak_count', 0)
        _ppct = int((_xp / _level_info['next_xp']) * 100) if _level_info['next_xp'] != "Max" else 100
    return render_template('project_page.html', project=project, is_owner=is_owner,
        commits=commits, creator_name=creator_name,
        user_liked=user_liked, like_count=like_count,
        comments=comments, comment_count=comment_count, is_admin=is_admin,
        user_bookmarked=user_bookmarked, bookmark_count=bookmark_count,
        xp=_xp, level_info=_level_info, streak=_streak, progress_pct=_ppct)


@app.route('/project/<project_id>/upload', methods=['GET', 'POST'])
@login_required
@limiter.limit("10 per hour")                             # ← ADDED
def upload_version(project_id):
    try:
        obj_id = ObjectId(project_id)
        project = projects_collection.find_one({'_id': obj_id})
    except Exception:
        flash('Invalid project ID format.', 'error'); return redirect(url_for('my_projects'))
    if not project:
        flash('Project not found.', 'error'); return redirect(url_for('my_projects'))
    if str(project.get('created_by_id')) != current_user.id:
        flash('You are not authorized to upload to this project.', 'error')
        return redirect(url_for('view_project', project_id=project_id))
    if request.method == 'POST':
        commit_message = request.form.get('message', '').strip()
        if not commit_message:
            flash('A version message is required.', 'error'); return render_template('upload_version.html', project=project)
        if 'project_file' not in request.files or not request.files['project_file'].filename:
            flash('You must select a .zip file to upload.', 'error'); return render_template('upload_version.html', project=project)
        file = request.files['project_file']
        original_filename = secure_filename(file.filename)
        _, f_ext = os.path.splitext(original_filename)
        if f_ext.lower() != '.zip':
             flash('Only .zip files are allowed.', 'error'); return render_template('upload_version.html', project=project)
        random_hex = secrets.token_hex(8)
        project_filename = random_hex + f_ext
        upload_dir = app.config['UPLOAD_FOLDER']
        file_path = os.path.join(upload_dir, project_filename)
        try:
            os.makedirs(upload_dir, exist_ok=True)
            file.save(file_path)
            commits_collection.insert_one({'project_id': obj_id, 'user_id': ObjectId(current_user.id), 'user_name': current_user.name, 'timestamp': now_ist(), 'message': commit_message, 'filename': project_filename, 'xp_status': 'pending'})
            flash('Version uploaded! ⏳ Waiting for admin approval to earn +50 XP.', 'success')
            return redirect(url_for('view_project', project_id=project_id))
        except Exception as e:
            print(f"Error saving file or commit for project {project_id}: {e}")
            flash('An error occurred during upload. Please try again.', 'error')
            if os.path.exists(file_path):
                 try: os.remove(file_path)
                 except OSError as e_rm: print(f"Error removing partially uploaded file {file_path}: {e_rm}")
            return render_template('upload_version.html', project=project)
    return render_template('upload_version.html', project=project)


@app.route('/download_project/<filename>')
def download_project(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
         flash('Invalid filename.', 'error'); return redirect(url_for('projects'))
    try:
        directory = app.config['UPLOAD_FOLDER']
        if not os.path.abspath(directory).startswith(os.path.abspath(os.path.join(app.root_path, 'static'))):
             print(f"Attempted directory traversal: {directory}")
             flash('Access denied.', 'error'); return redirect(url_for('projects'))
        return send_from_directory(directory, safe_filename, as_attachment=True)
    except FileNotFoundError:
        flash(f'The requested project file ({safe_filename}) was not found.', 'error')
        referrer = request.referrer
        if referrer and ('/project/' in referrer or '/projects' in referrer or '/my_projects' in referrer):
             return redirect(referrer)
        return redirect(url_for('projects'))
    except Exception as e:
        print(f"Error downloading file {filename}: {e}")
        flash('An error occurred while downloading the file.', 'error')
        return redirect(url_for('projects'))