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


@app.route('/community/<community_id>')
@login_required
def view_community(community_id):
    try:
        community = communities_collection.find_one({"_id": ObjectId(community_id)})
    except Exception:
        flash("Invalid community.", "error"); return redirect(url_for("find_communities"))
    if not community:
        flash("Community not found", "error"); return redirect(url_for("find_communities"))

    if community.get('hidden'):
        me = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
        if not (me and me.get('is_admin')):
            flash("This community is no longer available.", "error"); return redirect(url_for("find_communities"))

    user_id   = ObjectId(current_user.id)
    is_owner  = community.get("owner_id") == user_id
    is_admin  = user_id in community.get("admins", [])
    is_member = user_id in community.get("members", [])
    is_public = community.get("visibility") == "public"

    member_users = list(users_collection.find(
        {"_id": {"$in": community.get("members", [])}, "is_banned": {"$ne": True}, "pending_deletion": {"$ne": True}},
        {"name": 1, "username": 1}
    ))
    members_data = []
    for u in member_users:
        role = "member"
        if u["_id"] == community.get("owner_id"):     role = "owner"
        elif u["_id"] in community.get("admins", []): role = "admin"
        members_data.append({
            "_id": str(u["_id"]), "name": u.get("name", "User"),
            "username": u.get("username", ""), "role": role
        })

    messages = []
    if is_member or is_public:
        raw_msgs = list(community_messages_collection.find(
            {"community_id": ObjectId(community_id)}
        ).sort("timestamp", -1).limit(50))
        raw_msgs = list(reversed(raw_msgs))  # show oldest→newest
        for m in raw_msgs:
            ts = m.get("timestamp")
            if not ts or not isinstance(ts, datetime):
                ts = now_utc()
            m["timestamp"] = ts
            m["timestamp_iso"] = utc_iso(ts)
            messages.append(m)

    pending_ids   = community.get("pending_requests", [])
    pending_users = []
    if (is_owner or is_admin) and pending_ids:
        for u in users_collection.find({"_id": {"$in": pending_ids}}, {"name": 1, "username": 1}):
            pending_users.append({
                "_id": str(u["_id"]), "name": u.get("name", "User"),
                "username": u.get("username", "")
            })

    user_request_pending = user_id in pending_ids

    return render_template("community_chat.html",
        community            = community,
        messages             = messages,
        members_data         = members_data,
        is_owner             = is_owner,
        is_admin             = is_admin,
        is_member            = is_member,
        is_public            = is_public,
        pending_requests     = pending_ids,
        pending_users        = pending_users,
        user_request_pending = user_request_pending
    )

