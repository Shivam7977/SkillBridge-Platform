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

from extensions import (app, bcrypt, client, db, limiter, login_manager, scheduler, users_collection,
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


from .admin_users import admin_required

@app.route('/admin/delete_project/<project_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_project(project_id):
    """Admin deletes a project. Optional: also delete linked community."""
    data = request.get_json(silent=True) or {}
    also_delete_community = data.get('delete_community', False)

    project = projects_collection.find_one({'_id': ObjectId(project_id)})
    if project:
        create_notification(
            str(project['created_by_id']), 'declined',
            f'Your project "{project.get("title", "Untitled")}" was removed by an admin.',
            url_for('my_projects')
        )
        # Optionally delete linked community
        if also_delete_community and project.get('community_id'):
            community = communities_collection.find_one({'_id': ObjectId(str(project['community_id']))})
            if community:
                comm_title = community.get('project_title', 'a community')
                # Notify owner
                create_notification(
                    str(community['owner_id']), 'declined',
                    f'Your community "{comm_title}" was removed by an admin.',
                    url_for('find_communities')
                )
                # Notify all members
                for member_id in community.get('members', []):
                    if member_id != community['owner_id']:
                        create_notification(
                            str(member_id), 'declined',
                            f'The community "{comm_title}" was removed by an admin.',
                            url_for('find_communities')
                        )
                communities_collection.delete_one({'_id': ObjectId(str(project['community_id']))})
                community_messages_collection.delete_many({'community_id': ObjectId(str(project['community_id']))})

    projects_collection.delete_one({'_id': ObjectId(project_id)})
    commits_collection.delete_many({'project_id': ObjectId(project_id)})
    comments_collection.delete_many({'project_id': ObjectId(project_id)})
    users_collection.update_many({}, {'$pull': {'bookmarks': ObjectId(project_id)}})
    return jsonify({'success': True})

@app.route('/admin/delete_roadmap/<roadmap_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_roadmap(roadmap_id):
    roadmap = roadmaps_collection.find_one({'_id': ObjectId(roadmap_id)})
    if roadmap:
        create_notification(
            str(roadmap['user_id']), 'declined',
            f'Your roadmap "{roadmap.get("goal", "Untitled")}" was removed by an admin.',
            url_for('my_roadmaps')
        )
    roadmaps_collection.delete_one({'_id': ObjectId(roadmap_id)})
    return jsonify({'success': True})

@app.route('/admin/delete_community/<community_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_community(community_id):
    """Admin deletes a community and its messages"""
    community = communities_collection.find_one({'_id': ObjectId(community_id)})
    if community:
        title = community.get('project_title', 'a community')
        # Notify owner
        create_notification(
            str(community['owner_id']), 'declined',
            f'Your community "{title}" was removed by an admin.',
            url_for('find_communities')
        )
        # Notify all members
        for member_id in community.get('members', []):
            if member_id != community['owner_id']:
                create_notification(
                    str(member_id), 'declined',
                    f'The community "{title}" was removed by an admin.',
                    url_for('find_communities')
                )
    communities_collection.delete_one({'_id': ObjectId(community_id)})
    community_messages_collection.delete_many({'community_id': ObjectId(community_id)})
    return jsonify({'success': True})

@app.route('/admin/project_comments/<project_id>')
@login_required
@admin_required
def admin_project_comments(project_id):
    """Return all comments for a project — used by admin modal"""
    try:
        obj_id = ObjectId(project_id)
        raw = list(comments_collection.find({'project_id': obj_id}).sort('created_at', 1))
        result = []
        for c in raw:
            dt = c.get('created_at')
            created_at_iso = utc_iso(dt) if dt and hasattr(dt, 'strftime') else ''
            result.append({
                '_id': str(c['_id']),
                'user_name': c.get('user_name', ''),
                'username': c.get('username', ''),
                'profile_pic': c.get('profile_pic', 'default.jpg'),
                'content': c.get('content', ''),
                'parent_id': str(c['parent_id']) if c.get('parent_id') else None,
                'like_count': len(c.get('likes', [])),
                'is_deleted': c.get('is_deleted', False),
                'created_at_iso': created_at_iso,
                'created_at_str': dt.strftime('%b %d, %Y at %I:%M %p') if dt and hasattr(dt, 'strftime') else '—',
            })
        return jsonify({'success': True, 'comments': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/project/<project_id>')
@login_required
@admin_required
def admin_project_detail(project_id):
    """JSON data for the Project detail modal."""
    try:
        obj_id = ObjectId(project_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid project ID'}), 400
    project = projects_collection.find_one({'_id': obj_id})
    if not project:
        return jsonify({'success': False, 'error': 'Project not found'}), 404

    creator_oid = project.get('created_by_id')
    creator = users_collection.find_one({'_id': creator_oid}, {'name': 1, 'username': 1}) if creator_oid else None

    def _hydrate(ids):
        docs = list(users_collection.find({'_id': {'$in': ids}}, {'name': 1, 'username': 1, 'profile_pic': 1}))
        return [{
            'id': str(u['_id']), 'name': u.get('name', '?'), 'username': u.get('username', ''),
            'profile_pic_url': f"/static/profile_pics/{u.get('profile_pic', 'default.jpg')}"
        } for u in docs]

    likers = _hydrate(project.get('likes', []))
    saver_ids = [u['_id'] for u in users_collection.find({'bookmarks': obj_id}, {'_id': 1})]
    savers = _hydrate(saver_ids)
    comment_count = comments_collection.count_documents({'project_id': obj_id, 'is_deleted': {'$ne': True}})

    community_oid = project.get('community_id')
    community = communities_collection.find_one({'_id': ObjectId(str(community_oid))}, {'project_title': 1}) if community_oid else None

    return jsonify({'success': True, 'project': {
        'id': str(project['_id']),
        'title': project.get('title', 'Untitled'),
        'description': project.get('description', ''),
        'skills_needed': project.get('skills_needed', []),
        'tech_stack': project.get('tech_stack', ''),
        'is_completed': bool(project.get('is_completed') or project.get('status') == 'completed'),
        'hidden': bool(project.get('hidden')),
        'github_link': project.get('github_link', ''),
        'created_at': utc_iso(project.get('created_at')) if project.get('created_at') else None,
        'views': project.get('views', 0),
        'creator': {'id': str(creator['_id']), 'name': creator.get('name'), 'username': creator.get('username', '')} if creator else None,
        'community': {'id': str(community_oid), 'title': community.get('project_title')} if community else None,
        'likers': likers,
        'savers': savers,
        'comment_count': comment_count,
    }})

@app.route('/admin/roadmap/<roadmap_id>')
@login_required
@admin_required
def admin_roadmap_detail(roadmap_id):
    """JSON data for the Roadmap detail modal."""
    try:
        obj_id = ObjectId(roadmap_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid roadmap ID'}), 400
    roadmap = roadmaps_collection.find_one({'_id': obj_id})
    if not roadmap:
        return jsonify({'success': False, 'error': 'Roadmap not found'}), 404

    user_oid = roadmap.get('user_id')
    creator = users_collection.find_one({'_id': user_oid}, {'name': 1, 'username': 1}) if user_oid else None

    content = roadmap.get('roadmap_content', {})
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = {}
    stages = content.get('stages', []) if isinstance(content, dict) else []
    stage_list = [{
        'title': (s.get('title') or s.get('name') or f"Stage {i + 1}") if isinstance(s, dict) else f"Stage {i + 1}",
        'completed': bool(isinstance(s, dict) and s.get('completed'))
    } for i, s in enumerate(stages)]

    return jsonify({'success': True, 'roadmap': {
        'id': str(roadmap['_id']),
        'goal': roadmap.get('goal', 'Untitled'),
        'creator': {'id': str(creator['_id']), 'name': creator.get('name'), 'username': creator.get('username', '')} if creator else None,
        'created_at': utc_iso(roadmap.get('created_at')) if roadmap.get('created_at') else None,
        'stages': stage_list,
        'stage_count': len(stage_list),
        'completed_stages': sum(1 for s in stage_list if s['completed']),
        'is_fully_complete': bool(stage_list) and all(s['completed'] for s in stage_list),
        'hidden': bool(roadmap.get('hidden')),
        'last_visited': utc_iso(roadmap.get('last_activity')) if roadmap.get('last_activity') else None,
        'last_reminder_sent_at': utc_iso(roadmap.get('last_reminder_sent_at')) if roadmap.get('last_reminder_sent_at') else None,
    }})

@app.route('/admin/community/<community_id>')
@login_required
@admin_required
def admin_community_detail(community_id):
    """JSON data for the Community detail modal."""
    try:
        obj_id = ObjectId(community_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid community ID'}), 400
    community = communities_collection.find_one({'_id': obj_id})
    if not community:
        return jsonify({'success': False, 'error': 'Community not found'}), 404

    owner_oid = community.get('owner_id')
    admin_ids = community.get('admins', [])
    member_ids = community.get('members', [])
    member_docs = list(users_collection.find({'_id': {'$in': member_ids}}, {'name': 1, 'username': 1, 'profile_pic': 1}))
    members = []
    for m in member_docs:
        role = 'Owner' if m['_id'] == owner_oid else ('Admin' if m['_id'] in admin_ids else 'Member')
        members.append({
            'id': str(m['_id']), 'name': m.get('name', '?'), 'username': m.get('username', ''),
            'profile_pic_url': f"/static/profile_pics/{m.get('profile_pic', 'default.jpg')}",
            'role': role
        })
    role_order = {'Owner': 0, 'Admin': 1, 'Member': 2}
    members.sort(key=lambda m: role_order.get(m['role'], 3))

    linked_project_oid = community.get('project_id')
    linked_project = projects_collection.find_one({'_id': linked_project_oid}, {'title': 1}) if linked_project_oid else None

    return jsonify({'success': True, 'community': {
        'id': str(community['_id']),
        'title': community.get('project_title', 'Untitled'),
        'visibility': community.get('visibility', 'public'),
        'owner_id': str(owner_oid) if owner_oid else None,
        'owner_name': community.get('owner_name', ''),
        'created_at': utc_iso(community.get('created_at')) if community.get('created_at') else None,
        'skills_required': community.get('skills_required', []),
        'hidden': bool(community.get('hidden')),
        'linked_project': {'id': str(linked_project['_id']), 'title': linked_project.get('title')} if linked_project else None,
        'members': members,
    }})

