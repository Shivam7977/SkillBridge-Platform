import os
import re
import random
import json
from recommendation_engine import get_recommended_projects
import secrets
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from pymongo import MongoClient
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from bson.objectid import ObjectId
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from dotenv import load_dotenv
from markupsafe import Markup
from ai_roadmap_generator import configure_ai, generate_roadmap_with_ai, find_youtube_playlist
import regex as re_ext
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

load_dotenv(override=True)
try:
    if not os.getenv('GEMINI_API_KEY'):
        print("WARNING: GEMINI_API_KEY not found in .env file. AI features will likely fail.")
    configure_ai()
    print("AI configured successfully.")
except ValueError as e:
    print(f"AI Configuration Error: {e}")
except Exception as e_ai_config:
     print(f"An unexpected error occurred during AI configuration: {e_ai_config}")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'project_uploads')
bcrypt = Bcrypt(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))
s = URLSafeTimedSerializer(app.secret_key)

if not os.getenv('MAIL_USERNAME') or not os.getenv('MAIL_PASSWORD'):
    print("WARNING: Email credentials not found in .env file. Email features will likely fail.")
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')
mail = Mail(app)

try:
    mongo_uri = "mongodb://127.0.0.1:27017/skillbridge_db"
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    db = client.get_database()
    client.admin.command('ismaster')
    print(f"MongoDB connection successful.")
    users_collection = db['users']
    roadmaps_collection = db['roadmaps']
    projects_collection = db['projects']
    commits_collection = db['commits']
    messages_collection = db['messages']
    communities_collection = db["communities"]
    community_messages_collection = db["community_messages"]
    chat_history_collection = db["chat_history"]
except Exception as e:
    print(f"ERROR: Could not connect to MongoDB. Please ensure it's running. Details: {e}")
    exit()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"

class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data["_id"])
        self.email = user_data.get("email", "N/A")
        self.name = user_data.get("name", "User")

@login_manager.user_loader
def load_user(user_id):
    try:
        obj_id = ObjectId(user_id)
        user_data = users_collection.find_one({"_id": obj_id})
        if user_data:
            return User(user_data)
    except Exception as e:
        print(f"Error loading user {user_id}: {e}")
    return None

def flatten_data(y):
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x:
                flatten(x.get(a), name + str(a) + '_')
        elif isinstance(x, list):
            pass
        elif x is not None:
            out[name[:-1]] = x
    flatten(y)
    return out

TEMPLATE_NAMES = {
    1:"Classic Tech",2:"Minimal Pro",3:"Fresh Graduate",4:"Creative Bold",
    5:"Data Scientist",6:"Full Stack Dev",7:"Product Manager",8:"UI/UX Designer",
    9:"Marketing Pro",10:"Finance / CPA",11:"Healthcare",12:"Cybersecurity",
    13:"DevOps / Cloud",14:"Freelancer",15:"Executive",16:"Educator",
    17:"Sales Pro",18:"Startup Founder",
}

# ════════════════════════════════════════════════════════════
# GAMIFICATION ENGINE (XP & LEVELS)
# ════════════════════════════════════════════════════════════

def get_level_info(xp):
    """XP ke hisaab se Level aur Badge return karega"""
    if xp < 200:
        return {"level": 1, "badge": "Bronze Builder", "icon": "🥉", "next_xp": 200}
    elif xp < 500:
        return {"level": 2, "badge": "Silver Coder", "icon": "🥈", "next_xp": 500}
    elif xp < 1000:
        return {"level": 3, "badge": "Gold Developer", "icon": "🥇", "next_xp": 1000}
    elif xp < 2000:
        return {"level": 4, "badge": "Platinum Architect", "icon": "💎", "next_xp": 2000}
    else:
        return {"level": 5, "badge": "Diamond Founder", "icon": "🚀", "next_xp": "Max"}

def add_xp(user_id, points, reason="Action"):
    """User ke account mein XP add karne ka function"""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        if not user: return False

        current_xp = user.get('xp', 0)
        new_xp = current_xp + points

        # Check if Level Up
        old_level = get_level_info(current_xp)['level']
        new_level_info = get_level_info(new_xp)

        update_data = {'xp': new_xp, 'level': new_level_info['level'], 'badge': new_level_info['badge']}

        users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': update_data})
        # Track weekly XP for leaderboard
        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$inc': {'weekly_xp': points}, '$set': {'weekly_xp_updated': datetime.utcnow()}}
        )

        if new_level_info['level'] > old_level:
            print(f"🎉 LEVEL UP! User {user.get('name')} is now Level {new_level_info['level']} ({new_level_info['badge']})")
            # Store level-up in DB so next page load can show toast
            users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': {'pending_levelup': new_level_info['badge']}})

        return True
    except Exception as e:
        print(f"Error adding XP: {e}")
        return False

def deduct_xp(user_id, points, reason="Action Reversed"):
    """User ke account se XP minus karne ka function (abuse prevention)"""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        if not user: return False

        current_xp = user.get('xp', 0)
        new_xp = max(0, current_xp - points)  # XP kabhi 0 se neeche nahi jayega
        new_level_info = get_level_info(new_xp)

        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'xp': new_xp, 'level': new_level_info['level'], 'badge': new_level_info['badge']}}
        )
        print(f"📉 XP DEDUCTED: -{points} from user {user.get('name')} for: {reason}")
        return True
    except Exception as e:
        print(f"Error deducting XP: {e}")
        return False

