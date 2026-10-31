import json
import os
import random
import re
import requests
import secrets
import pytz
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
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
from extensions import (app, limiter, bcrypt, client, db, s, login_manager, scheduler, UTC_TZ,
                        DEFAULT_USER_TZ, users_collection, roadmaps_collection, projects_collection,
                        commits_collection, messages_collection, communities_collection,
                        community_messages_collection, chat_history_collection, notifications_collection,
                        xp_history_collection, comments_collection, interview_sessions_collection,
                        deleted_accounts_collection)
from services.time_utils import (now_ist, now_utc, get_current_week_start_utc, get_next_weekly_reset_utc,
                                 as_utc, utc_iso, fmt_utc, get_timezone, user_timezone_name,
                                 user_today_key, to_ist, fmt_ist)
from services.utils import (get_login_key, get_user_key, sanitize, flatten_data, TEMPLATE_NAMES, parse_skills,
                            safe_experience_years, clean_cert_payload, clean_social_url)
from services.email_service import (app_base_url, make_unsubscribe_token, unsubscribe_url_for,
                                    send_email, send_reminder_digest_email)
from services.xp import (get_level_info, add_xp, check_and_award_profile_complete, recalculate_xp,
                         deduct_xp, update_streak)
from services.notifications import create_notification
from services.ai_limits import (INTERVIEW_SESSIONS_PER_DAY, INTERVIEW_CALL_SAFETY_CAP, time_until_reset,
                                check_ai_daily_limit, get_ai_usage_today)
from services.account_service import (is_sole_active_admin, sync_hidden_flag, log_account_deletion,
                                      purge_expired_accounts)

def _safe_int(value, default=0, lo=0, hi=None):
    """int() that never raises and always stays inside [lo, hi]."""
    try:
        if value is None or isinstance(value, bool):
            return default
        number = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return default
    number = max(lo, number)
    return min(number, hi) if hi is not None else number


@app.route('/profile', methods=['GET'])
@login_required
def profile():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    if not user_data:                       # account was deleted while the session was still open
        logout_user()
        return redirect(url_for('login'))
    experience_years = safe_experience_years(user_data.get('experience_years'))
    profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
    profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)
    user_profile = {
        'name': user_data.get('name', ''), 'email': user_data.get('email', ''),
        'title': user_data.get('title', ''), 'about_me': user_data.get('about_me', ''),
        'location': user_data.get('location', ''), 'github_url': user_data.get('github_url', ''),
        'linkedin_url': user_data.get('linkedin_url', ''), 'instagram_url': user_data.get('instagram_url', ''),
        'facebook_url': user_data.get('facebook_url', ''), 'education_college': user_data.get('education_college', ''),
        'education_degree': user_data.get('education_degree', ''), 'experience_years': str(experience_years),
        'education': user_data.get('education', []),
        'current_status': user_data.get('current_status', ''), 'availability': user_data.get('availability', ''),
        'career_goal': user_data.get('career_goal', ''),
        'known_skills_str': ', '.join(user_data.get('known_skills', [])),
        'learning_skills_str': ', '.join(user_data.get('learning_skills', [])),
        'profile_pic_url': profile_pic_url, 'certificates': user_data.get('certificates', []),
        'github_username': user_data.get('github_username', ''), 'github_avatar': user_data.get('github_avatar', ''),
        'work_experience': user_data.get('work_experience', []),
        'github_repos': user_data.get('github_repos', []), 'github_langs': user_data.get('github_langs', []),
        'github_followers': _safe_int(user_data.get('github_followers')), 'github_public_repos': _safe_int(user_data.get('github_public_repos')),
        'github_synced_at': str(user_data.get('github_synced_at', ''))
    }
    xp = _safe_int(user_data.get('xp'))
    level_info = get_level_info(xp)
    streak = _safe_int(user_data.get('streak_count'))
    progress_pct = min(100, max(0, int((xp / level_info['next_xp']) * 100))) if level_info['next_xp'] != "Max" else 100
    return render_template('profile.html', user=user_profile, xp=xp, level_info=level_info, streak=streak, progress_pct=progress_pct)



