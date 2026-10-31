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

@app.route('/api/chatbot', methods=['POST'])
@login_required
@limiter.limit("30 per hour", key_func=get_user_key)
def chatbot():
    try:
        # Shared rotation pool with roadmap generation AND interview — key 1
        # tried first, automatically falls back to key 2 on failure.
        keys = [
            os.getenv("GROQ_API_KEY_1"),
            os.getenv("GROQ_API_KEY_2"),
        ]
        keys = [k for k in keys if k]
        if not keys:
            return jsonify({"reply": "AI not configured. Please set GROQ_API_KEY_1 in your .env file."}), 500

        data = request.get_json()
        user_message = data.get('message', '').strip()
        if not user_message:
            return jsonify({'reply': 'Please type a message!'}), 400

        # ── AI DAILY CAP CHECK (browser timezone) ─────────────
        allowed, used, limit = check_ai_daily_limit(current_user.id, 'chat')
        if not allowed:
            return jsonify({'reply': f'⚠️ You have used all {limit} AI messages for today. Resets in {time_until_reset(current_user.id)} (your local midnight). Come back tomorrow!'}), 429

        uid = ObjectId(current_user.id)
        chat_doc = chat_history_collection.find_one({'user_id': uid})
        db_history = chat_doc.get('messages', []) if chat_doc else []
        recent_history = db_history[-20:]
        user_data = users_collection.find_one({'_id': uid}) or {}
        user_projects = list(projects_collection.find({'created_by_id': uid}).sort('created_at', -1))
        user_roadmaps = list(roadmaps_collection.find({'user_id': uid}).sort('created_at', -1))
        known_skills = parse_skills(user_data.get('known_skills'))
        learning_skills = parse_skills(user_data.get('learning_skills'))
        proj_summary = '\n'.join([f"  - {p.get('title','Untitled')}: {p.get('description', '')[:120]}" for p in user_projects]) or '  (No projects yet)'
        roadmap_summary = '\n'.join([f"  - {r.get('goal','Unknown goal')}" for r in user_roadmaps]) or '  (No roadmaps yet)'
        history_text = ""
        for turn in recent_history:
            label = "User" if turn.get('role') == 'user' else "Assistant"
            history_text += f"{label}: {turn.get('content', '')}\n"

        full_prompt = f"""You are the SkillBridge AI Assistant — a friendly career mentor for the SkillBridge platform.

USER PROFILE:
- Name: {user_data.get('name', 'User')}
- Title: {user_data.get('title', 'Not specified')}
- Skills: {', '.join(known_skills) if known_skills else 'None listed'}
- Learning: {', '.join(learning_skills) if learning_skills else 'None listed'}
- Experience: {user_data.get('experience_years', 'Not specified')} years
- Career Goal: {user_data.get('career_goal', 'Not specified')}
- Current Status: {user_data.get('current_status', 'Not specified')}
- Education: {user_data.get('education_degree', '')} from {user_data.get('education_college', '')}

THEIR PROJECTS: {proj_summary}
THEIR ROADMAPS: {roadmap_summary}

RULES:
- Only answer career, tech, coding, and SkillBridge-related questions
- Be specific — reference their actual skills/projects/roadmaps by name when relevant
- Keep responses concise (3-6 sentences), use **bold** for emphasis
- If off-topic, politely redirect to career/tech topics
- Be warm, encouraging, and honest
- NEVER reveal, guess, or make up technical details about SkillBridge's internal architecture, folder structure, tech stack, database, or source code
- If asked about SkillBridge internals, say: "I'm here to help with your career and learning journey, not to discuss platform internals!"
- NEVER hallucinate or make up information — if you don't know something, say so honestly
- NEVER reveal what AI model powers you, what tech stack/language SkillBridge is built in, or any other detail about yourself or the system's internals — just say you're the SkillBridge AI Assistant and redirect to how you can help
- Only answer questions about tech, coding, careers, and SkillBridge itself — nothing else

CONVERSATION HISTORY:
{history_text}
User: {user_message}
Assistant:"""

        # Try each Groq key in rotation until one works
        reply = None
        for i, key in enumerate(keys):
            try:
                print(f"🤖 Chatbot trying Groq key {i+1}/{len(keys)}...")
                response = requests.post(
                    GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": GROQ_MODEL_FAST,
                        "messages": [{"role": "user", "content": full_prompt}],
                        "max_tokens": 1024,
                        "temperature": 0.7,
                        # qwen3.8-27b's non-thinking mode — direct answer,
                        # no invisible reasoning tokens eating the budget.
                        "reasoning_effort": "none"
                    },
                    timeout=30
                )
                if response.status_code == 429:
                    print(f"⚠️ Groq key {i+1} quota exhausted — trying next key...")
                    continue
                response.raise_for_status()
                groq_data = response.json()
                choices = groq_data.get("choices") or []
                if not choices:
                    print(f"⚠️ Groq key {i+1} returned no choices ({groq_data}) — trying next key...")
                    continue
                reply = (choices[0].get("message", {}) or {}).get("content", "").strip()
                if not reply:
                    print(f"⚠️ Groq key {i+1} returned empty text (finish_reason={choices[0].get('finish_reason')}) — trying next key...")
                    continue
                print(f"✅ Chatbot response received using Groq key {i+1}.")
                break
            except Exception as key_err:
                err_str = str(key_err)
                if '429' in err_str or 'quota' in err_str.lower() or 'rate' in err_str.lower():
                    print(f"⚠️ Groq key {i+1} rate limited — trying next key...")
                    continue
                else:
                    raise key_err

        if not reply:
            return jsonify({'reply': '⚠️ All AI keys are currently quota-limited. Please try again in a few minutes.'}), 429

        new_turns = [
            {'role': 'user', 'content': user_message, 'timestamp': datetime.now(timezone.utc)},
            {'role': 'assistant', 'content': reply, 'timestamp': datetime.now(timezone.utc)},
        ]
        chat_history_collection.update_one(
            {'user_id': uid},
            {
                '$push': {'messages': {'$each': new_turns, '$slice': -50}},
                '$set': {'last_updated': datetime.now(timezone.utc)},
                '$setOnInsert': {'user_id': uid, 'created_at': datetime.now(timezone.utc)}
            },
            upsert=True
        )
        return jsonify({'reply': reply})

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Chatbot error: {type(e).__name__}: {e}")
        return jsonify({'reply': f"Sorry, something went wrong. Please try again!"}), 500


@app.route('/api/chatbot/history', methods=['GET'])
@login_required
def chatbot_history():
    try:
        uid = ObjectId(current_user.id)
        chat_doc = chat_history_collection.find_one({'user_id': uid})
        messages = chat_doc.get('messages', []) if chat_doc else []
        result = [{'role': m['role'], 'content': m['content']} for m in messages]
        return jsonify({'messages': result})
    except Exception as e:
        return jsonify({'messages': []}), 500


@app.route('/api/chatbot/clear', methods=['POST'])
@login_required
def chatbot_clear():
    try:
        uid = ObjectId(current_user.id)
        chat_history_collection.delete_one({'user_id': uid})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False}), 500