def update_streak(user_id):
    """Login par activity streak track karne ka function"""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        last_login = user.get('last_activity_date')
        streak = user.get('streak_count', 0)

        if last_login:
            if isinstance(last_login, str):
                last_login = datetime.fromisoformat(last_login.replace("Z", "+00:00"))
            if last_login.tzinfo is None:
                last_login = last_login.replace(tzinfo=timezone.utc)
            last_login = last_login.replace(hour=0, minute=0, second=0, microsecond=0)
            diff = (today - last_login).days

            if diff == 0:  # Same day login — keep streak, just update timestamp
                pass
            elif diff == 1:  # Consecutive day — increment
                streak += 1
                if streak == 7:
                    add_xp(user_id, 35, "7-day activity streak")
                    print(f"🔥 7-DAY STREAK! User {user.get('name')} earned 35 XP")
                if streak == 30:
                    add_xp(user_id, 100, "30-day activity streak")
                    print(f"🔥🔥 30-DAY STREAK! User {user.get('name')} earned 100 XP")
            else:  # Streak broken
                streak = 1
        else:
            streak = 1  # First ever login — set to 1 immediately

        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'streak_count': streak, 'last_activity_date': datetime.utcnow()}}
        )
    except Exception as e:
        print(f"Streak update error: {e}")

def parse_skills(value):
    if not value:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                for s in item.split(','):
                    s = s.strip()
                    if s:
                        result.append(s)
            else:
                result.append(str(item))
        return result
    if isinstance(value, str):
        if ',' in value:
            return [s.strip() for s in value.split(',') if s.strip()]
        else:
            return [s.strip() for s in value.split() if s.strip()]
    return []

