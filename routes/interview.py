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

INTERVIEW_DIFF_LADDER = ['easy', 'medium', 'hard']


def compute_adaptive_difficulty(base_difficulty, history):
    """Adaptive difficulty, computed fresh from the passed-in history every call —
    no extra session state needed. Walks every past 'Score: X/10' line found in
    assistant turns and shifts the ladder up/down a notch (capped at both ends):
    score >= 8 -> harder, score < 5 -> easier, else unchanged."""
    base_difficulty = base_difficulty if base_difficulty in INTERVIEW_DIFF_LADDER else 'medium'
    idx = INTERVIEW_DIFF_LADDER.index(base_difficulty)
    for turn in history or []:
        if turn.get('role') != 'assistant':
            continue
        m = re.search(r'Score:\s*(\d+(?:\.\d+)?)\s*/\s*10', turn.get('content', '') or '', re.IGNORECASE)
        if not m:
            continue
        score = float(m.group(1))
        if score >= 8 and idx < len(INTERVIEW_DIFF_LADDER) - 1:
            idx += 1
        elif score < 5 and idx > 0:
            idx -= 1
    return INTERVIEW_DIFF_LADDER[idx]


def call_interview_ai(prompt, max_tokens=400):
    """Same shared rotation pool as the chatbot/roadmap generator — key 1
    tried first, automatically falls back to key 2 on failure."""
    keys = [
        os.getenv("GROQ_API_KEY_1"),
        os.getenv("GROQ_API_KEY_2"),
    ]
    keys = [k for k in keys if k]
    if not keys:
        return None
    for i, key in enumerate(keys):
        try:
            print(f"🎤 Interview AI trying Groq key {i+1}/{len(keys)}...")
            response = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL_FAST,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                    # qwen3.8-27b's non-thinking mode — direct answer, no
                    # invisible reasoning tokens eating the max_tokens budget
                    # (this is what gpt-oss couldn't do — "low" was its floor).
                    "reasoning_effort": "none"
                },
                timeout=30
            )
            if response.status_code == 429:
                print(f"⚠️ Interview Groq key {i+1} quota exhausted — trying next key...")
                continue
            response.raise_for_status()
            groq_data = response.json()
            choices = groq_data.get("choices") or []
            if not choices:
                print(f"⚠️ Interview Groq key {i+1} returned no choices ({groq_data}) — trying next key...")
                continue
            text = (choices[0].get("message", {}) or {}).get("content", "").strip()
            if not text:
                print(f"⚠️ Interview Groq key {i+1} returned empty text (finish_reason={choices[0].get('finish_reason')}) — trying next key...")
                continue
            return text
        except Exception as key_err:
            err_str = str(key_err)
            if '429' in err_str or 'quota' in err_str.lower() or 'rate' in err_str.lower():
                print(f"⚠️ Interview Groq key {i+1} rate limited — trying next key...")
                continue
            else:
                raise key_err
    return None


def get_interview_profile_context(user_id):
    """'Use my Profile' personalization — same data shape the chatbot already
    pulls (skills, projects, roadmaps), kept short so it doesn't eat tokens."""
    uid = ObjectId(user_id)
    user_data = users_collection.find_one({'_id': uid}) or {}
    user_projects = list(projects_collection.find({'created_by_id': uid}).sort('created_at', -1).limit(6))
    user_roadmaps = list(roadmaps_collection.find({'user_id': uid}).sort('created_at', -1).limit(4))
    known_skills = parse_skills(user_data.get('known_skills'))
    learning_skills = parse_skills(user_data.get('learning_skills'))
    proj_summary = '\n'.join(
        f"  - {p.get('title','Untitled')}: {(p.get('description') or '')[:160]}"
        for p in user_projects
    ) or '  (No projects yet)'
    roadmap_summary = '\n'.join(f"  - {r.get('goal','Unknown goal')}" for r in user_roadmaps) or '  (No roadmaps yet)'
    return f"""CANDIDATE PROFILE (use this to personalize questions where it genuinely fits — don't force it into every question):
- Name: {user_data.get('name', 'Candidate')}
- Title/Goal: {user_data.get('title') or user_data.get('career_goal') or 'Not specified'}
- Skills: {', '.join(known_skills) if known_skills else 'None listed'}
- Currently Learning: {', '.join(learning_skills) if learning_skills else 'None listed'}
- Experience: {user_data.get('experience_years', 'Not specified')} years
- Projects:
{proj_summary}
- Roadmaps/Goals:
{roadmap_summary}"""