@app.route('/community/<community_id>/react/<message_id>/<emoji>', methods=['GET','POST'])
@login_required
def react_to_message(community_id, message_id, emoji):
    user_id = ObjectId(current_user.id)
    message = community_messages_collection.find_one({"_id": ObjectId(message_id)})
    if not message:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": "Message not found"}), 404
        return redirect(url_for("view_community", community_id=community_id))
    reactions = message.get("reactions", {})
    if emoji not in reactions: reactions[emoji] = []
    if user_id in reactions[emoji]:
        reactions[emoji].remove(user_id)
        reacted = False
    else:
        reactions[emoji].append(user_id)
        reacted = True
    community_messages_collection.update_one({"_id": ObjectId(message_id)}, {"$set": {"reactions": reactions}})
    # AJAX response for frontend reaction updates
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({"success": True, "reacted": reacted, "count": len(reactions[emoji])})
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/request')
@login_required
def request_to_join_community(community_id):
    obj_id = ObjectId(community_id)
    user_id = ObjectId(current_user.id)
    community = communities_collection.find_one({"_id": obj_id})
    if not community:
        flash("Community not found.", "error"); return redirect(url_for("find_communities"))
    if user_id in community.get("members", []):
        return redirect(url_for("view_community", community_id=community_id))
    # Check if already has a pending request (prevent spam-clicking for XP)
    if user_id not in community.get("pending_requests", []):
        communities_collection.update_one({"_id": obj_id}, {"$addToSet": {"pending_requests": user_id}, "$pull": {"rejected_requests": user_id}})
        add_xp(current_user.id, 10, "Joined a Community")
        flash("Request sent! Wait for owner approval. +10 XP 🎉", "info")
    else:
        flash("Request already pending.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/leave', methods=['POST'])
@login_required
def leave_community(community_id):
    """User leaves a community — deduct XP to prevent join-leave abuse"""
    obj_id = ObjectId(community_id)
    user_id = ObjectId(current_user.id)
    community = communities_collection.find_one({"_id": obj_id})
    if not community:
        return jsonify({'success': False, 'error': 'Community not found'}), 404
    # Owner cannot leave
    if community.get("owner_id") == user_id:
        return jsonify({'success': False, 'error': 'Owner cannot leave community'}), 400
    if user_id in community.get("members", []):
        communities_collection.update_one({"_id": obj_id}, {"$pull": {"members": user_id, "admins": user_id}})
        deduct_xp(current_user.id, 10, "Left a Community")
        flash("You have left the community. -10 XP", "info")
    return redirect(url_for("find_communities"))

@app.route('/community/<community_id>/delete', methods=['POST'])
@login_required
def owner_delete_community(community_id):
    """Owner deletes their own community. Notifies all members. Unlinks project."""
    try:
        obj_id = ObjectId(community_id)
        community = communities_collection.find_one({'_id': obj_id})
        if not community:
            return jsonify({'success': False, 'error': 'Community not found'}), 404

        # Only owner can delete
        if community.get('owner_id') != ObjectId(current_user.id):
            return jsonify({'success': False, 'error': 'Only the owner can delete this community'}), 403

        comm_title = community.get('project_title', 'a community')
        owner_id_obj = community.get('owner_id')

        # Notify all members except owner
        for mid in community.get('members', []):
            if mid != owner_id_obj:
                create_notification(
                    str(mid), 'declined',
                    f'The community "{comm_title}" was deleted by the owner.',
                    url_for('find_communities')
                )

        # Unlink community from its project (clean up community_id field)
        project_id = community.get('project_id')
        if project_id:
            projects_collection.update_one(
                {'_id': project_id},
                {'$unset': {'community_id': ''}}
            )

        # Delete community and all its messages
        communities_collection.delete_one({'_id': obj_id})
        community_messages_collection.delete_many({'community_id': obj_id})

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/community/<community_id>/approve/<user_id>')
@login_required
def approve_member(community_id, user_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    if not community:
        flash("Community not found.", "error"); return redirect(url_for("find_communities"))
    # FIX: members are stored as plain ObjectIds, not dicts — check owner/admin arrays directly
    is_owner = community["owner_id"] == ObjectId(current_user.id)
    is_admin = ObjectId(current_user.id) in community.get("admins", [])
    if not is_owner and not is_admin:
        flash("Unauthorized action.", "error"); return redirect(url_for("view_community", community_id=community_id))
    # FIX: store member as plain ObjectId (consistent with rest of codebase), not as a dict
    communities_collection.update_one(
        {"_id": ObjectId(community_id)},
        {
            "$pull": {"pending_requests": ObjectId(user_id)},
            "$addToSet": {"members": ObjectId(user_id)}
        }
    )
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    comm_title = community.get('project_title', 'a community') if community else 'a community'
    create_notification(user_id, 'approved', f'Your request to join "{comm_title}" was approved! 🎉', url_for('view_community', community_id=community_id))
    flash("Member approved!", "success")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/make_admin/<user_id>')
@login_required
def make_admin(community_id, user_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    if community["owner_id"] != ObjectId(current_user.id):
        flash("Only owner can assign admin.", "error"); return redirect(url_for("view_community", community_id=community_id))
    communities_collection.update_one({"_id": ObjectId(community_id)}, {"$addToSet": {"admins": ObjectId(user_id)}})
    flash("Member promoted to Admin.", "success")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/decline/<user_id>')
@login_required
def decline_member(community_id, user_id):
    obj_id = ObjectId(community_id)
    user_obj = ObjectId(user_id)
    community = communities_collection.find_one({"_id": obj_id})
    if community["owner_id"] != ObjectId(current_user.id):
        flash("Unauthorized.", "error"); return redirect(url_for("view_community", community_id=community_id))
    communities_collection.update_one({"_id": obj_id}, {"$pull": {"pending_requests": user_obj}, "$addToSet": {"rejected_requests": user_obj}})
    deduct_xp(str(user_obj), 10, "Community request declined")
    comm = communities_collection.find_one({"_id": obj_id})
    comm_title = comm.get('project_title', 'a community') if comm else 'a community'
    create_notification(str(user_obj), 'declined', f'Your request to join "{comm_title}" was declined.', url_for('find_communities'))
    flash("Request declined.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/remove_member/<user_id>')
@login_required
def remove_member(community_id, user_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    current_user_id = ObjectId(current_user.id)
    is_owner = community["owner_id"] == current_user_id
    is_admin = current_user_id in community.get("admins", [])
    if not is_owner and not is_admin:
        flash("Unauthorized action.", "error"); return redirect(url_for("view_community", community_id=community_id))
    communities_collection.update_one({"_id": ObjectId(community_id)}, {"$pull": {"members": ObjectId(user_id), "admins": ObjectId(user_id)}})
    deduct_xp(user_id, 10, "Removed from Community")
    comm_r = communities_collection.find_one({"_id": ObjectId(community_id)})
    comm_title_r = comm_r.get('project_title', 'a community') if comm_r else 'a community'
    create_notification(user_id, 'removed', f'You were removed from "{comm_title_r}".', url_for('find_communities'))
    flash("Member removed.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/remove_admin/<user_id>')
@login_required
def remove_admin(community_id, user_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    if community["owner_id"] != ObjectId(current_user.id):
        flash("Only owner can remove admin.", "error"); return redirect(url_for("view_community", community_id=community_id))
    communities_collection.update_one({"_id": ObjectId(community_id)}, {"$pull": {"admins": ObjectId(user_id)}})
    flash("Admin removed.", "info")
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/communities')
@login_required
def find_communities():
    user_id = ObjectId(current_user.id)

    # My communities (owned) — load all, usually very few
    my_communities = list(communities_collection.find(
        {"owner_id": user_id}
    ).sort("created_at", -1))
    # Get current user's username for owned communities
    current_user_data = users_collection.find_one({'_id': user_id}, {'username': 1})
    current_username = current_user_data.get('username', '') if current_user_data else ''
    for c in my_communities:
        c["is_owner"] = True
        c["is_member"] = True
        c["is_other"] = False
        c["owner_username"] = current_username

    # Joined communities (member but not owner) — load all, usually few
    joined_communities = list(communities_collection.find(
        {"members": user_id, "owner_id": {"$ne": user_id}, "hidden": {"$ne": True}}
    ).sort("created_at", -1))
    for c in joined_communities:
        c["is_owner"] = False
        c["is_member"] = True
        c["is_other"] = False
        # Get owner username
        owner_data = users_collection.find_one({'_id': c.get('owner_id')}, {'username': 1, 'name': 1})
        c["owner_username"] = owner_data.get('username', '') if owner_data else ''
        c["owner_name"] = owner_data.get('name', '') if owner_data else ''

    # Explore — paginated with Load More
    PER_PAGE = 9
    page = request.args.get('page', 1, type=int)
    exclude_ids = [c["_id"] for c in my_communities + joined_communities]
    other_query = {"_id": {"$nin": exclude_ids}, "hidden": {"$ne": True}}
    other_total = communities_collection.count_documents(other_query)
    other_communities = list(communities_collection.find(other_query)
        .sort("created_at", -1)
        .skip((page - 1) * PER_PAGE)
        .limit(PER_PAGE))
    for c in other_communities:
        c["is_owner"] = False
        c["is_member"] = False
        c["is_other"] = True
        c["member_count"] = len(c.get("members", []))
        owner_data = users_collection.find_one({'_id': c.get('owner_id')}, {'username': 1, 'name': 1})
        c["owner_username"] = owner_data.get('username', '') if owner_data else ''
        c["owner_name"] = owner_data.get('name', '') if owner_data else ''

    total_pages = max(1, (other_total + PER_PAGE - 1) // PER_PAGE)
    has_more = page < total_pages

    _u = users_collection.find_one({'_id': user_id}, {'xp': 1, 'streak_count': 1}) or {}
    _xp = _u.get('xp', 0)
    _level_info = get_level_info(_xp)
    _streak = _u.get('streak_count', 0)
    _ppct = int((_xp / _level_info['next_xp']) * 100) if _level_info['next_xp'] != "Max" else 100

    return render_template("find_communities.html",
        my_communities=my_communities,
        joined_communities=joined_communities,
        other_communities=other_communities,
        other_total=other_total,
        page=page,
        total_pages=total_pages,
        has_more=has_more,
        xp=_xp,
        level_info=_level_info,
        streak=_streak,
        progress_pct=_ppct)

@app.route('/api/communities')
@login_required
def api_communities():
    """JSON endpoint for Load More on find_communities page"""
    try:
        PER_PAGE = 9
        page = request.args.get('page', 1, type=int)
        user_id = ObjectId(current_user.id)

        # Exclude user's own and joined communities
        exclude_ids = [c["_id"] for c in communities_collection.find(
            {"$or": [{"owner_id": user_id}, {"members": user_id}]},
            {"_id": 1}
        )]
        query = {"_id": {"$nin": exclude_ids}}
        total = communities_collection.count_documents(query)
        comms = list(communities_collection.find(query)
            .sort("created_at", -1)
            .skip((page - 1) * PER_PAGE)
            .limit(PER_PAGE))

        result = []
        for c in comms:
            owner_data = users_collection.find_one({'_id': c.get('owner_id')}, {'username': 1})
            result.append({
                '_id': str(c['_id']),
                'project_title': c.get('project_title', 'Untitled'),
                'visibility': c.get('visibility', 'public'),
                'skills_required': c.get('skills_required', []),
                'member_count': len(c.get('members', [])),
                'owner_username': owner_data.get('username', '') if owner_data else '',
            })

        return jsonify({
            'communities': result,
            'page': page,
            'total_pages': max(1, (total + PER_PAGE - 1) // PER_PAGE),
            'has_more': page < max(1, (total + PER_PAGE - 1) // PER_PAGE)
        })
    except Exception as e:
        print(f"API communities error: {e}")
        return jsonify({'communities': [], 'has_more': False}), 500

@app.route('/api/community/<community_id>/messages')
@login_required
def get_community_messages(community_id):
    """Returns messages since a given timestamp for real-time polling"""
    since = request.args.get('since', None)
    query = {"community_id": ObjectId(community_id)}
    if since:
        try:
            since_dt = as_utc(since)
            query["timestamp"] = {"$gt": since_dt}
        except Exception as e:
            print(f"Since parse error: {e}")

    messages = list(community_messages_collection.find(query).sort("timestamp", 1))
    result = []
    for msg in messages:
        ts = msg.get('timestamp')
        if not ts or not isinstance(ts, datetime):
            ts = now_utc()
        reactions_raw = msg.get('reactions', {})
        reactions = {k: (len(v) if isinstance(v, list) else int(v)) for k, v in reactions_raw.items()}
        result.append({
            'id':            str(msg['_id']),
            'sender_id':     str(msg.get('sender_id', '')),
            'sender_name':   msg.get('sender_name', 'Unknown'),
            'message':       msg.get('message', ''),
            'timestamp':     fmt_utc(ts, '%I:%M %p') + ' UTC',
            'timestamp_iso': utc_iso(ts),
            'reactions':     reactions,
            'is_me':         str(msg.get('sender_id', '')) == current_user.id
        })
    return jsonify({'messages': result, 'current_user_id': current_user.id})

@app.route('/api/community/<community_id>/send', methods=['POST'])
@login_required
@limiter.limit("30 per minute", key_func=get_user_key)    # ← UPDATED: per-account key
def send_community_message_api(community_id):
    """Send a message via JSON API (used by real-time chat)"""
    data = request.get_json()
    msg = sanitize(data.get('message', '')) if data else ''  # ← XSS fix
    if msg:
        community_messages_collection.insert_one({
            "community_id": ObjectId(community_id),
            "sender_id": ObjectId(current_user.id),
            "sender_name": current_user.name,
            "message": msg,
            "timestamp": now_ist(),
            "reactions": {}
        })
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Empty message'}), 400

@app.route('/api/community/<community_id>/members')
@login_required
def api_community_members(community_id):
    """Paginated + searchable members list — avoids loading 1000 members on page load."""
    q      = request.args.get('q', '').strip().lower()
    offset = int(request.args.get('offset', 0))
    limit  = min(int(request.args.get('limit', 2)), 50)  # cap at 50 per request

    try:
        community = communities_collection.find_one({'_id': ObjectId(community_id)})
    except Exception:
        return jsonify({'members': []}), 400

    if not community:
        return jsonify({'members': []})

    # Only members/public communities can view the list
    user_id   = ObjectId(current_user.id)
    is_member = user_id in community.get('members', [])
    is_public = community.get('visibility') == 'public'
    if not is_member and not is_public:
        return jsonify({'members': []}), 403

    member_ids = community.get('members', [])
    owner_id   = community.get('owner_id')

    # Build mongo query
    query = {'_id': {'$in': member_ids}, 'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}}
    if q:
        query['name'] = {'$regex': q, '$options': 'i'}

    total  = users_collection.count_documents(query)
    users  = list(users_collection.find(query, {'name': 1}).skip(offset).limit(limit))

    result = []
    for u in users:
        uid  = u['_id']
        role = 'owner' if uid == owner_id else (
               'admin'  if uid in community.get('admins', []) else 'member')
        result.append({
            'id':   str(uid),
            'name': u.get('name', '?'),
            'role': role
        })

    return jsonify({'members': result, 'total': total})