@app.route('/')
def index():
    try:
        total_users = users_collection.count_documents({})
        total_projects = projects_collection.count_documents({})
        completed_project_ids = commits_collection.distinct("project_id")
        completed_projects = len(completed_project_ids)
        completion_percent = 0
        if total_projects > 0:
            completion_percent = int((completed_projects / total_projects) * 100)
        return render_template('index.html',total_users=total_users,total_projects=total_projects,completed_projects=completed_projects,completion_percent=completion_percent)
    except Exception as e:
        print(f"Index stats error: {e}")
        return render_template('index.html',total_users=0,total_projects=0,completed_projects=0,completion_percent=0)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not name or not email or not password:
             flash("All fields are required.", "error"); return redirect(url_for('signup'))
        if password != confirm_password:
            flash("Passwords do not match.", "error"); return redirect(url_for('signup'))
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash("Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol (@$!%*?&).", "error"); return redirect(url_for('signup'))
        if users_collection.find_one({'email': email}):
            flash("An account with this email already exists. Try logging in.", "error"); return redirect(url_for('login'))
        otp = random.randint(100000, 999999)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        session['temp_user_data'] = {'name': name, 'email': email, 'password': hashed_password}
        session['otp'] = otp
        session['otp_timestamp'] = datetime.utcnow().timestamp()
        try:
            msg = Message('Your SkillBridge OTP Code', recipients=[email])
            msg.body = f'Your One-Time Password (OTP) for SkillBridge is: {otp}. It expires in 10 minutes.'
            mail.send(msg)
            flash('An OTP has been sent to your email. Please check your inbox (and spam folder).', 'success')
            return redirect(url_for('verify'))
        except Exception as e:
            print(f"Failed to send OTP email to {email}: {e}")
            flash(f'Failed to send verification email. Please try again later or contact support.', 'error')
            session.pop('temp_user_data', None); session.pop('otp', None); session.pop('otp_timestamp', None)
            return redirect(url_for('signup'))
    return render_template('signup.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'temp_user_data' not in session or 'otp' not in session or 'otp_timestamp' not in session:
        flash('Verification session expired or invalid. Please sign up again.', 'warning')
        return redirect(url_for('signup'))
    otp_age = datetime.utcnow().timestamp() - session.get('otp_timestamp', 0)
    if otp_age > 600:
        session.pop('temp_user_data', None); session.pop('otp', None); session.pop('otp_timestamp', None)
        flash('Your OTP has expired. Please sign up again.', 'error')
        return redirect(url_for('signup'))
    if request.method == 'POST':
        user_otp_str = request.form.get('otp')
        try:
            user_otp = int(user_otp_str)
            stored_otp = session.get('otp')
            if stored_otp is not None and user_otp == stored_otp:
                user_data = session.pop('temp_user_data', None)
                session.pop('otp', None); session.pop('otp_timestamp', None)
                if user_data:
                    user_data['profile_pic'] = 'default.jpg'
                    user_data['github_url'] = ''; user_data['linkedin_url'] = ''
                    user_data['known_skills'] = []; user_data['learning_skills'] = []
                    user_data['xp'] = 0  # INIT XP on account creation
                    user_data['created_at'] = datetime.utcnow()
                    users_collection.insert_one(user_data)
                    flash('Email verified successfully! Please log in.', 'success'); return redirect(url_for('login'))
                else:
                    flash('Session error retrieving user data. Please sign up again.', 'error'); return redirect(url_for('signup'))
            else:
                flash('Invalid OTP. Please try again.', 'error'); return redirect(url_for('verify'))
        except (ValueError, TypeError):
             flash('Invalid OTP format. Please enter numbers only.', 'error'); return redirect(url_for('verify'))
    remaining_time = max(0, 600 - int(otp_age))
    return render_template('verify.html', remaining_minutes=remaining_time // 60, remaining_seconds=remaining_time % 60)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        remember = bool(request.form.get('remember'))
        if not email or not password:
             flash("Email and password are required.", "error"); return redirect(url_for('login'))
        user_data = users_collection.find_one({'email': email})
        if user_data and bcrypt.check_password_hash(user_data.get('password', ''), password):
            user = User(user_data)
            login_user(user, remember=remember)
            update_streak(str(user_data['_id']))  # STREAK TRACKING on every login
            next_page = request.args.get('next')
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(next_page or url_for('main_page'))
        else:
            flash("Invalid email or password. Please try again.", "error"); return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if email and users_collection.find_one({'email': email}, {'_id': 1}):
            token = s.dumps(email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            try:
                msg = Message('Password Reset Request for SkillBridge', recipients=[email])
                msg.body = f'Click the following link to reset your password: {reset_url}\n\nThis link will expire in 1 hour.'
                mail.send(msg)
                flash('A password reset link has been sent to your email.', 'success')
            except Exception as e:
                print(f"Failed to send password reset email to {email}: {e}")
                flash('Could not send email. Please check your email address or try again later.', 'error')
            return redirect(url_for('login'))
        flash('If an account exists for that email, a password reset link has been sent.', 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=3600)
    except (SignatureExpired, BadTimeSignature):
        flash('The password reset link is invalid or has expired.', 'error'); return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if password != confirm_password:
            flash('Passwords do not match.', 'error'); return render_template('reset_password.html', token=token)
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash('Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol.', 'error'); return render_template('reset_password.html', token=token)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        result = users_collection.update_one({'email': email}, {'$set': {'password': hashed_password}})
        if result.modified_count > 0:
            flash('Your password has been updated successfully! Please log in with your new password.', 'success'); return redirect(url_for('login'))
        else:
            flash('Could not update password. Please try again.', 'error'); return redirect(url_for('forgot_password'))
    return render_template('reset_password.html', token=token)

@app.route('/mainpage')
@login_required
def main_page():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    user_name = current_user.name
    roadmap_count = roadmaps_collection.count_documents({'user_id': ObjectId(current_user.id)})
    project_count = projects_collection.count_documents({'created_by_id': ObjectId(current_user.id)})
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
    return render_template('mainpage.html',
        user_name=user_name,
        roadmap_count=roadmap_count,
        project_count=project_count,
        xp=xp,
        level_info=level_info,
        streak=streak,
        progress_pct=progress_pct
    )

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out successfully.", "success")
    return redirect(url_for('index'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        old_user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        old_learning_skills = set(old_user_data.get('learning_skills', []))
        profile_pic_fn = old_user_data.get('profile_pic', 'default.jpg')
        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                random_hex = secrets.token_hex(8)
                _, f_ext = os.path.splitext(filename)
                profile_pic_fn = random_hex + f_ext
                file.save(os.path.join(app.root_path, 'static/profile_pics', profile_pic_fn))
        new_learning_skills_list = [s.strip() for s in request.form.get('learning_skills', '').split(',') if s.strip()]

        # FIX: Prevent negative experience years
        try:
            exp_years = max(0, int(request.form.get('experience_years') or 0))
        except (ValueError, TypeError):
            exp_years = 0

        update_data = {
            'name': request.form.get('name', '').strip(),
            'title': request.form.get('title', '').strip(),
            'about_me': request.form.get('about_me', '').strip(),
            'location': request.form.get('location', '').strip(),
            'github_url': request.form.get('github_url', '').strip(),
            'linkedin_url': request.form.get('linkedin_url', '').strip(),
            'instagram_url': request.form.get('instagram_url', '').strip(),
            'facebook_url': request.form.get('facebook_url', '').strip(),
            'education_college': request.form.get('education_college', '').strip(),
            'education_degree': request.form.get('education_degree', '').strip(),
            'experience_years': str(exp_years),
            'current_status': request.form.get('current_status', '').strip(),
            'availability': request.form.get('availability', '').strip(),
            'career_goal': request.form.get('career_goal', '').strip(),
            'known_skills': [s.strip() for s in request.form.get('known_skills', '').split(',') if s.strip()],
            'learning_skills': new_learning_skills_list,
            'profile_pic': profile_pic_fn
        }

        # PROFILE 100% COMPLETE REWARD (+30 XP) — only once, loophole-safe
        req_fields = ['about_me', 'location', 'education_college', 'education_degree', 'github_url']
        if all(update_data.get(f) for f in req_fields) and not old_user_data.get('profile_complete_reward'):
            add_xp(current_user.id, 30, "Profile 100% Complete")
            update_data['profile_complete_reward'] = True

        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        flash('Profile updated successfully!', 'success')
        new_learning_skills = set(new_learning_skills_list)
        added_skills = list(new_learning_skills - old_learning_skills)
        if added_skills:
            goal_str = added_skills[0]
            link = url_for('roadmap_generator', goal=goal_str)
            message = Markup(f'New goal detected! 🚀 <a href="{link}">Generate a roadmap for "{goal_str}"?</a>')
            flash(message, 'info')
        return redirect(url_for('profile'))

    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
    profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)
    user_profile = {
        'name': user_data.get('name', ''), 'email': user_data.get('email', ''),
        'title': user_data.get('title', ''), 'about_me': user_data.get('about_me', ''),
        'location': user_data.get('location', ''), 'github_url': user_data.get('github_url', ''),
        'linkedin_url': user_data.get('linkedin_url', ''), 'instagram_url': user_data.get('instagram_url', ''),
        'facebook_url': user_data.get('facebook_url', ''), 'education_college': user_data.get('education_college', ''),
        'education_degree': user_data.get('education_degree', ''), 'experience_years': user_data.get('experience_years', ''),
        'current_status': user_data.get('current_status', ''), 'availability': user_data.get('availability', ''),
        'career_goal': user_data.get('career_goal', ''),
        'known_skills_str': ', '.join(user_data.get('known_skills', [])),
        'learning_skills_str': ', '.join(user_data.get('learning_skills', [])),
        'profile_pic_url': profile_pic_url, 'certificates': user_data.get('certificates', []),
        'github_username': user_data.get('github_username', ''), 'github_avatar': user_data.get('github_avatar', ''),
        'github_repos': user_data.get('github_repos', []), 'github_langs': user_data.get('github_langs', []),
        'github_followers': user_data.get('github_followers', 0), 'github_public_repos': user_data.get('github_public_repos', 0),
        'github_synced_at': str(user_data.get('github_synced_at', ''))
    }
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
    return render_template('profile.html', user=user_profile, xp=xp, level_info=level_info, streak=streak, progress_pct=progress_pct)

@app.route('/roadmap_generator', methods=['GET', 'POST'])
@login_required
def roadmap_generator():
    roadmap_data = None
    goal = ""
    if request.method == 'POST':
        goal = request.form.get('goal', '').strip()
        if not goal:
            flash("Please enter a goal for your roadmap.", "error")
            return render_template('roadmap_generator.html', goal=goal)
    else:
        goal_from_url = request.args.get('goal', '').strip()
        if goal_from_url:
            goal = goal_from_url
            flash(f"Generating roadmap for '{goal}'...", 'info')
    if goal:
        try:
            print(f"Calling Gemini AI with FINAL prompt for '{goal}'...")
            roadmap_data = generate_roadmap_with_ai(goal)
            if roadmap_data and isinstance(roadmap_data, dict) and isinstance(roadmap_data.get('stages'), list):
                for stage in roadmap_data.get("stages", []):
                    learning_modules = stage.get("learning_modules", [])
                    if not isinstance(learning_modules, list): continue
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
                return render_template('roadmap_generator.html', roadmap_data=roadmap_data, goal=goal)
            else:
                print(f"AI response invalid or missing stages for goal '{goal}'. Response: {roadmap_data}")
                flash("Sorry, the AI response was incomplete or in an unexpected format. Please try again.", "error")
        except Exception as e_ai:
            print(f"Error during roadmap generation or processing for '{goal}': {e_ai}")
            flash(f"An error occurred while communicating with the AI: {e_ai}", "error")
        return render_template('roadmap_generator.html', goal=goal)
    return render_template('roadmap_generator.html')

@app.route('/save_roadmap', methods=['POST'])
@login_required
def save_roadmap():
    goal = request.form.get('goal')
    roadmap_content_str = request.form.get('roadmap_content')
    if goal and roadmap_content_str:
        try:
            roadmap_data = json.loads(roadmap_content_str)
            if isinstance(roadmap_data, dict) and 'stages' in roadmap_data:
                roadmaps_collection.insert_one({'user_id': ObjectId(current_user.id), 'goal': goal, 'roadmap_content': roadmap_data, 'created_at': datetime.utcnow()})
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
        return render_template('my_roadmaps.html', roadmaps=user_roadmaps)
    except Exception as e:
        print(f"Error fetching roadmaps for user {current_user.id}: {e}")
        flash("Could not load your saved roadmaps.", "error")
        return render_template('my_roadmaps.html', roadmaps=[])

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
        roadmaps_collection.delete_one({'_id': obj_id})
        if completed_stages > 0:
            deduct_xp(current_user.id, completed_stages * 20, f"Deleted roadmap ({completed_stages} completed stages)")
        return jsonify({'success': True, 'deducted': completed_stages * 20})
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
                roadmaps_collection.update_one({'_id': obj_id}, {'$set': {'roadmap_content': content}})
                add_xp(current_user.id, 20, "Completed Roadmap Stage")
                flash('Stage marked as complete! +20 XP 🎉', 'success')
            else:
                roadmaps_collection.update_one({'_id': obj_id}, {'$set': {'roadmap_content': content}})
                flash('Stage already completed.', 'info')
        return redirect(url_for('view_roadmap', roadmap_id=roadmap_id))
    except Exception as e:
        print(f"Update error: {e}"); return redirect(url_for('my_roadmaps'))

# --- PROJECT SYSTEM ROUTES ---

@app.route('/projects')
def projects():
    try:
        all_projects_cursor = projects_collection.find().sort('created_at', -1)
        all_projects = list(all_projects_cursor)
        recommended_projects = []
        other_projects = []
        profile_incomplete = False
        is_authenticated = current_user.is_authenticated
        if is_authenticated:
            recommended_projects = get_recommended_projects(current_user.id, users_collection, projects_collection)
            rec_ids = [p['_id'] for p in recommended_projects]
            other_projects = list(projects_collection.find({'_id': {'$nin': rec_ids}}).sort('created_at', -1))
            user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'known_skills': 1, 'learning_skills': 1})
            if not user_data.get('known_skills') and not user_data.get('learning_skills'):
                profile_incomplete = True
        else:
            other_projects = all_projects
        return render_template('projects.html', recommended_projects=recommended_projects, other_projects=other_projects, profile_incomplete=profile_incomplete, is_authenticated=is_authenticated)
    except Exception as e:
        print(f"Error fetching community projects: {e}")
        flash("Could not load community projects at this time.", "error")
        return render_template('projects.html', recommended_projects=[], other_projects=[], profile_incomplete=False, is_authenticated=current_user.is_authenticated)

@app.route('/my_projects')
@login_required
def my_projects():
    try:
        my_projects_list = list(projects_collection.find({'created_by_id': ObjectId(current_user.id)}).sort('created_at', -1))
        return render_template('my_projects.html', projects=my_projects_list)
    except Exception as e:
         print(f"Error fetching user projects for {current_user.id}: {e}")
         flash("Could not load your projects.", "error")
         return render_template('my_projects.html', projects=[])

@app.route('/create_project', methods=['GET', 'POST'])
@login_required
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
            "created_at": datetime.utcnow()
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
                "created_at": datetime.utcnow()
            }).inserted_id
            projects_collection.update_one({"_id": project_id}, {"$set": {"community_id": community_id}})
            flash("Project and community created successfully! +10 XP 🎉", "success")
            return redirect(url_for("find_communities"))
        flash("Project created successfully! +10 XP 🎉", "success")
        return redirect(url_for("my_projects"))
    return render_template("create_project.html")