def build_interview_question_prompt(mode, role, difficulty, q_number, total_q, is_first_question, personalization_ctx):
    """System prompt for generating the NEXT interview question (no scoring here)."""
    if mode == 'technical':
        focus = ("DSA problems, coding logic, SQL, ML/CS concepts, or real-world technical scenarios "
                  "appropriate for the role — answerable in plain text (no need for a code editor).")
    else:
        focus = ("a mix of behavioral, project-based, and HR-style questions of the kind top companies ask "
                  "(e.g. 'tell me about yourself', handling conflict/failure, why this role, strengths & "
                  "weaknesses, or a deep-dive on one of the candidate's own projects/tool choices if profile "
                  "context is given below).")

    opener = (
        "This is the FIRST question of this round — open with a short, natural one-line greeting "
        "before asking it."
        if is_first_question else
        "This is a FOLLOW-UP question — do NOT greet again, just ask it naturally, as a real interviewer would."
    )

    profile_block = f"\n\n{personalization_ctx}" if personalization_ctx else (
        "\n\nNo profile personalization requested for this session — keep questions purely role/difficulty based."
    )

    return f"""You are a professional interviewer conducting a {difficulty}-difficulty {'technical' if mode == 'technical' else 'behavioral/HR'} round for a {role} candidate.

This is question {q_number} of {total_q} in this round. Ask exactly ONE interview question focused on {focus}
{opener}
Never repeat a question already asked earlier in this conversation.{profile_block}

Output ONLY the question text (with the brief greeting if instructed above). No "Question:" labels, no numbering, no markdown headers."""


def build_interview_eval_prompt(mode, role, difficulty, is_final, personalization_ctx, allow_followup=False):
    """System prompt for evaluating the candidate's last answer."""
    rubric = (
        "correctness, depth of reasoning, edge-case awareness, and clarity of explanation" if mode == 'technical'
        else "clarity of communication, structure (e.g. STAR method), confidence, and relevance to the question"
    )
    closing = ""
    if is_final:
        closing = ("\n\nThis is the LAST question of the session. After scoring this answer, also add a short "
                   "closing summary (3-5 sentences) covering: overall strengths shown across the round, the "
                   "single biggest area to improve, and a readiness-style verdict (Interview Ready / Good — "
                   "Keep Practicing / Needs Improvement / More Preparation Recommended) for this round. "
                   "Never phrase this as a hiring decision (avoid words like 'hire').")

    followup_rule = ""
    if allow_followup and not is_final:
        followup_rule = ("\n\nFOLLOW-UP RULE: If — and only if — this answer scores between 5 and 7.9 out of 10 "
                          "(shows partial understanding but is incomplete, vague, or could go deeper), add ONE "
                          "short, natural clarifying follow-up question a real interviewer would ask to probe "
                          "this same answer further. Put it on its own line after the exact marker "
                          "'---FOLLOWUP---' with nothing else on that marker line. If the score is 8 or above, "
                          "or below 5, do NOT include a follow-up or the marker at all — just move on.")

    profile_block = f"\n\n{personalization_ctx}" if personalization_ctx else ""

    return f"""You are evaluating a candidate's answer in a {difficulty}-difficulty {'technical' if mode == 'technical' else 'behavioral/HR'} interview round for a {role} candidate.

Judge the answer on: {rubric}.

Respond in EXACTLY this format:
Score: X/10
<2-4 sentence feedback — be specific and honest about what was good and what to improve>{closing}{followup_rule}

Do not ask a new question here unless the FOLLOW-UP RULE above applies. Do not add any other headers or labels.{profile_block}"""


@app.route('/interview')
@login_required
def interview_room():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}) or {}
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
    return render_template('interview_room.html',
        xp=xp,
        level_info=level_info,
        streak=streak,
        progress_pct=progress_pct,
    )


