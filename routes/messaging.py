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


@app.route('/user/<user_id>')
def view_user_profile(user_id):
    try:
        obj_id = ObjectId(user_id)
        user_data = users_collection.find_one({'_id': obj_id})
        if not user_data:
            flash("User not found.", "error"); return redirect(url_for('projects'))

        if user_data.get('is_banned') or user_data.get('pending_deletion'):
            is_admin_viewer = False
            if current_user.is_authenticated:
                me = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'is_admin': 1})
                is_admin_viewer = bool(me and me.get('is_admin'))
            if not is_admin_viewer:
                flash("This profile is no longer available.", "error"); return redirect(url_for('projects'))
        profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
        profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)
        user_profile = {
            'id': str(user_data['_id']), 'name': user_data.get('name', 'Builder'),
            'username': user_data.get('username', ''),
            'title': user_data.get('title', 'Developer'), 'about_me': user_data.get('about_me', ''),
            'experience_years': user_data.get('experience_years', '0'), 'current_status': user_data.get('current_status', 'Available'),
            'location': user_data.get('location', 'Remote'), 'education_college': user_data.get('education_college', 'N/A'),
            'education_degree': user_data.get('education_degree', 'N/A'), 'career_goal': user_data.get('career_goal', ''),
            'github_url': user_data.get('github_url', ''), 'linkedin_url': user_data.get('linkedin_url', ''),
            'instagram_url': user_data.get('instagram_url', ''), 'facebook_url': user_data.get('facebook_url', ''),
            'portfolio_url': user_data.get('portfolio_url', ''), 'profile_pic_url': profile_pic_url,
            'known_skills': user_data.get('known_skills', []), 'learning_skills': user_data.get('learning_skills', []),
            'work_experience': user_data.get('work_experience', []),
            'education': user_data.get('education', []),
            'availability': user_data.get('availability', ''),
        }
        xp = user_data.get('xp', 0)
        level_info = get_level_info(xp)
        streak = user_data.get('streak_count', 1)
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
        user_projects = list(projects_collection.find({'created_by_id': obj_id, 'hidden': {'$ne': True}}).sort('created_at', -1))
        for p in user_projects:
            p['_id'] = str(p['_id'])
            p['created_by_id'] = str(p.get('created_by_id', ''))
        return render_template('view_profile.html', profile=user_profile, xp=xp, level_info=level_info, streak=streak, progress_pct=progress_pct, projects=user_projects)
    except Exception as e:
        print(f"Error viewing profile: {e}"); return redirect(url_for('projects'))

@app.route('/messages')
@login_required
def messages_list():
    try:
        u_id = ObjectId(current_user.id)
        pipeline = [
            {"$match": {"$or": [{"sender_id": u_id}, {"receiver_id": u_id}]}},
            {"$sort": {"timestamp": -1}},
            {"$group": {"_id": {"$cond": [{"$eq": ["$sender_id", u_id]}, "$receiver_id", "$sender_id"]}, "last_message": {"$first": "$content"}, "timestamp": {"$first": "$timestamp"}, "is_read": {"$first": "$is_read"}, "last_sender": {"$first": "$sender_id"}}},
            {"$sort": {"timestamp": -1}}
        ]
        results = list(messages_collection.aggregate(pipeline))
        conversations = []
        for res in results:
            other_user = users_collection.find_one({"_id": res["_id"]})
            if not other_user: continue
            sent_by_me = str(res["last_sender"]) == current_user.id
            if sent_by_me:
                sender_name = "Me"
            else:
                sender_user = users_collection.find_one({"_id": res["last_sender"]})
                sender_name = sender_user.get("name", "User") if sender_user else "User"
            is_unread = (not res["is_read"]) and (not sent_by_me)
            conversations.append({"user_id": str(other_user["_id"]), "user_name": other_user.get("name", "Unknown"), "username": other_user.get("username", ""), "profile_pic": other_user.get("profile_pic", "default.jpg"), "last_message": res.get("last_message", ""), "timestamp": fmt_utc(res["timestamp"], "%b %d, %I:%M %p") + " UTC", "timestamp_iso": utc_iso(res["timestamp"]), "is_unread": is_unread, "sent_by_me": sent_by_me, "sender_name": sender_name})
        _u = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1, 'streak_count': 1}) or {}
        _xp = _u.get('xp', 0); _li = get_level_info(_xp)
        _streak = _u.get('streak_count', 0)
        _ppct = int((_xp / _li['next_xp']) * 100) if _li['next_xp'] != "Max" else 100
        return render_template("messages_list.html", conversations=conversations, xp=_xp, level_info=_li, streak=_streak, progress_pct=_ppct)
    except Exception as e:
        print(f"Inbox error: {e}"); return redirect(url_for("main_page"))

@app.route('/chat/<receiver_id>', methods=['GET', 'POST'])
@login_required
@limiter.limit("60 per minute")                           # ← ADDED
def chat(receiver_id):
    try:
        r_id = ObjectId(receiver_id)
        u_id = ObjectId(current_user.id)
        rec = users_collection.find_one({"_id": r_id})
        if not rec:
            flash("User not found.", "error"); return redirect(url_for('messages_list'))
        if request.method == 'POST':
            msg = sanitize(request.form.get('content', ''))  # ← XSS fix
            if msg:
                messages_collection.insert_one({"sender_id": u_id, "receiver_id": r_id, "content": msg, "timestamp": now_ist(), "is_read": False})
                sender_name = current_user.name
                create_notification(str(r_id), 'message', f'{sender_name} sent you a message', url_for('chat', receiver_id=current_user.id))
            return redirect(url_for('chat', receiver_id=receiver_id))
        messages_collection.update_many({"sender_id": r_id, "receiver_id": u_id, "is_read": False}, {"$set": {"is_read": True}})
        history = list(messages_collection.find({"$or": [{"sender_id": u_id, "receiver_id": r_id}, {"sender_id": r_id, "receiver_id": u_id}]}).sort("timestamp", 1))
        for m in history:
            m["timestamp_iso"] = utc_iso(m.get("timestamp"))
        return render_template('chat.html', receiver=rec, messages=history)
    except Exception as e:
        print(f"Chat Error: {e}"); return redirect(url_for('messages_list'))

@app.route('/api/dm/<receiver_id>/messages')
@login_required
def api_dm_messages(receiver_id):
    try:
        r_id = ObjectId(receiver_id)
        u_id = ObjectId(current_user.id)
        since_str = request.args.get('since')
        query = {"$or": [{"sender_id": u_id, "receiver_id": r_id}, {"sender_id": r_id, "receiver_id": u_id}]}
        if since_str:
            try:
                since_dt = as_utc(since_str)
                query["timestamp"] = {"$gt": since_dt}
            except Exception:
                pass
        msgs = list(messages_collection.find(query).sort("timestamp", 1))
        # Mark incoming as read
        messages_collection.update_many({"sender_id": r_id, "receiver_id": u_id, "is_read": False}, {"$set": {"is_read": True}})
        result = []
        for m in msgs:
            ts = m.get("timestamp")
            ts_str = fmt_utc(ts, '%I:%M %p') + ' UTC' if ts else ''
            result.append({
                "id": str(m["_id"]),
                "content": m.get("content", ""),
                "sender_id": str(m["sender_id"]),
                "timestamp_str": ts_str,
                "timestamp_iso": utc_iso(ts) if ts else ""
            })
        return jsonify({"messages": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