# FIX: Toggle project completed status
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
    """Delete a project and deduct XP to prevent create-delete abuse"""
    try:
        result = projects_collection.delete_one(
            {'_id': ObjectId(project_id), 'created_by_id': ObjectId(current_user.id)}
        )
        if result.deleted_count:
            deduct_xp(current_user.id, 10, "Project Deleted")
            flash("Project deleted. -10 XP", "info")
        return jsonify({'success': True})
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
    is_owner = False
    if current_user.is_authenticated and str(project.get('created_by_id')) == current_user.id:
         is_owner = True
    try:
        commits = list(commits_collection.find({'project_id': obj_id}).sort('timestamp', -1))
    except Exception as e:
         print(f"Error fetching commits for project {project_id}: {e}")
         flash("Could not load project history.", "error"); commits = []
    creator_name = project.get('created_by_name', 'Unknown User')
    return render_template('project_page.html', project=project, is_owner=is_owner, commits=commits, creator_name=creator_name)

@app.route('/project/<project_id>/upload', methods=['GET', 'POST'])
@login_required
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
            commits_collection.insert_one({'project_id': obj_id, 'user_id': ObjectId(current_user.id), 'user_name': current_user.name, 'timestamp': datetime.utcnow(), 'message': commit_message, 'filename': project_filename})
            add_xp(current_user.id, 50, "Uploaded a Project Version")
            flash('New project version uploaded successfully! +50 XP 🎉', 'success')
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