@app.route('/interview/history')
@login_required
def interview_history():
    try:
        uid = ObjectId(current_user.id)
        sessions = list(
            interview_sessions_collection.find({'user_id': uid})
            .sort('created_at', -1)
            .limit(30)
        )
        result = []
        for s in sessions:
            created = s.get('created_at')
            result.append({
                'role': s.get('role', ''),
                'mode': s.get('mode', ''),
                'answer_method': s.get('answer_method', 'written'),
                'difficulty': s.get('difficulty', ''),
                'score': s.get('score', 0),
                'created_at': created.isoformat() if isinstance(created, datetime) else str(created or ''),
                'report': s.get('report', ''),
                'history': s.get('history', []),
            })
        return jsonify({'sessions': result})
    except Exception as e:
        print(f"Interview history error: {e}")
        return jsonify({'sessions': []}), 500


@app.route('/interview/ask', methods=['POST'])
@login_required
@limiter.limit("60 per hour", key_func=get_user_key)
def interview_ask():
    try:
        data = request.get_json() or {}
        role = sanitize(data.get('role', '') or 'Software Engineer')[:80]
        # 'communication' kept as an accepted legacy alias so older frontend builds
        # (which still send it) keep working; normalized to 'behavioral' internally.
        mode = data.get('mode') if data.get('mode') in ('technical', 'behavioral', 'communication') else 'technical'
        if mode == 'communication':
            mode = 'behavioral'
        base_difficulty = data.get('difficulty') if data.get('difficulty') in INTERVIEW_DIFF_LADDER else 'medium'
        history = data.get('history') or []
        if not isinstance(history, list):
            history = []
        history = history[-20:]  # cap, same spirit as the chatbot's recent_history
        user_answer = (data.get('user_answer') or '').strip()
        try:
            total_q = int(data.get('total_q') or 6)
        except (TypeError, ValueError):
            total_q = 6
        total_q = max(3, min(total_q, 15))  # clamp: no runaway/degenerate session lengths
        try:
            q_number = int(data.get('q_number') or 1)
        except (TypeError, ValueError):
            q_number = 1
        q_number = max(1, min(q_number, total_q))
        personalization = data.get('personalization', 'generic')
        is_first_question = bool(data.get('is_first_question'))
        # True only when this call is evaluating the candidate's answer to a
        # follow-up question we ourselves generated — caps follow-ups at 1 per
        # question by telling the eval prompt not to offer another one.
        is_followup_answer = bool(data.get('is_followup_answer'))

        # ── PER-INTERVIEW DAILY CAP (browser timezone) ─────────────
        # A NEW interview (first question) consumes 1 of the daily interviews.
        # Follow-up questions/evaluations inside that interview are free.
        if is_first_question:
            allowed, used, limit = check_ai_daily_limit(current_user.id, 'interview_sessions', limit=INTERVIEW_SESSIONS_PER_DAY)
            if not allowed:
                return jsonify({'reply': f'⚠️ You have used all {limit} interviews for today. Resets in {time_until_reset(current_user.id)} (your local midnight). Come back tomorrow!'}), 429

        # Hidden abuse backstop (not user-facing, see INTERVIEW_CALL_SAFETY_CAP)
        safe_ok, _, _ = check_ai_daily_limit(current_user.id, 'interview', limit=INTERVIEW_CALL_SAFETY_CAP)
        if not safe_ok:
            return jsonify({'reply': f'⚠️ Too many interview requests today. Resets in {time_until_reset(current_user.id)} (your local midnight).'}), 429

        personalization_ctx = get_interview_profile_context(current_user.id) if personalization == 'profile' else ''
        difficulty = compute_adaptive_difficulty(base_difficulty, history)

        history_text = ""
        for turn in history:
            label = 'Interviewer' if turn.get('role') == 'assistant' else 'Candidate'
            history_text += f"{label}: {turn.get('content', '')}\n"

        if user_answer:
            # Evaluating the candidate's last answer — no new question here
            is_final = q_number >= total_q
            # Only offer a follow-up when this isn't itself a follow-up answer
            # (caps it at one per question) and it isn't the closing question.
            allow_followup = (not is_final) and (not is_followup_answer)
            system_prompt = build_interview_eval_prompt(mode, role, difficulty, is_final, personalization_ctx, allow_followup=allow_followup)
            full_prompt = f"{system_prompt}\n\nCONVERSATION SO FAR:\n{history_text}\nCandidate's answer to evaluate: {user_answer}\n\nYour evaluation:"
            # qwen3.8-27b non-thinking mode doesn't need the inflated
            # gpt-oss-era headroom — back to Mistral-scale budgets.
            max_tokens = 450 if is_final else (330 if allow_followup else 280)
        else:
            # Generating the next (or first) question
            system_prompt = build_interview_question_prompt(mode, role, difficulty, q_number, total_q, is_first_question, personalization_ctx)
            full_prompt = f"{system_prompt}\n\nCONVERSATION SO FAR:\n{history_text}\nYour next question:"
            max_tokens = 220

        reply = call_interview_ai(full_prompt, max_tokens=max_tokens)
        if not reply:
            return jsonify({'reply': '⚠️ All AI keys are currently quota-limited. Please try again in a few minutes.'}), 429

        score = None
        m = re.search(r'Score:\s*(\d+(?:\.\d+)?)\s*/\s*10', reply, re.IGNORECASE)
        if m:
            score = float(m.group(1))

        # Split out a follow-up question if the model included one, but never
        # trust it blindly — only honor it when we actually allowed a follow-up
        # for this turn AND the score genuinely falls in the 5–7.9 band. This
        # protects the "max 1 follow-up per question" cap even if the model
        # ignores the instruction.
        followup = None
        if user_answer and '---FOLLOWUP---' in reply:
            main_part, _, followup_part = reply.partition('---FOLLOWUP---')
            reply = main_part.strip()
            followup_candidate = followup_part.strip()
            if allow_followup and followup_candidate and score is not None and 5 <= score < 8:
                followup = followup_candidate

        return jsonify({'reply': reply, 'difficulty': difficulty, 'score': score, 'followup': followup})

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Interview ask error: {type(e).__name__}: {e}")
        return jsonify({'reply': 'Sorry, something went wrong generating that. Please try again!'}), 500