ALLOWED_PIC_TYPES = {
    '.png': (b'\x89PNG\r\n\x1a\n',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
    '.gif': (b'GIF87a', b'GIF89a'), '.webp': (b'RIFF',),
}
MAX_PROFILE_PIC_BYTES = 2 * 1024 * 1024


def _check_profile_pic(file):
    """Returns (extension, None) when the upload is a real image <= 2 MB, else (None, message)."""
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in ALLOWED_PIC_TYPES:
        shown = ext if ext else 'no extension'
        return None, f'Profile image must be a PNG, JPG, GIF or WEBP file (you chose: {shown}).'
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return None, 'The selected profile image is empty. Please choose another file.'
    if size > MAX_PROFILE_PIC_BYTES:
        return None, f'Profile image must be 2 MB or smaller (yours is {size / 1048576:.1f} MB).'
    head = file.stream.read(12)
    file.stream.seek(0)
    ok = any(head.startswith(sig) for sig in ALLOWED_PIC_TYPES[ext])
    if ext == '.webp':
        ok = ok and head[8:12] == b'WEBP'
    if not ok:
        return None, 'That file is not a valid image. Please choose a real PNG, JPG, GIF or WEBP picture.'
    return ext, None


@app.route('/profile/save/identity', methods=['POST'])
@login_required
def profile_save_identity():
    try:
        name = sanitize(request.form.get('name', '').strip())
        title = sanitize(request.form.get('title', '').strip())
        location = sanitize(request.form.get('location', '').strip())
        about_me = sanitize(request.form.get('about_me', '').strip())

        # Blank card is fine. If any field is filled, all three are required together.
        any_filled = name or title or location
        if any_filled and not (name and title and location):
            return jsonify({'success': False, 'error': 'Name, Job Title, and Location are all required when any one is filled.'}), 400

        update_data = {'name': name, 'title': title, 'location': location, 'about_me': about_me}

        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '':
                f_ext, pic_error = _check_profile_pic(file)
                if pic_error:
                    return jsonify({'success': False, 'error': pic_error}), 400
                random_hex = secrets.token_hex(8)
                profile_pic_fn = random_hex + f_ext
                file.save(os.path.join(app.root_path, 'static/profile_pics', profile_pic_fn))
                update_data['profile_pic'] = profile_pic_fn

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        reward_granted = check_and_award_profile_complete(current_user.id)
        profile_pic_url = None
        if 'profile_pic' in update_data:
            profile_pic_url = url_for('static', filename='profile_pics/' + update_data['profile_pic'])
        return jsonify({'success': True, 'reward_granted': reward_granted, 'profile_pic_url': profile_pic_url})
    except Exception as e:
        print(f"Error saving identity card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/profile/save/skills', methods=['POST'])
@login_required
def profile_save_skills():
    try:
        old_user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        old_learning_skills = set(old_user_data.get('learning_skills', []))

        known_skills = [sanitize(s.strip()) for s in request.form.get('known_skills', '').split(',') if s.strip()]
        new_learning_skills_list = [sanitize(s.strip()) for s in request.form.get('learning_skills', '').split(',') if s.strip()]

        # Both known and learning are independent — blank is allowed for each.

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {
            'known_skills': known_skills, 'learning_skills': new_learning_skills_list
        }})
        reward_granted = check_and_award_profile_complete(current_user.id)

        added_goal = None
        new_learning_skills = set(new_learning_skills_list)
        added_skills = list(new_learning_skills - old_learning_skills)
        if added_skills:
            added_goal = added_skills[0]

        return jsonify({
            'success': True, 'reward_granted': reward_granted,
            'added_goal': added_goal,
            'roadmap_url': url_for('roadmap_generator', goal=added_goal) if added_goal else None
        })
    except Exception as e:
        print(f"Error saving skills card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


def _bad(message):
    return jsonify({'success': False, 'error': message}), 400


def _text_check(value, label, lo, hi, prefix=''):
    value = sanitize(str(value if value is not None else '')).strip()
    if not value:
        return value, f'{prefix}{label} is required.'
    if len(value) < lo:
        return value, f'{prefix}{label} must be at least {lo} characters (you typed {len(value)}).'
    if len(value) > hi:
        return value, f'{prefix}{label} must be at most {hi} characters (you typed {len(value)}).'
    return value, None


def _year_check(raw, label, lo, hi, prefix=''):
    raw = str(raw if raw is not None else '').strip()
    if not raw:
        return None, f'{prefix}{label} is required.'
    if not re.fullmatch(r'\d{4}', raw):
        return None, f'{prefix}{label} must be a 4-digit year between {lo} and {hi} (you entered "{raw[:10]}").'
    year = int(raw)
    if year < lo or year > hi:
        return None, f'{prefix}{label} must be between {lo} and {hi} (you entered {year}).'
    return year, None


# Allowed ranges (keep in sync with RULES in templates/profile.html)
EXPERIENCE_YEARS_RANGE = (0, 50)
EDU_MAX_ENTRIES, WORK_MAX_ENTRIES = 10, 15
EDU_GRAD_YEARS_AHEAD = 8          # expected graduation year may be up to 8 years ahead
MIN_CALENDAR_YEAR = 1980
WORK_MAX_BULLETS, WORK_BULLET_MAX_LEN = 10, 300


@app.route('/profile/save/career', methods=['POST'])
@login_required
def profile_save_career():
    try:
        current_status = sanitize(request.form.get('current_status', '').strip())
        raw = (request.form.get('experience_years') or '').strip() or '0'
        lo, hi = EXPERIENCE_YEARS_RANGE
        if not re.fullmatch(r'-?\d+(\.\d+)?', raw):
            return _bad(f'Experience must be a number of years between {lo} and {hi} (you entered "{raw[:10]}").')
        value = Decimal(raw)
        if value != value.to_integral_value():
            return _bad(f'Experience must be a whole number of years (you entered {raw[:10]}). Use 0 if you have less than a year.')
        exp_years = int(value)
        if exp_years < lo or exp_years > hi:
            return _bad(f'Experience must be between {lo} and {hi} years (you entered {exp_years}).')
        # Blank career card is fine — no forced status.
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {
            'current_status': current_status, 'experience_years': str(exp_years)
        }})
        reward_granted = check_and_award_profile_complete(current_user.id)
        return jsonify({'success': True, 'reward_granted': reward_granted})
    except Exception as e:
        print(f"Error saving career card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/profile/save/education', methods=['POST'])
@login_required
def profile_save_education():
    try:
        try:
            edu_entries = json.loads(request.form.get('education_json', '[]'))
        except ValueError:
            return _bad('Education data could not be read. Please refresh the page and try again.')
        if not isinstance(edu_entries, list):
            return _bad('Education entries must be a list.')

        # 0 entries = blank card = allowed — user cleared everything.
        if not edu_entries:
            users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'education': []}})
            reward_granted = check_and_award_profile_complete(current_user.id)
            return jsonify({'success': True, 'reward_granted': reward_granted})
        if len(edu_entries) > EDU_MAX_ENTRIES:
            return _bad(f'You can add at most {EDU_MAX_ENTRIES} education entries (you sent {len(edu_entries)}).')

        clean_edu_entries = []
        current_year = datetime.now().year
        for idx, e in enumerate(edu_entries, 1):
            p = f'Education #{idx}: '
            if not isinstance(e, dict):
                return _bad(p + 'entry is not valid.')
            institution, err = _text_check(e.get('institution'), 'Institution', 2, 120, p)
            if err: return _bad(err)
            degree, err = _text_check(e.get('degree'), 'Degree', 2, 80, p)
            if err: return _bad(err)
            field, err = _text_check(e.get('field'), 'Field of study', 2, 80, p)
            if err: return _bad(err)

            grade_type = sanitize(str(e.get('grade_type', '') or '')) or 'CGPA'
            if grade_type not in ('CGPA', 'Percentage'):
                return _bad(p + 'Grade type must be CGPA or Percentage.')
            grade_value = str(e.get('grade_value', '') or '').strip()
            if not grade_value:
                return _bad(p + 'Grade is required.')
            if grade_value.startswith('-'):
                return _bad(f'{p}Grade cannot be negative (you entered {grade_value[:10]}). CGPA must be between 0 and 10, Percentage between 0 and 100.')
            if not re.fullmatch(r'\d+(\.\d{1,2})?', grade_value):
                return _bad(f'{p}Grade must be a number like 8.7 or 85.25 (max 2 decimal places); you entered "{grade_value[:10]}".')
            grade_max = 100 if grade_type == 'Percentage' else 10
            if Decimal(grade_value) > grade_max:
                return _bad(f'{p}{grade_type} must be between 0 and {grade_max} (you entered {grade_value}).')

            is_current = e.get('is_current', False)
            if not isinstance(is_current, bool):
                return _bad(p + 'Currently studying must be true or false.')
            start_year, err = _year_check(e.get('from_year'), 'From Year', MIN_CALENDAR_YEAR, current_year, p)
            if err: return _bad(err)
            if is_current:
                to_year = str(current_year)
            else:
                end_year, err = _year_check(e.get('to_year'), 'To Year', start_year,
                                            current_year + EDU_GRAD_YEARS_AHEAD, p)
                if err: return _bad(err)
                to_year = str(end_year)

            clean_edu_entries.append({
                'institution': institution, 'degree': degree, 'field': field,
                'grade_type': grade_type, 'grade_value': grade_value,
                'from_year': str(start_year), 'to_year': to_year, 'is_current': is_current
            })

        update_data = {'education': clean_edu_entries}
        primary_edu = clean_edu_entries[0]
        update_data['education_college'] = primary_edu['institution']
        update_data['education_degree'] = primary_edu['degree']
        update_data['education_field'] = primary_edu['field']
        suffix = '%' if primary_edu['grade_type'] == 'Percentage' else ' CGPA'
        update_data['education_grade'] = f"{primary_edu['grade_value']}{suffix}"

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        reward_granted = check_and_award_profile_complete(current_user.id)
        return jsonify({'success': True, 'reward_granted': reward_granted})
    except Exception as e:
        print(f"Error saving education card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/profile/save/experience', methods=['POST'])
@login_required
def profile_save_experience():
    try:
        try:
            exp_entries = json.loads(request.form.get('work_experience_json', '[]'))
        except ValueError:
            return _bad('Experience data could not be read. Please refresh the page and try again.')
        if not isinstance(exp_entries, list):
            return _bad('Experience entries must be a list.')

        # 0 entries = blank card = allowed — no one is forced to have experience.
        if not exp_entries:
            users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'work_experience': []}})
            reward_granted = check_and_award_profile_complete(current_user.id)
            return jsonify({'success': True, 'reward_granted': reward_granted})
        if len(exp_entries) > WORK_MAX_ENTRIES:
            return _bad(f'You can add at most {WORK_MAX_ENTRIES} experience entries (you sent {len(exp_entries)}).')

        clean_entries = []
        current_year = datetime.now().year
        for idx, e in enumerate(exp_entries, 1):
            p = f'Experience #{idx}: '
            if not isinstance(e, dict):
                return _bad(p + 'entry is not valid.')
            company, err = _text_check(e.get('company'), 'Company', 2, 100, p)
            if err: return _bad(err)
            role, err = _text_check(e.get('role'), 'Role', 2, 80, p)
            if err: return _bad(err)
            is_current = e.get('is_current', False)
            if not isinstance(is_current, bool):
                return _bad(p + 'Currently working must be true or false.')
            start_year, err = _year_check(e.get('from_year'), 'From Year', MIN_CALENDAR_YEAR, current_year, p)
            if err: return _bad(err)
            if is_current:
                to_year = str(current_year)
            else:
                end_year, err = _year_check(e.get('to_year'), 'To Year', start_year, current_year, p)
                if err: return _bad(err)
                to_year = str(end_year)

            raw_bullets = e.get('bullets', [])
            if not isinstance(raw_bullets, list):
                raw_bullets = []
            bullets = [str(b).strip() for b in raw_bullets if isinstance(b, str) and b.strip()]
            if len(bullets) > WORK_MAX_BULLETS:
                return _bad(f'{p}at most {WORK_MAX_BULLETS} achievements are allowed (you added {len(bullets)}).')
            for n, bullet in enumerate(bullets, 1):
                if len(bullet) > WORK_BULLET_MAX_LEN:
                    return _bad(f'{p}achievement #{n} must be at most {WORK_BULLET_MAX_LEN} characters (you typed {len(bullet)}).')
            clean_bullets = [sanitize(b) for b in bullets]

            clean_entries.append({
                'company': company, 'role': role, 'from_year': str(start_year),
                'to_year': to_year, 'is_current': is_current, 'bullets': clean_bullets
            })

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'work_experience': clean_entries}})
        reward_granted = check_and_award_profile_complete(current_user.id)
        return jsonify({'success': True, 'reward_granted': reward_granted})
    except Exception as e:
        print(f"Error saving experience card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/profile/save/social', methods=['POST'])
@login_required
def profile_save_social():
    try:
        update_data = {}
        for platform in ('linkedin', 'github', 'instagram', 'facebook'):
            clean_url, url_error = clean_social_url(request.form.get(f'{platform}_url', ''), platform)
            if url_error:
                return _bad(url_error)
            update_data[f'{platform}_url'] = clean_url
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        reward_granted = check_and_award_profile_complete(current_user.id)
        return jsonify({'success': True, 'reward_granted': reward_granted})
    except Exception as e:
        print(f"Error saving social card: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/certificates/add', methods=['POST'])
@login_required
@limiter.limit("20 per hour", key_func=get_user_key)      # ← UPDATED: per-account key
def add_certificate():
    try:
        clean, error = clean_cert_payload(request.get_json(silent=True))
        if error:
            return jsonify({'success': False, 'error': error}), 400
        cert = dict(clean, _id=ObjectId(), added_at=now_ist(), xp_awarded=False)
        result = users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$push': {'certificates': cert}})
        if not result.matched_count:
            return jsonify({'success': False, 'error': 'Account not found.'}), 404
        # xp_awarded is flipped to True ONLY if the XP was really credited, so delete
        # can never deduct XP that was never given.
        if add_xp(current_user.id, 15, "Added a Certificate"):
            users_collection.update_one(
                {'_id': ObjectId(current_user.id), 'certificates._id': cert['_id']},
                {'$set': {'certificates.$.xp_awarded': True}})
            cert['xp_awarded'] = True
        cert['_id'] = str(cert['_id']); cert['added_at'] = cert['added_at'].isoformat()
        return jsonify({'success': True, 'certificate': cert, 'xp_awarded': cert['xp_awarded']})
    except Exception as e:
        print(f"Add certificate error: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/certificates/delete/<cert_id>', methods=['DELETE'])
@login_required
def delete_certificate(cert_id):
    try:
        user_id = ObjectId(current_user.id)
        try:
            certificate_id = ObjectId(cert_id)
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid certificate id.'}), 400
        user = users_collection.find_one(
            {'_id': user_id, 'certificates._id': certificate_id}, {'certificates': 1})
        if not user:
            return jsonify({'success': False, 'error': 'Certificate not found.'}), 404
        certificate = next((i for i in user.get('certificates', []) if i.get('_id') == certificate_id), None)
        if not certificate:
            return jsonify({'success': False, 'error': 'Certificate not found.'}), 404
        # $pull with modified_count guarantees a double-click / second tab can't deduct twice.
        result = users_collection.update_one(
            {'_id': user_id, 'certificates._id': certificate_id},
            {'$pull': {'certificates': {'_id': certificate_id}}})
        if not result.modified_count:
            return jsonify({'success': False, 'error': 'Certificate already deleted.'}), 404
        # FIX: old logic skipped the deduction for legacy certs (no xp_awarded flag) when the user
        # had pre_gamification_certs. Now every cert deducts, unless it was explicitly marked
        # xp_awarded=False (add_xp failed at creation time). XP floors at 0 inside deduct_xp.
        should_deduct = certificate.get('xp_awarded') is not False
        deducted = bool(should_deduct and deduct_xp(current_user.id, 15, "Deleted a Certificate"))
        return jsonify({'success': True, 'xp_deducted': deducted})
    except Exception as e:
        print(f"Delete certificate error: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500


@app.route('/certificates/edit/<cert_id>', methods=['PUT'])
@login_required
def edit_certificate(cert_id):
    try:
        clean, error = clean_cert_payload(request.get_json(silent=True))
        if error:
            return jsonify({'success': False, 'error': error}), 400
        try:
            certificate_id = ObjectId(cert_id)
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid certificate id.'}), 400
        result = users_collection.update_one(
            {'_id': ObjectId(current_user.id), 'certificates._id': certificate_id},
            {'$set': {f'certificates.$.{k}': v for k, v in clean.items()}})
        if not result.matched_count:
            return jsonify({'success': False, 'error': 'Certificate not found.'}), 404
        return jsonify({'success': True})
    except Exception as e:
        print(f"Edit certificate error: {e}")
        return jsonify({'success': False, 'error': 'Something went wrong. Please try again.'}), 500