# --- RESUME BUILDER ROUTES ---

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
        return render_template("resume_builder.html", user=user_data, projects=user_projects, certificates=certificates, github_repos=github_repos, github_langs=github_langs)
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
    return render_template('resume_pdf.html', user=user_data, projects=user_projects, template_num=template_num, template_name=template_name)

# --- SKILLBRIDGE AI CHATBOT ---

@app.route('/api/chatbot', methods=['POST'])
@login_required
def chatbot():
    try:
        import google.generativeai as genai
        import traceback
        _api_key = os.getenv("GEMINI_API_KEY")
        if not _api_key:
            return jsonify({"reply": "AI not configured. Please set GEMINI_API_KEY in your .env file."}), 500
        genai.configure(api_key=_api_key)
        data = request.get_json()
        user_message = data.get('message', '').strip()
        if not user_message:
            return jsonify({'reply': 'Please type a message!'}), 400
        uid = ObjectId(current_user.id)
        chat_doc = chat_history_collection.find_one({'user_id': uid})
        db_history = chat_doc.get('messages', []) if chat_doc else []
        recent_history = db_history[-20:]
        user_data = users_collection.find_one({'_id': uid}) or {}
        user_projects = list(projects_collection.find({'created_by_id': uid}).sort('created_at', -1))
        user_roadmaps = list(roadmaps_collection.find({'user_id': uid}).sort('created_at', -1))
        known_skills = parse_skills(user_data.get('known_skills'))
        learning_skills = parse_skills(user_data.get('learning_skills'))
        proj_summary = '\n'.join([f"  - {p.get('title','Untitled')}: {p.get('description','')[:120]}" for p in user_projects]) or '  (No projects yet)'
        roadmap_summary = '\n'.join([f"  - {r.get('goal','Unknown goal')}" for r in user_roadmaps]) or '  (No roadmaps yet)'
        history_text = ""
        for turn in recent_history:
            label = "User" if turn.get('role') == 'user' else "Assistant"
            history_text += f"{label}: {turn.get('content','')}\n"
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