@app.route('/interview/save', methods=['POST'])
@login_required
def interview_save():
    try:
        data = request.get_json() or {}
        role = sanitize(data.get('role', ''))[:80]
        # 'communication' kept as a legacy alias, normalized to 'behavioral'.
        mode = data.get('mode') if data.get('mode') in ('technical', 'behavioral', 'communication', 'full') else 'technical'
        if mode == 'communication':
            mode = 'behavioral'
        difficulty = data.get('difficulty') if data.get('difficulty') in INTERVIEW_DIFF_LADDER else 'medium'
        answer_method = data.get('answer_method') if data.get('answer_method') in ('written', 'voice', 'both') else 'written'
        try:
            score = round(float(data.get('score', 0)), 1)
        except (TypeError, ValueError):
            score = 0.0
        history = data.get('history') or []
        if not isinstance(history, list):
            history = []
        report = (data.get('report') or '')[:4000]

        uid = ObjectId(current_user.id)
        interview_sessions_collection.insert_one({
            'user_id': uid,
            'role': role,
            'mode': mode,
            'answer_method': answer_method,
            'difficulty': difficulty,
            'score': score,
            'history': history[-30:],
            'report': report,
            'created_at': now_ist(),
        })

        # NOTE: XP is awarded separately by /interview/complete, the moment the
        # interview reaches 100% completion — independent of whether the user
        # later chooses to Save or Discard the report from history.
        return jsonify({'success': True})
    except Exception as e:
        print(f"Interview save error: {e}")
        return jsonify({'success': False}), 500


@app.route('/interview/complete', methods=['POST'])
@login_required
def interview_complete():
    """Called automatically the moment a session reaches 100% completion
    (all questions answered) — awards XP regardless of the later Save/Discard
    choice on the report screen. Separate from /interview/save on purpose."""
    try:
        add_xp(current_user.id, 50, "Mock Interview Completed")
        return jsonify({'success': True})
    except Exception as e:
        print(f"Interview complete (XP) error: {e}")
        return jsonify({'success': False}), 500

