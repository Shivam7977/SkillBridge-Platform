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

@app.route('/project/<project_id>/like', methods=['POST'])
@login_required
def toggle_like(project_id):
    """Toggle like on a project — one like per user, stored as array of user IDs"""
    try:
        obj_id = ObjectId(project_id)
        user_id = ObjectId(current_user.id)
        project = projects_collection.find_one({'_id': obj_id}, {'likes': 1, 'created_by_id': 1})
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404

        likes = project.get('likes', [])
        already_liked = user_id in likes

        if already_liked:
            # Unlike
            projects_collection.update_one({'_id': obj_id}, {'$pull': {'likes': user_id}})
            new_count = len(likes) - 1
            liked = False
        else:
            # Like — can't like your own project
            if str(project.get('created_by_id')) == current_user.id:
                return jsonify({'success': False, 'error': 'Cannot like your own project'}), 400
            projects_collection.update_one({'_id': obj_id}, {'$addToSet': {'likes': user_id}})
            new_count = len(likes) + 1
            liked = True

        return jsonify({'success': True, 'liked': liked, 'count': new_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/project/<project_id>/bookmark', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def toggle_bookmark(project_id):
    """Toggle bookmark on a project — stored in users.bookmarks[]"""
    try:
        obj_id = ObjectId(project_id)
        user_id = ObjectId(current_user.id)
        user = users_collection.find_one({'_id': user_id}, {'bookmarks': 1})
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        bookmarks = user.get('bookmarks', [])
        if obj_id in bookmarks:
            users_collection.update_one({'_id': user_id}, {'$pull': {'bookmarks': obj_id}})
            # Decrement bookmark count on project
            projects_collection.update_one({'_id': obj_id}, {'$inc': {'bookmark_count': -1}})
            return jsonify({'success': True, 'bookmarked': False})
        else:
            users_collection.update_one({'_id': user_id}, {'$addToSet': {'bookmarks': obj_id}})
            projects_collection.update_one({'_id': obj_id}, {'$inc': {'bookmark_count': 1}})
            return jsonify({'success': True, 'bookmarked': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/project/<project_id>/comment', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def post_comment(project_id):
    """Post a top-level comment on a project"""
    try:
        obj_id = ObjectId(project_id)
        project = projects_collection.find_one({'_id': obj_id}, {'_id': 1, 'created_by_id': 1})
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        data = request.get_json()
        content = sanitize(data.get('content', ''))
        if not content or len(content) < 2:
            return jsonify({'success': False, 'error': 'Comment is too short'}), 400
        if len(content) > 500:
            return jsonify({'success': False, 'error': 'Comment too long (max 500 chars)'}), 400
        user = users_collection.find_one(
            {'_id': ObjectId(current_user.id)},
            {'name': 1, 'username': 1, 'profile_pic': 1}
        )
        is_owner_comment = str(ObjectId(current_user.id)) == str(project.get('created_by_id'))
        created_at = now_utc()
        comment_id = comments_collection.insert_one({
            'project_id': obj_id,
            'user_id': ObjectId(current_user.id),
            'user_name': user.get('name', current_user.name),
            'username': user.get('username', ''),
            'profile_pic': user.get('profile_pic', 'default.jpg'),
            'content': content,
            'parent_id': None,
            'likes': [],
            'created_at': created_at,
            'is_deleted': False
        }).inserted_id
        return jsonify({
            'success': True,
            'comment': {
                '_id': str(comment_id),
                'user_name': user.get('name', current_user.name),
                'username': user.get('username', ''),
                'profile_pic': user.get('profile_pic', 'default.jpg'),
                'content': content,
                'created_at': 'Just now',
                'created_at_iso': utc_iso(created_at),
                'like_count': 0,
                'user_liked': False,
                'is_owner_comment': is_owner_comment,
                'can_delete': True,
                'replies': []
            }
        })
    except Exception as e:
        print(f"Post comment error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/project/<project_id>/comment/<comment_id>/reply', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def post_reply(project_id, comment_id):
    """Post a reply to a top-level comment (1 level only)"""
    try:
        obj_id = ObjectId(project_id)
        parent_id = ObjectId(comment_id)
        parent = comments_collection.find_one({'_id': parent_id, 'parent_id': None})
        if not parent:
            return jsonify({'success': False, 'error': 'Parent comment not found'}), 404
        data = request.get_json()
        content = sanitize(data.get('content', ''))
        if not content or len(content) < 2:
            return jsonify({'success': False, 'error': 'Reply is too short'}), 400
        if len(content) > 500:
            return jsonify({'success': False, 'error': 'Reply too long (max 500 chars)'}), 400
        user = users_collection.find_one(
            {'_id': ObjectId(current_user.id)},
            {'name': 1, 'username': 1, 'profile_pic': 1}
        )
        project = projects_collection.find_one({'_id': obj_id}, {'created_by_id': 1})
        is_owner_comment = str(ObjectId(current_user.id)) == str(project.get('created_by_id'))
        created_at = now_utc()
        reply_id = comments_collection.insert_one({
            'project_id': obj_id,
            'user_id': ObjectId(current_user.id),
            'user_name': user.get('name', current_user.name),
            'username': user.get('username', ''),
            'profile_pic': user.get('profile_pic', 'default.jpg'),
            'content': content,
            'parent_id': parent_id,
            'likes': [],
            'created_at': created_at,
            'is_deleted': False
        }).inserted_id
        return jsonify({
            'success': True,
            'reply': {
                '_id': str(reply_id),
                'user_name': user.get('name', current_user.name),
                'username': user.get('username', ''),
                'profile_pic': user.get('profile_pic', 'default.jpg'),
                'content': content,
                'created_at': 'Just now',
                'created_at_iso': utc_iso(created_at),
                'like_count': 0,
                'user_liked': False,
                'is_owner_comment': is_owner_comment,
                'can_delete': True
            }
        })
    except Exception as e:
        print(f"Post reply error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/comment/<comment_id>/like', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def toggle_comment_like(comment_id):
    """Toggle like on a comment or reply"""
    try:
        cid = ObjectId(comment_id)
        user_id = ObjectId(current_user.id)
        comment = comments_collection.find_one({'_id': cid}, {'likes': 1})
        if not comment:
            return jsonify({'success': False, 'error': 'Comment not found'}), 404
        likes = comment.get('likes', [])
        if user_id in likes:
            comments_collection.update_one({'_id': cid}, {'$pull': {'likes': user_id}})
            liked = False
            count = len(likes) - 1
        else:
            comments_collection.update_one({'_id': cid}, {'$addToSet': {'likes': user_id}})
            liked = True
            count = len(likes) + 1
        return jsonify({'success': True, 'liked': liked, 'count': count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/comment/<comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    """Hard-delete a comment. If it's a top-level comment, also delete all its replies."""
    try:
        cid = ObjectId(comment_id)
        comment = comments_collection.find_one({'_id': cid})
        if not comment:
            return jsonify({'success': False, 'error': 'Comment not found'}), 404
        is_own = str(comment.get('user_id')) == current_user.id
        
        is_admin = False
        user_doc = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
        if user_doc and user_doc.get('is_admin'):
            is_admin = True
        if not is_own and not is_admin:
            return jsonify({'success': False, 'error': 'Not authorized'}), 403

        is_top_level = comment.get('parent_id') is None

        if is_top_level:
            # Delete all replies first, then the comment itself
            replies_deleted = comments_collection.delete_many({'parent_id': cid}).deleted_count
            comments_collection.delete_one({'_id': cid})
            return jsonify({'success': True, 'is_top_level': True, 'replies_deleted': replies_deleted})
        else:
            # Just delete the reply itself
            comments_collection.delete_one({'_id': cid})
            return jsonify({'success': True, 'is_top_level': False})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/project/<project_id>/comments', methods=['GET'])
@login_required
def get_comments_sorted(project_id):
    """Return comments re-sorted — called by JS sort toggle (top/recent)"""
    try:
        obj_id = ObjectId(project_id)
        sort_by = request.args.get('sort', 'recent')
        
        is_admin = False
        user_doc = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
        if user_doc and user_doc.get('is_admin'):
            is_admin = True
        project = projects_collection.find_one({'_id': obj_id}, {'created_by_id': 1})
        if not project:
            return jsonify({'success': False}), 404
        raw_top = list(comments_collection.find({'project_id': obj_id, 'parent_id': None, 'hidden': {'$ne': True}}))
        if sort_by == 'top':
            raw_top.sort(key=lambda c: len(c.get('likes', [])), reverse=True)
        else:
            raw_top.sort(key=lambda c: c.get('created_at', datetime.min), reverse=True)
        result = []
        for c in raw_top:
            replies = list(comments_collection.find({'parent_id': c['_id'], 'hidden': {'$ne': True}}).sort('created_at', 1))
            reply_list = []
            for r in replies:
                reply_list.append({
                    '_id': str(r['_id']),
                    'user_name': r.get('user_name', ''),
                    'username': r.get('username', ''),
                    'profile_pic': r.get('profile_pic', 'default.jpg'),
                    'content': r.get('content', ''),
                    'created_at': fmt_utc(r.get('created_at'), '%b %d, %Y') + ' UTC',
                    'created_at_iso': utc_iso(r.get('created_at')),
                    'like_count': len(r.get('likes', [])),
                    'user_liked': ObjectId(current_user.id) in r.get('likes', []),
                    'is_owner_comment': str(r.get('user_id')) == str(project.get('created_by_id')),
                    'can_delete': str(r.get('user_id')) == current_user.id or is_admin,
                    'is_deleted': r.get('is_deleted', False)
                })
            result.append({
                '_id': str(c['_id']),
                'user_name': c.get('user_name', ''),
                'username': c.get('username', ''),
                'profile_pic': c.get('profile_pic', 'default.jpg'),
                'content': c.get('content', ''),
                'created_at': fmt_utc(c.get('created_at'), '%b %d, %Y') + ' UTC',
                'created_at_iso': utc_iso(c.get('created_at')),
                'like_count': len(c.get('likes', [])),
                'user_liked': ObjectId(current_user.id) in c.get('likes', []),
                'is_owner_comment': str(c.get('user_id')) == str(project.get('created_by_id')),
                'can_delete': str(c.get('user_id')) == current_user.id or is_admin,
                'is_deleted': c.get('is_deleted', False),
                'replies': reply_list
            })
        return jsonify({'success': True, 'comments': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