CONVERSATION HISTORY:
{history_text}
User: {user_message}
Assistant:"""
        model = genai.GenerativeModel(model_name='gemini-2.5-flash')
        response = model.generate_content(full_prompt)
        reply = response.text.strip()
        new_turns = [
            {'role': 'user', 'content': user_message, 'timestamp': datetime.now(timezone.utc)},
            {'role': 'assistant', 'content': reply, 'timestamp': datetime.now(timezone.utc)},
        ]
        chat_history_collection.update_one(
            {'user_id': uid},
            {'$push': {'messages': {'$each': new_turns}}, '$set': {'last_updated': datetime.now(timezone.utc)}, '$setOnInsert': {'user_id': uid, 'created_at': datetime.now(timezone.utc)}},
            upsert=True
        )
        return jsonify({'reply': reply})
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Chatbot error: {type(e).__name__}: {e}")
        return jsonify({'reply': f"Error: {type(e).__name__}: {str(e)}"}), 500

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

# --- PORTFOLIO BUILDER ROUTES ---

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
        return render_template('portfolio_builder.html', user=user_data, projects=user_projects, certificates=certificates, github_repos=github_repos, github_langs=github_langs)
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
                template_id = filename.replace('.html', '')
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

# --- VIEW PUBLIC PROFILE ROUTE ---
@app.route('/user/<user_id>')
def view_user_profile(user_id):
    try:
        obj_id = ObjectId(user_id)
        user_data = users_collection.find_one({'_id': obj_id})
        if not user_data:
            flash("User not found.", "error"); return redirect(url_for('projects'))
        profile_pic_filename = user_data.get('profile_pic', 'default.jpg')
        profile_pic_url = url_for('static', filename='profile_pics/' + profile_pic_filename)
        user_profile = {
            'id': str(user_data['_id']), 'name': user_data.get('name', 'Builder'),
            'title': user_data.get('title', 'Developer'), 'about_me': user_data.get('about_me', ''),
            'experience_years': user_data.get('experience_years', '0'), 'current_status': user_data.get('current_status', 'Available'),
            'location': user_data.get('location', 'Remote'), 'education_college': user_data.get('education_college', 'N/A'),
            'education_degree': user_data.get('education_degree', 'N/A'), 'career_goal': user_data.get('career_goal', ''),
            'github_url': user_data.get('github_url', ''), 'linkedin_url': user_data.get('linkedin_url', ''),
            'instagram_url': user_data.get('instagram_url', ''), 'facebook_url': user_data.get('facebook_url', ''),
            'portfolio_url': user_data.get('portfolio_url', ''), 'profile_pic_url': profile_pic_url,
            'known_skills': user_data.get('known_skills', []), 'learning_skills': user_data.get('learning_skills', [])
        }
        xp = user_data.get('xp', 0)
        level_info = get_level_info(xp)
        streak = user_data.get('streak_count', 1)
        progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
        return render_template('view_profile.html', profile=user_profile, xp=xp, level_info=level_info, streak=streak, progress_pct=progress_pct)
    except Exception as e:
        print(f"Error viewing profile: {e}"); return redirect(url_for('projects'))

# --- MESSAGING ROUTES ---

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
            conversations.append({"user_id": str(other_user["_id"]), "user_name": other_user.get("name", "Unknown"), "profile_pic": other_user.get("profile_pic", "default.jpg"), "last_message": res.get("last_message", ""), "timestamp": res["timestamp"].strftime("%b %d, %I:%M %p"), "is_unread": is_unread, "sent_by_me": sent_by_me, "sender_name": sender_name})
        return render_template("messages_list.html", conversations=conversations)
    except Exception as e:
        print(f"Inbox error: {e}"); return redirect(url_for("main_page"))

@app.route('/chat/<receiver_id>', methods=['GET', 'POST'])
@login_required
def chat(receiver_id):
    try:
        r_id = ObjectId(receiver_id)
        u_id = ObjectId(current_user.id)
        rec = users_collection.find_one({"_id": r_id})
        if not rec:
            flash("User not found.", "error"); return redirect(url_for('messages_list'))
        if request.method == 'POST':
            msg = request.form.get('content', '').strip()
            if msg:
                messages_collection.insert_one({"sender_id": u_id, "receiver_id": r_id, "content": msg, "timestamp": datetime.utcnow(), "is_read": False})
            return redirect(url_for('chat', receiver_id=receiver_id))
        messages_collection.update_many({"sender_id": r_id, "receiver_id": u_id, "is_read": False}, {"$set": {"is_read": True}})
        history = list(messages_collection.find({"$or": [{"sender_id": u_id, "receiver_id": r_id}, {"sender_id": r_id, "receiver_id": u_id}]}).sort("timestamp", 1))
        return render_template('chat.html', receiver=rec, messages=history)
    except Exception as e:
        print(f"Chat Error: {e}"); return redirect(url_for('messages_list'))

@app.route('/community/<community_id>')
@login_required
def view_community(community_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    if not community:
        flash("Community not found", "error"); return redirect(url_for("find_communities"))
    user_id = ObjectId(current_user.id)
    is_owner = community["owner_id"] == user_id
    is_admin = user_id in community.get("admins", [])
    is_member = user_id in community.get("members", [])
    member_users = list(users_collection.find({"_id": {"$in": community.get("members", [])}}, {"name": 1}))
    members_data = []
    for user in member_users:
        role = "member"
        if user["_id"] == community["owner_id"]: role = "owner"
        elif user["_id"] in community.get("admins", []): role = "admin"
        members_data.append({"_id": str(user["_id"]), "name": user.get("name", "User"), "role": role})
    messages = list(community_messages_collection.find({"community_id": ObjectId(community_id)}).sort("timestamp", 1))
    return render_template("community_chat.html", community=community, messages=messages, members_data=members_data, is_owner=is_owner, is_admin=is_admin, is_member=is_member)

@app.route('/community/<community_id>/send', methods=['POST'])
@login_required
def send_community_message(community_id):
    msg = request.form.get("message", "").strip()
    if msg:
        community_messages_collection.insert_one({"community_id": ObjectId(community_id), "sender_id": ObjectId(current_user.id), "sender_name": current_user.name, "message": msg, "timestamp": datetime.utcnow(), "reactions": {}})
    return redirect(url_for("view_community", community_id=community_id))

@app.route('/community/<community_id>/react/<message_id>/<emoji>')
@login_required
def react_to_message(community_id, message_id, emoji):
    user_id = ObjectId(current_user.id)
    message = community_messages_collection.find_one({"_id": ObjectId(message_id)})
    if not message: return redirect(url_for("view_community", community_id=community_id))
    reactions = message.get("reactions", {})
    if emoji not in reactions: reactions[emoji] = []
    if user_id in reactions[emoji]: reactions[emoji].remove(user_id)
    else: reactions[emoji].append(user_id)
    community_messages_collection.update_one({"_id": ObjectId(message_id)}, {"$set": {"reactions": reactions}})
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

@app.route('/community/<community_id>/approve/<user_id>')
@login_required
def approve_member(community_id, user_id):
    community = communities_collection.find_one({"_id": ObjectId(community_id)})
    if not community:
        flash("Community not found.", "error"); return redirect(url_for("find_communities"))
    is_allowed = False
    for member in community.get("members", []):
        if member["user_id"] == ObjectId(current_user.id) and member["role"] in ["owner", "admin"]: is_allowed = True
    if not is_allowed:
        flash("Unauthorized action.", "error"); return redirect(url_for("view_community", community_id=community_id))
    communities_collection.update_one({"_id": ObjectId(community_id)}, {"$pull": {"pending_requests": ObjectId(user_id)}, "$push": {"members": {"user_id": ObjectId(user_id), "role": "member"}}})
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
    # Declined user should also lose the XP they gained from requesting
    deduct_xp(str(user_obj), 10, "Community request declined")
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
    communities = list(communities_collection.find().sort("created_at", -1))
    for c in communities:
        c["is_owner"] = c.get("owner_id") == user_id
        c["is_member"] = user_id in c.get("members", [])
        c["is_other"] = not c["is_owner"] and not c["is_member"]
    return render_template("find_communities.html", communities=communities)

@app.route('/debug/communities')
@login_required
def debug_communities():
    data = list(communities_collection.find({}))
    return {"count": len(data), "data": [str(d) for d in data]}

print("CONNECTED DB NAME:", db.name)
print("COMMUNITIES COLLECTION:", communities_collection.full_name)

@app.route('/db-test-communities')
@login_required
def db_test_communities():
    before = communities_collection.count_documents({})
    communities_collection.insert_one({"test": "db_integration_check"})
    after = communities_collection.count_documents({})
    return {"before_insert": before, "after_insert": after, "collection": communities_collection.full_name}

@app.route('/force-community')
@login_required
def force_community():
    result = communities_collection.insert_one({"project_title": "FORCED COMMUNITY TEST", "skills_required": ["Python"], "visibility": "public", "owner_id": ObjectId(current_user.id), "owner_name": current_user.name, "members": [ObjectId(current_user.id)], "created_at": datetime.utcnow()})
    return f"Inserted community with id: {result.inserted_id}"

# ════════════════════════════════════════════════════════════
# CERTIFICATES ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/certificates/add', methods=['POST'])
@login_required
def add_certificate():
    try:
        data = request.get_json()
        cert = {'_id': ObjectId(), 'name': data.get('name', '').strip(), 'issuer': data.get('issuer', '').strip(), 'issue_date': data.get('issue_date', '').strip(), 'expiry_date': data.get('expiry_date', '').strip(), 'credential_id': data.get('credential_id', '').strip(), 'cert_url': data.get('cert_url', '').strip(), 'added_at': datetime.utcnow()}
        if not cert['name'] or not cert['issuer']:
            return jsonify({'success': False, 'error': 'Name and issuer are required'}), 400
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$push': {'certificates': cert}})
        add_xp(current_user.id, 15, "Added a Certificate")
        cert['_id'] = str(cert['_id']); cert['added_at'] = cert['added_at'].isoformat()
        return jsonify({'success': True, 'certificate': cert})
    except Exception as e:
        print(f"Add certificate error: {e}"); return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/certificates/delete/<cert_id>', methods=['DELETE'])
@login_required
def delete_certificate(cert_id):
    try:
        result = users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$pull': {'certificates': {'_id': ObjectId(cert_id)}}})
        if result.modified_count:
            deduct_xp(current_user.id, 15, "Deleted a Certificate")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/certificates/edit/<cert_id>', methods=['PUT'])
@login_required
def edit_certificate(cert_id):
    try:
        data = request.get_json()
        users_collection.update_one(
            {'_id': ObjectId(current_user.id), 'certificates._id': ObjectId(cert_id)},
            {'$set': {'certificates.$.name': data.get('name', '').strip(), 'certificates.$.issuer': data.get('issuer', '').strip(), 'certificates.$.issue_date': data.get('issue_date', '').strip(), 'certificates.$.expiry_date': data.get('expiry_date', '').strip(), 'certificates.$.credential_id': data.get('credential_id', '').strip(), 'certificates.$.cert_url': data.get('cert_url', '').strip()}}
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ════════════════════════════════════════════════════════════
# GITHUB INTEGRATION ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/api/github/fetch', methods=['POST'])
@login_required
def github_fetch():
    try:
        import urllib.request
        import urllib.error
        data = request.get_json()
        username = data.get('username', '').strip().lstrip('@')
        if not username:
            return jsonify({'success': False, 'error': 'Username is required'}), 400
        headers = {'User-Agent': 'SkillBridge-App/1.0'}
        def gh_get(url):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode())
        profile = gh_get(f'https://api.github.com/users/{username}')
        if 'message' in profile and profile['message'] == 'Not Found':
            return jsonify({'success': False, 'error': f'GitHub user "{username}" not found'}), 404
        repos_raw = gh_get(f'https://api.github.com/users/{username}/repos?per_page=100&sort=pushed')
        repos_raw = [r for r in repos_raw if not r.get('fork')]
        repos_raw.sort(key=lambda r: r.get('stargazers_count', 0), reverse=True)
        top_repos = repos_raw[:6]
        lang_count = {}
        for r in repos_raw[:20]:
            lang = r.get('language')
            if lang: lang_count[lang] = lang_count.get(lang, 0) + 1
        top_langs = sorted(lang_count, key=lang_count.get, reverse=True)[:8]
        repos_out = [{'id': r['id'], 'name': r['name'], 'description': r.get('description') or '', 'url': r['html_url'], 'stars': r.get('stargazers_count', 0), 'forks': r.get('forks_count', 0), 'language': r.get('language') or '', 'updated': (r.get('pushed_at') or '')[:10]} for r in top_repos]
        result = {'username': profile.get('login', ''), 'name': profile.get('name') or '', 'bio': profile.get('bio') or '', 'avatar': profile.get('avatar_url', ''), 'location': profile.get('location') or '', 'blog': profile.get('blog') or '', 'followers': profile.get('followers', 0), 'following': profile.get('following', 0), 'public_repos': profile.get('public_repos', 0), 'github_url': profile.get('html_url', ''), 'repos': repos_out, 'top_languages': top_langs}
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        print(f"GitHub fetch error: {e}"); return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/github/sync', methods=['POST'])
@login_required
def github_sync():
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        gh_data = data.get('github_data', {})
        merge_skills = data.get('merge_skills', False)
        import_repos = data.get('import_repos', False)
        update = {
            'github_username': username, 'github_avatar': gh_data.get('avatar', ''),
            'github_repos': gh_data.get('repos', []), 'github_langs': gh_data.get('top_languages', []),
            'github_followers': gh_data.get('followers', 0), 'github_public_repos': gh_data.get('public_repos', 0),
            'github_synced_at': datetime.utcnow(),
        }
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}) or {}

        # GITHUB XP: Only award once, lifetime — loophole-safe flag
        if not user_data.get('github_reward_claimed'):
            add_xp(current_user.id, 25, "Connected GitHub")
            update['github_reward_claimed'] = True

        if not user_data.get('github_url') and gh_data.get('github_url'): update['github_url'] = gh_data['github_url']
        if not user_data.get('location') and gh_data.get('location'): update['location'] = gh_data['location']
        if not user_data.get('about_me') and gh_data.get('bio'): update['about_me'] = gh_data['bio']
        if merge_skills and gh_data.get('top_languages'):
            existing = set(parse_skills(user_data.get('known_skills')))
            merged = list(existing | set(gh_data['top_languages']))
            update['known_skills'] = merged
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': update})

        # FIX: Import GitHub repos as projects with correct field names
        if import_repos and gh_data.get('repos'):
            for repo in gh_data['repos']:
                existing = projects_collection.find_one({
                    'created_by_id': ObjectId(current_user.id),
                    'github_repo_id': repo.get('id')
                })
                if not existing:
                    skills = [repo['language']] if repo.get('language') else []
                    projects_collection.insert_one({
                        'created_by_id':   ObjectId(current_user.id),
                        'created_by_name': current_user.name,
                        'title':           repo['name'].replace('-', ' ').replace('_', ' ').title(),
                        'description':     repo.get('description') or f"A {repo.get('language') or 'code'} project on GitHub.",
                        'skills_needed':   skills,
                        'github_url':      repo['url'],
                        'github_repo_id':  repo.get('id'),
                        'stars':           repo.get('stars', 0),
                        'is_completed':    True,
                        'source':          'github',
                        'created_at':      datetime.utcnow(),
                    })
        return jsonify({'success': True})
    except Exception as e:
        print(f"GitHub sync error: {e}"); return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/github/disconnect', methods=['POST'])
@login_required
def github_disconnect():
    try:
        # NOTE: github_reward_claimed is NOT unset on disconnect.
        # This prevents the disconnect-reconnect XP farming loop.
        users_collection.update_one(
            {'_id': ObjectId(current_user.id)},
            {'$unset': {'github_username': '', 'github_avatar': '', 'github_repos': '', 'github_langs': '', 'github_followers': '', 'github_public_repos': '', 'github_synced_at': ''}}
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ════════════════════════════════════════════════════════════
# SETTINGS & ACCOUNT ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/settings')
@login_required
def settings():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # Gamification data for settings page
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)

    # Progress bar percentage
    if level_info['next_xp'] == "Max":
        progress_pct = 100
    else:
        progress_pct = int((xp / level_info['next_xp']) * 100)

    # Member since
    member_since = user_data.get('created_at', datetime.utcnow()).strftime("%B %Y")
    streak = user_data.get('streak_count', 1)
    return render_template(
        'settings.html',
        user=user_data,
        xp=xp,
        level_info=level_info,
        progress_pct=progress_pct,
        member_since=member_since,
        streak=streak
    )

@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')

    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # Verify current password
    if not bcrypt.check_password_hash(user_data.get('password', ''), current_password):
        flash('Incorrect current password.', 'error')
        return redirect(url_for('settings'))

    # Check if new passwords match
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('settings'))

    # Validate new password strength
    password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
    if not password_pattern.match(new_password):
        flash('Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol.', 'error')
        return redirect(url_for('settings'))

    # Update password
    hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'password': hashed_password}}
    )

    flash('Password updated successfully!', 'success')
    return redirect(url_for('settings'))

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    user_id = ObjectId(current_user.id)

    # Delete all user data
    projects_collection.delete_many({'created_by_id': user_id})
    roadmaps_collection.delete_many({'user_id': user_id})
    chat_history_collection.delete_one({'user_id': user_id})
    users_collection.delete_one({'_id': user_id})

    logout_user()
    flash('Your account and all associated data have been deleted.', 'info')
    return redirect(url_for('index'))


# ════════════════════════════════════════════════════════════
# LEADERBOARD ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/leaderboard')
@login_required
def leaderboard():
    from datetime import timedelta

    # ── ALL TIME: top 50 by total XP ──
    all_time = list(users_collection.find(
        {}, {'name': 1, 'xp': 1, 'level': 1, 'badge': 1, 'streak_count': 1, 'profile_pic': 1}
    ).sort('xp', -1).limit(50))

    # ── WEEKLY: top 50 by weekly_xp, reset every Monday ──
    now = datetime.utcnow()
    # Days since last Monday
    days_since_monday = now.weekday()  # Monday=0
    week_start = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)

    weekly = list(users_collection.find(
        {'weekly_xp_updated': {'$gte': week_start}},
        {'name': 1, 'weekly_xp': 1, 'level': 1, 'badge': 1, 'streak_count': 1, 'profile_pic': 1}
    ).sort('weekly_xp', -1).limit(50))

    # Compute current user rank in all-time
    current_user_xp = 0
    current_user_rank = None
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'xp': 1})
    if user_data:
        current_user_xp = user_data.get('xp', 0)
        current_user_rank = users_collection.count_documents({'xp': {'$gt': current_user_xp}}) + 1

    # Add profile pic URLs and stringify IDs
    for u in all_time:
        u['_id'] = str(u['_id'])
        u['xp'] = u.get('xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"

    for u in weekly:
        u['_id'] = str(u['_id'])
        u['weekly_xp'] = u.get('weekly_xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"

    # Next Monday reset time
    days_until_monday = (7 - days_since_monday) % 7 or 7
    next_reset = (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    hours_until_reset = int((next_reset - now).total_seconds() / 3600)

    # Sidebar widget data
    level_info = get_level_info(current_user_xp)
    progress_pct = int((current_user_xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
    user_full = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'streak_count': 1})
    streak = user_full.get('streak_count', 1) if user_full else 1

    return render_template('leaderboard.html',
        all_time=all_time,
        weekly=weekly,
        current_user_rank=current_user_rank,
        current_user_xp=current_user_xp,
        hours_until_reset=hours_until_reset,
        week_start=week_start.strftime('%b %d'),
        level_info=level_info,
        progress_pct=progress_pct,
        streak=streak
    )

# ════════════════════════════════════════════════════════════
# GAMIFICATION API ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/api/check_levelup')
@login_required
def check_levelup():
    """Called by frontend to check if a level-up toast should be shown"""
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    pending = user_data.get('pending_levelup')
    if pending:
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$unset': {'pending_levelup': ''}})
        return jsonify({'levelup': True, 'badge': pending})
    return jsonify({'levelup': False})

@app.route('/api/xp_status')
@login_required
def xp_status():
    """Returns current XP, level, streak for sidebar/dashboard live display"""
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    xp = user_data.get('xp', 0)
    level_info = get_level_info(xp)
    streak = user_data.get('streak_count', 0)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != "Max" else 100
    return jsonify({
        'xp': xp,
        'level': level_info['level'],
        'badge': level_info['badge'],
        'icon': level_info['icon'],
        'next_xp': level_info['next_xp'],
        'progress_pct': progress_pct,
        'streak': streak
    })

# --- Main Execution ---
if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'profile_pics'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_img'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_assets'), exist_ok=True)
    print(f"Ensured upload folder exists at: {app.config['UPLOAD_FOLDER']}")
    app.run(debug=True)