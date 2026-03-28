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
from flask_limiter import Limiter                         # ← ADDED
from flask_limiter.util import get_remote_address         # ← ADDED
from flask_limiter.errors import RateLimitExceeded        # ← ADDED
import bleach                                             # ← ADDED: XSS sanitization
from apscheduler.schedulers.background import BackgroundScheduler  # ← ADDED: weekly XP reset
from apscheduler.triggers.cron import CronTrigger                  # ← ADDED
import pytz                                                         # ← ADDED
import os
from dotenv import load_dotenv
import requests

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
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024      # ← ADDED: 16MB max upload size
bcrypt = Bcrypt(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))
s = URLSafeTimedSerializer(app.secret_key)

# ── RATE LIMITER ──────────────────────────────────────────  # ← ADDED
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ── SMART RATE LIMIT KEY FUNCTIONS ────────────────────────  # ← ADDED
def get_login_key():
    """Rate limit by IP + target email combined.
    Prevents both credential stuffing (many accounts, one IP)
    and targeted brute force (one account, one IP).
    50 students on same WiFi are NOT affected — each has unique ip:email bucket."""
    email = request.form.get('email', '').strip().lower()
    ip = get_remote_address()
    return f"{ip}:{email}"

def get_user_key():
    """Rate limit by user ID when logged in, fallback to IP.
    Prevents VPN bypass on AI endpoints — quota is per account, not per IP."""
    if current_user and current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()

def sanitize(text):
    """Strip ALL HTML tags from user input — prevents XSS attacks.
    tags=[] means no HTML allowed at all, plain text only."""
    if not text:
        return ""
    return bleach.clean(text.strip(), tags=[], strip=True)


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
    # This looks for MONGO_URI in your .env file. 
    # If it's not there, it falls back to your local computer.
    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/skillbridge_db")
    client = MongoClient(mongo_uri)
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
    notifications_collection = db["notifications"]
    xp_history_collection = db["xp_history"]
    comments_collection = db["comments"]
    # Indexes for comments
    comments_collection.create_index([("project_id", 1), ("created_at", -1)])
    comments_collection.create_index([("parent_id", 1)])
except Exception as e:
    print(f"ERROR: Could not connect to MongoDB. Please ensure it's running. Details: {e}")
    exit()

# ════════════════════════════════════════════════════════════
# WEEKLY XP RESET SCHEDULER
# ════════════════════════════════════════════════════════════

def reset_weekly_xp():
    """
    Runs every Monday at 12:00 AM IST.
    Resets weekly_xp to 0 for ALL users — clean slate every week.
    Prevents stale XP data from accumulating in MongoDB.
    """
    try:
        result = users_collection.update_many(
            {},
            {'$set': {'weekly_xp': 0, 'weekly_xp_updated': None}}
        )
        print(f"✅ Weekly XP Reset complete — {result.modified_count} users reset to 0")
    except Exception as e:
        print(f"❌ Weekly XP Reset failed: {e}")

# Start the background scheduler
IST_TZ = pytz.timezone('Asia/Kolkata')
scheduler = BackgroundScheduler(timezone=IST_TZ)

# ── IST HELPERS ───────────────────────────────────────────────────────────────
def now_ist():
    """Current datetime in IST (naive) — use instead of datetime.utcnow()."""
    return datetime.now(IST_TZ).replace(tzinfo=None)

def to_ist(dt):
    """Convert a UTC naive datetime from MongoDB to IST naive for display."""
    if dt is None or not isinstance(dt, datetime):
        return now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.utc)
    return dt.astimezone(IST_TZ).replace(tzinfo=None)

def fmt_ist(dt, fmt='%I:%M %p'):
    """Convert UTC datetime to IST and format as string."""
    return to_ist(dt).strftime(fmt)
# ─────────────────────────────────────────────────────────────────────────────
scheduler.add_job(
    reset_weekly_xp,
    CronTrigger(day_of_week='mon', hour=0, minute=0, timezone=IST_TZ),
    id='weekly_xp_reset',
    replace_existing=True
)
scheduler.start()
print("✅ Weekly XP reset scheduler started — resets every Monday 12:00 AM IST")

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

def check_ai_daily_limit(user_id, action='chat', limit=50):
    """
    Check and increment AI usage for a user per day in IST timezone.
    action: 'chat' (limit=50) or 'roadmap' (limit=10)
    Returns True if allowed, False if limit hit.
    Auto-cleans usage entries older than 7 days.
    """
    try:
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).strftime('%Y-%m-%d')
        key = f"{action}_{today_ist}"

        user = users_collection.find_one(
            {'_id': ObjectId(user_id)},
            {'ai_usage': 1}
        )
        if not user:
            return False, 0, limit

        ai_usage = user.get('ai_usage', {})
        today_count = ai_usage.get(key, 0)

        if today_count >= limit:
            return False, today_count, limit # limit hit

        # Increment today's count
        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$inc': {f'ai_usage.{key}': 1}}
        )

        # Auto-cleanup: remove entries older than 7 days (runs silently)
        cutoff = (datetime.now(IST) - timedelta(days=7)).strftime('%Y-%m-%d')
        cleaned = {k: v for k, v in ai_usage.items() if k.split('_')[-1] >= cutoff}
        if len(cleaned) < len(ai_usage):
            users_collection.update_one(
                {'_id': ObjectId(user_id)},
                {'$set': {'ai_usage': cleaned}}
            )

        return True, today_count + 1, limit

    except Exception as e:
        print(f"AI limit check error: {e}")
        return True, 0, limit  # fail open — don’t block user on DB error

def get_ai_usage_today(user_id):
    """Returns today's AI usage counts for a user (IST). Used in settings + admin."""
    try:
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).strftime('%Y-%m-%d')
        user = users_collection.find_one({'_id': ObjectId(user_id)}, {'ai_usage': 1})
        if not user:
            return {'chat_used': 0, 'chat_limit': 50, 'roadmap_used': 0, 'roadmap_limit': 10}
        ai_usage = user.get('ai_usage', {})
        return {
            'chat_used':     ai_usage.get(f'chat_{today_ist}', 0),
            'chat_limit':    50,
            'roadmap_used':  ai_usage.get(f'roadmap_{today_ist}', 0),
            'roadmap_limit': 10,
        }
    except Exception as e:
        print(f"get_ai_usage_today error: {e}")
        return {'chat_used': 0, 'chat_limit': 50, 'roadmap_used': 0, 'roadmap_limit': 10}

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
# RATE LIMIT ERROR HANDLER                                    # ← ADDED
# ════════════════════════════════════════════════════════════

@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': 'Too many requests. Please slow down and try again later.'}), 429
    return render_template('429.html'), 429

@app.errorhandler(413)
def handle_file_too_large(e):
    flash('File is too large. Maximum allowed size is 16MB.', 'error')
    return redirect(request.referrer or url_for('main_page'))

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500

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
        
        # --- BULLETPROOF WEEKLY RESET LOGIC ---
        last_updated = user.get('weekly_xp_updated')
        from datetime import timedelta
        now_time = now_ist()
        days_since_monday = now_time.weekday()
        week_start = (now_time - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)

        # If last updated is older than this week's Monday, reset to 0 before adding new points
        if not last_updated or last_updated < week_start:
            users_collection.update_one(
                {'_id': ObjectId(user_id)},
                {'$set': {'weekly_xp': points, 'weekly_xp_updated': now_time}}
            )
        else:
            users_collection.update_one(
                {'_id': ObjectId(user_id)},
                {'$inc': {'weekly_xp': points}, '$set': {'weekly_xp_updated': now_time}}
            )
        # --------------------------------------

        # Log XP history
        xp_history_collection.insert_one({
            'user_id': ObjectId(user_id),
            'points': points,
            'reason': reason,
            'type': 'earn',
            'timestamp': now_ist()
        })

        if new_level_info['level'] > old_level:
            print(f"🎉 LEVEL UP! User {user.get('name')} is now Level {new_level_info['level']} ({new_level_info['badge']})")
            users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': {'pending_levelup': new_level_info['badge']}})
            create_notification(str(user_id), 'levelup', f'🎉 You reached Level {new_level_info["level"]}: {new_level_info["badge"]}!', url_for('settings'))

        return True
    except Exception as e:
        print(f"Error adding XP: {e}")
        return False


def recalculate_xp(user_id):
    """Recalculates XP from scratch based on actual user data in DB.
    Fixes any double-counting or incorrect XP values."""
    try:
        uid = ObjectId(user_id)
        user = users_collection.find_one({'_id': uid})
        if not user:
            return False

        xp = 0

        # 1. GitHub connected — +25 (one-time)
        if user.get('github_reward_claimed'):
            xp += 25

        # 2. Profile 100% complete — +30 (one-time)
        if user.get('profile_complete_reward'):
            xp += 30

        # 3. Projects created — +10 each
        project_count = projects_collection.count_documents({'created_by_id': uid})
        xp += project_count * 10

        # 4. Project versions uploaded — +50 each
        upload_count = commits_collection.count_documents({'user_id': uid})
        xp += upload_count * 50

        # 5. Certificates — +15 each
        cert_count = len(user.get('certificates', []))
        xp += cert_count * 15

        # 6. Completed roadmap stages — +5 each
        user_roadmaps = list(roadmaps_collection.find({'user_id': uid}))
        completed_stages = 0
        for roadmap in user_roadmaps:
            content_data = roadmap.get('roadmap_content', {})
            if isinstance(content_data, str):
                import json as _json
                try: content_data = _json.loads(content_data)
                except: content_data = {}
            stages = content_data.get('stages', []) if isinstance(content_data, dict) else []
            completed_stages += sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))
        xp += completed_stages * 5

        # 7. Streak bonuses already earned — keep them
        # These are already baked into XP from add_xp calls, but we track separately
        xp += user.get('streak_bonus_xp', 0)

        # Update DB with correct XP
        level_info = get_level_info(xp)
        users_collection.update_one(
            {'_id': uid},
            {'$set': {
                'xp': xp,
                'level': level_info['level'],
                'badge': level_info['badge']
            }}
        )
        print(f"✅ Recalculated XP for {user.get('name')}: {xp} XP ({level_info['badge']})")
        return xp
    except Exception as e:
        print(f"Recalculate XP error: {e}")
        return False

def deduct_xp(user_id, points, reason="Action Reversed"):
    """User ke account se XP minus karne ka function (abuse prevention)"""
    try:
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        if not user: return False

        current_xp = user.get('xp', 0)
        current_weekly_xp = user.get('weekly_xp', 0)

        new_xp = max(0, current_xp - points)  # XP kabhi 0 se neeche nahi jayega
        new_weekly_xp = max(0, current_weekly_xp - points) # Weekly XP bhi 0 se neeche nahi jayega
        
        new_level_info = get_level_info(new_xp)

        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {
                'xp': new_xp, 
                'weekly_xp': new_weekly_xp, 
                'level': new_level_info['level'], 
                'badge': new_level_info['badge']
            }}
        )
        print(f"📉 XP DEDUCTED: -{points} from user {user.get('name')} for: {reason}")
        # Log XP history
        xp_history_collection.insert_one({
            'user_id': ObjectId(user_id),
            'points': -points,
            'reason': reason,
            'type': 'deduct',
            'timestamp': now_ist()
        })
        return True
    except Exception as e:
        print(f"Error deducting XP: {e}")
        return False

def update_streak(user_id):
    """Login par activity streak track karne ka function"""
    try:
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        last_login = user.get('last_activity_date')
        streak = user.get('streak_count', 0)

        if last_login:
            if isinstance(last_login, str):
                last_login = datetime.fromisoformat(last_login.replace("Z", "+00:00"))
            if last_login.tzinfo is None:
                last_login = last_login.replace(tzinfo=IST)
            # Convert last_login to IST before comparing
            last_login = last_login.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
            diff = (today - last_login).days

            if diff == 0:  # Same day login — keep streak, just update timestamp
                pass
            elif diff == 1:  # Consecutive day — increment
                streak += 1
                if streak == 7:
                    add_xp(user_id, 35, "7-day activity streak")
                    users_collection.update_one({'_id': ObjectId(user_id)}, {'$inc': {'streak_bonus_xp': 35}})
                    print(f"🔥 7-DAY STREAK! User {user.get('name')} earned 35 XP")
                if streak == 30:
                    add_xp(user_id, 100, "30-day activity streak")
                    users_collection.update_one({'_id': ObjectId(user_id)}, {'$inc': {'streak_bonus_xp': 100}})
                    print(f"🔥🔥 30-DAY STREAK! User {user.get('name')} earned 100 XP")
            else:  # Streak broken
                streak = 1
        else:
            streak = 1  # First ever login — set to 1 immediately

        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'streak_count': streak, 'last_activity_date': now_ist()}}
        )
    except Exception as e:
        print(f"Streak update error: {e}")


def create_notification(user_id, notif_type, message, link="#"):
    """Create a notification for a user"""
    try:
        notifications_collection.insert_one({
            'user_id': ObjectId(user_id),
            'type': notif_type,       # 'message', 'approved', 'declined', 'removed', 'levelup'
            'message': message,
            'link': link,
            'is_read': False,
            'created_at': datetime.now(timezone.utc).replace(tzinfo=None)
        })
    except Exception as e:
        print(f"Notification error: {e}")

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
@limiter.limit("10 per hour")                             # ← ADDED
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main_page'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not name or not email or not password or not username:
             flash("All fields are required.", "error"); return redirect(url_for('signup'))
        import re as _re
        if not _re.match(r'^[a-z0-9_]{3,20}$', username):
            flash("Username must be 3-20 characters, letters/numbers/underscores only.", "error")
            return redirect(url_for('signup'))
        if users_collection.find_one({'username': username}):
            flash("That username is already taken. Try another.", "error")
            return redirect(url_for('signup'))
        if password != confirm_password:
            flash("Passwords do not match.", "error"); return redirect(url_for('signup'))
        password_pattern = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$')
        if not password_pattern.match(password):
            flash("Password must be at least 8 characters and include uppercase, lowercase, number, and special symbol (@$!%*?&).", "error"); return redirect(url_for('signup'))
        if users_collection.find_one({'email': email}):
            flash("An account with this email already exists. Try logging in.", "error"); return redirect(url_for('login'))
        otp = random.randint(100000, 999999)
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        session['temp_user_data'] = {'name': name, 'email': email, 'password': hashed_password, 'username': username}
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

@app.route('/api/check_username')
def check_username():
    """Live username availability check — called as user types in signup"""
    username = request.args.get('username', '').strip().lower()
    if not username:
        return jsonify({'available': False, 'error': 'Empty'})
    import re as _re
    if not _re.match(r'^[a-z0-9_]{3,20}$', username):
        return jsonify({'available': False, 'error': 'Username must be 3-20 chars, letters/numbers/underscores only'})
    existing = users_collection.find_one({'username': username}, {'_id': 1})
    return jsonify({'available': existing is None})

@app.route('/verify', methods=['GET', 'POST'])
@limiter.limit("10 per 15 minutes")                       # ← ADDED
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
                    user_data['username'] = user_data.get('username', '')
                    user_data['created_at'] = now_ist()
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
@limiter.limit("20 per hour", key_func=get_login_key)     # ← UPDATED: IP+email key
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
        if user_data and not user_data.get('is_banned') and bcrypt.check_password_hash(user_data.get('password', ''), password):
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
@limiter.limit("5 per hour")                              # ← ADDED
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
@limiter.limit("5 per hour")                              # ← ADDED
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

@app.before_request
def auto_update_streak():
    """Runs before every request — ensures streak is updated even without re-login"""
    if current_user.is_authenticated:
        from datetime import timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).date()
        # Store last checked date in session to avoid hitting DB on every single request
        last_checked = session.get('streak_checked_date')
        if last_checked != str(today_ist):
            update_streak(current_user.id)
            session['streak_checked_date'] = str(today_ist)

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

        # Work experience entries — only save if not Student with 0 years
        import json as _json
        exp_entries_raw = request.form.get('work_experience_json', '[]')
        try:
            exp_entries = _json.loads(exp_entries_raw)
            # Sanitize each entry
            clean_entries = []
            for e in exp_entries:
                if e.get('company','').strip() and e.get('role','').strip():
                    clean_entries.append({
                        'company': sanitize(e.get('company','')),
                        'role':    sanitize(e.get('role','')),
                        'from_year': sanitize(e.get('from_year','')),
                        'to_year':   sanitize(e.get('to_year','')),
                        'is_current': bool(e.get('is_current', False))
                    })
            update_data['work_experience'] = clean_entries
        except Exception:
            pass

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
        'work_experience': user_data.get('work_experience', []),
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
@limiter.limit("15 per hour", key_func=get_user_key)      # ← UPDATED: per-account key, VPN-proof
def roadmap_generator():
    roadmap_data = None
    goal = ""
    if request.method == 'POST':
        goal = request.form.get('goal', '').strip()
        if not goal:
            flash("Please enter a goal for your roadmap.", "error")
            return render_template('roadmap_generator.html', goal=goal)

        # ── AI DAILY CAP CHECK (IST) ──────────────────────────
        allowed, used, limit = check_ai_daily_limit(current_user.id, 'roadmap')
        if not allowed:
            flash(f'⚠️ You have used all {limit} roadmap generations for today. Resets at midnight IST.', 'error')
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
                roadmaps_collection.insert_one({'user_id': ObjectId(current_user.id), 'goal': goal, 'roadmap_content': roadmap_data, 'created_at': now_ist()})
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
        is_pre = roadmap.get('pre_gamification', False)
        roadmaps_collection.delete_one({'_id': obj_id})
        deducted = 0
        if not is_pre and completed_stages > 0:
            deduct_xp(current_user.id, completed_stages * 5, f"Deleted roadmap ({completed_stages} completed stages)")
            deducted = completed_stages * 5
        return jsonify({'success': True, 'deducted': deducted})
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
                add_xp(current_user.id, 5, "Completed Roadmap Stage")
                flash('Stage marked as complete! +5 XP 🎉', 'success')
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
        PER_PAGE = 10
        page = request.args.get('page', 1, type=int)
        recommended_projects = []
        profile_incomplete = False
        is_authenticated = current_user.is_authenticated

        # Total count of ALL projects for display
        total = projects_collection.count_documents({})

        if is_authenticated:
            recommended_projects = get_recommended_projects(current_user.id, users_collection, projects_collection)
            rec_ids = [p['_id'] for p in recommended_projects]

            # other_projects = all projects NOT in recommended
            other_query = {'_id': {'$nin': rec_ids}} if rec_ids else {}
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
            other_projects = list(projects_collection.find()
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

        return render_template('projects.html',
            recommended_projects=recommended_projects,
            other_projects=other_projects,
            profile_incomplete=profile_incomplete,
            is_authenticated=is_authenticated,
            page=page,
            total_pages=total_pages,
            total=other_total,
            has_more=has_more,
            user_bookmark_ids=user_bookmark_ids)
    except Exception as e:
        print(f"Error fetching community projects: {e}")
        flash("Could not load community projects at this time.", "error")
        return render_template('projects.html', recommended_projects=[], other_projects=[],
            profile_incomplete=False, is_authenticated=current_user.is_authenticated,
            page=1, total_pages=1, total=0, has_more=False)

@app.route('/api/projects')
def api_projects():
    """JSON endpoint for Load More button on community projects page"""
    try:
        PER_PAGE = 10
        page = request.args.get('page', 1, type=int)
        is_authenticated = current_user.is_authenticated

        if is_authenticated:
            recommended_ids = [p['_id'] for p in get_recommended_projects(current_user.id, users_collection, projects_collection)]
            query = {'_id': {'$nin': recommended_ids}}
        else:
            query = {}

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

        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

        # ── BOOKMARKED PROJECTS ──────────────────────────────
        user_data = users_collection.find_one(
            {'_id': ObjectId(current_user.id)}, {'bookmarks': 1}
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
                bookmarked_projects.append(p)

        return render_template('my_projects.html',
            projects=my_projects_list,
            page=page,
            total_pages=total_pages,
            total=total,
            bookmarked_projects=bookmarked_projects)
    except Exception as e:
        print(f"Error fetching user projects for {current_user.id}: {e}")
        flash("Could not load your projects.", "error")
        return render_template('my_projects.html', projects=[], page=1, total_pages=1, total=0,
            bookmarked_projects=[])

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
        project = projects_collection.find_one(
            {'_id': ObjectId(project_id), 'created_by_id': ObjectId(current_user.id)}
        )
        if project:
            projects_collection.delete_one({'_id': ObjectId(project_id)})
            if not project.get('pre_gamification'):
                deduct_xp(current_user.id, 10, "Project Deleted")
                flash("Project deleted. -10 XP", "info")
            else:
                flash("Project deleted.", "info")
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
        {'project_id': obj_id, 'parent_id': None}
    ).sort('created_at', -1))

    def fmt_date(dt):
        if dt and hasattr(dt, 'strftime'):
            return dt.strftime('%b %d, %Y')
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
        # Fetch replies (1 level only)
        replies = list(comments_collection.find(
            {'parent_id': c_id_obj}
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

    return render_template('project_page.html', project=project, is_owner=is_owner,
        commits=commits, creator_name=creator_name,
        user_liked=user_liked, like_count=like_count,
        comments=comments, comment_count=comment_count, is_admin=is_admin,
        user_bookmarked=user_bookmarked, bookmark_count=bookmark_count)

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


# ════════════════════════════════════════════════════════════
# BOOKMARK ROUTES
# ════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════
# COMMENT SYSTEM ROUTES
# ════════════════════════════════════════════════════════════

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
        comment_id = comments_collection.insert_one({
            'project_id': obj_id,
            'user_id': ObjectId(current_user.id),
            'user_name': user.get('name', current_user.name),
            'username': user.get('username', ''),
            'profile_pic': user.get('profile_pic', 'default.jpg'),
            'content': content,
            'parent_id': None,
            'likes': [],
            'created_at': now_ist(),
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
        reply_id = comments_collection.insert_one({
            'project_id': obj_id,
            'user_id': ObjectId(current_user.id),
            'user_name': user.get('name', current_user.name),
            'username': user.get('username', ''),
            'profile_pic': user.get('profile_pic', 'default.jpg'),
            'content': content,
            'parent_id': parent_id,
            'likes': [],
            'created_at': now_ist(),
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
        raw_top = list(comments_collection.find({'project_id': obj_id, 'parent_id': None}))
        if sort_by == 'top':
            raw_top.sort(key=lambda c: len(c.get('likes', [])), reverse=True)
        else:
            raw_top.sort(key=lambda c: c.get('created_at', datetime.min), reverse=True)
        result = []
        for c in raw_top:
            replies = list(comments_collection.find({'parent_id': c['_id']}).sort('created_at', 1))
            reply_list = []
            for r in replies:
                reply_list.append({
                    '_id': str(r['_id']),
                    'user_name': r.get('user_name', ''),
                    'username': r.get('username', ''),
                    'profile_pic': r.get('profile_pic', 'default.jpg'),
                    'content': r.get('content', ''),
                    'created_at': fmt_ist(r.get('created_at'), '%b %d, %Y'),
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
                'created_at': fmt_ist(c.get('created_at'), '%b %d, %Y'),
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
        work_experience = user_data.get('work_experience', [])
        return render_template("resume_builder.html", user=user_data, projects=user_projects, certificates=certificates, github_repos=github_repos, github_langs=github_langs, work_experience=work_experience)
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
@limiter.limit("30 per hour", key_func=get_user_key)      # ← UPDATED: per-account key, VPN-proof
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

        # ── AI DAILY CAP CHECK (IST) ──────────────────────────
        allowed, used, limit = check_ai_daily_limit(current_user.id, 'chat')
        if not allowed:
            return jsonify({'reply': f'⚠️ You have used all {limit} AI messages for today. Your limit resets at midnight IST. Come back tomorrow!'}), 429

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
            {'$push': {'messages': {'$each': new_turns, '$slice': -50}}, '$set': {'last_updated': datetime.now(timezone.utc)}, '$setOnInsert': {'user_id': uid, 'created_at': datetime.now(timezone.utc)}},
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
        work_experience = user_data.get('work_experience', [])
        return render_template('portfolio_builder.html', user=user_data, projects=user_projects, certificates=certificates, github_repos=github_repos, github_langs=github_langs, work_experience=work_experience)
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
            'availability': user_data.get('availability', ''),
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
            conversations.append({"user_id": str(other_user["_id"]), "user_name": other_user.get("name", "Unknown"), "username": other_user.get("username", ""), "profile_pic": other_user.get("profile_pic", "default.jpg"), "last_message": res.get("last_message", ""), "timestamp": fmt_ist(res["timestamp"], "%b %d, %I:%M %p"), "is_unread": is_unread, "sent_by_me": sent_by_me, "sender_name": sender_name})
        return render_template("messages_list.html", conversations=conversations)
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
        return render_template('chat.html', receiver=rec, messages=history)
    except Exception as e:
        print(f"Chat Error: {e}"); return redirect(url_for('messages_list'))

@app.route('/community/<community_id>')
@login_required
def view_community(community_id):
    try:
        community = communities_collection.find_one({"_id": ObjectId(community_id)})
    except Exception:
        flash("Invalid community.", "error"); return redirect(url_for("find_communities"))
    if not community:
        flash("Community not found", "error"); return redirect(url_for("find_communities"))

    user_id   = ObjectId(current_user.id)
    is_owner  = community.get("owner_id") == user_id
    is_admin  = user_id in community.get("admins", [])
    is_member = user_id in community.get("members", [])
    is_public = community.get("visibility") == "public"

    member_users = list(users_collection.find(
        {"_id": {"$in": community.get("members", [])}}, {"name": 1, "username": 1}
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
        ).sort("timestamp", 1))
        for m in raw_msgs:
            # now_ist() stores naive IST already. to_ist() would add 5:30 again - use directly.
            ts = m.get("timestamp")
            if not ts or not isinstance(ts, datetime):
                ts = now_ist()
            m["timestamp"] = ts
            m["timestamp_iso"] = ts.strftime('%Y-%m-%dT%H:%M:%S.') + f'{ts.microsecond:06d}'
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

# NOTE: The old form-based /community/<id>/send route has been removed.
# The chat frontend uses the JSON API at /api/community/<id>/send (see below).
# Keeping this route would create a duplicate path that bypasses rate limiting.

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
        {"members": user_id, "owner_id": {"$ne": user_id}}
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
    other_query = {"_id": {"$nin": exclude_ids}}
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

    return render_template("find_communities.html",
        my_communities=my_communities,
        joined_communities=joined_communities,
        other_communities=other_communities,
        other_total=other_total,
        page=page,
        total_pages=total_pages,
        has_more=has_more)

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


# ════════════════════════════════════════════════════════════
# CERTIFICATES ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/certificates/add', methods=['POST'])
@login_required
@limiter.limit("20 per hour", key_func=get_user_key)      # ← UPDATED: per-account key
def add_certificate():
    try:
        data = request.get_json()
        cert = {'_id': ObjectId(), 'name': data.get('name', '').strip(), 'issuer': data.get('issuer', '').strip(), 'issue_date': data.get('issue_date', '').strip(), 'expiry_date': data.get('expiry_date', '').strip(), 'credential_id': data.get('credential_id', '').strip(), 'cert_url': data.get('cert_url', '').strip(), 'added_at': now_ist()}
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
        user = users_collection.find_one({'_id': ObjectId(current_user.id)}, {'pre_gamification_certs': 1})
        result = users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$pull': {'certificates': {'_id': ObjectId(cert_id)}}})
        if result.modified_count:
            if not user.get('pre_gamification_certs'):
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

@app.route('/api/github/save', methods=['POST'])
@login_required
def github_save():
    try:
        data = request.get_json()
        github_data = data.get('github_data', {})
        username = github_data.get('username', '')
        if not username:
            return jsonify({'success': False, 'error': 'No GitHub data to save'}), 400

        users_collection.update_one(
            {'_id': ObjectId(current_user.id)},
            {'$set': {
                'github_username': username,
                'github_avatar': github_data.get('avatar', ''),
                'github_url': github_data.get('github_url', ''),
                'github_repos': github_data.get('repos', []),
                'github_langs': github_data.get('top_languages', []),
                'github_followers': github_data.get('followers', 0),
                'github_public_repos': github_data.get('public_repos', 0),
                'github_synced_at': now_ist()
            }}
        )

        # One-time XP reward for connecting GitHub
        user = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user.get('github_reward_claimed'):
            add_xp(current_user.id, 25, "Connected GitHub profile")
            users_collection.update_one(
                {'_id': ObjectId(current_user.id)},
                {'$set': {'github_reward_claimed': True}}
            )

        return jsonify({'success': True, 'message': 'GitHub profile saved!'})
    except Exception as e:
        print(f"GitHub save error: {e}")
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
    member_since = to_ist(user_data.get('created_at')).strftime('%B %Y')
    streak = user_data.get('streak_count', 1)

    ai_usage = get_ai_usage_today(current_user.id)  # ← AI daily cap data

    return render_template(
        'settings.html',
        user=user_data,
        xp=xp,
        level_info=level_info,
        progress_pct=progress_pct,
        member_since=member_since,
        streak=streak,
        ai_usage=ai_usage,
        username=user_data.get('username', '')
    )

@app.route('/change_password', methods=['POST'])
@login_required
@limiter.limit("5 per hour")                              # ← ADDED
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
    commits_collection.delete_many({'user_id': user_id})
    chat_history_collection.delete_one({'user_id': user_id})
    notifications_collection.delete_many({'user_id': user_id})
    xp_history_collection.delete_many({'user_id': user_id})
    comments_collection.delete_many({'user_id': user_id})
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
    now = now_ist()
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


@app.route('/api/recalculate_xp')
@login_required
def api_recalculate_xp():
    """Recalculates current user XP from scratch — fixes any double counting"""
    new_xp = recalculate_xp(current_user.id)
    if new_xp is not False:
        level_info = get_level_info(new_xp)
        return jsonify({'success': True, 'xp': new_xp, 'badge': level_info['badge']})
    return jsonify({'success': False}), 500



@app.route('/api/xp_history')
@login_required
def xp_history():
    """Returns full XP transaction history for current user"""
    history = list(xp_history_collection.find(
        {'user_id': ObjectId(current_user.id)}
    ).sort('timestamp', -1))

    result = []
    for h in history:
        result.append({
            'points': h.get('points', 0),
            'reason': h.get('reason', 'Unknown'),
            'type': h.get('type', 'earn'),
            'timestamp': fmt_ist(h.get('timestamp'), '%Y-%m-%d %H:%M'),
            'date_only': fmt_ist(h.get('timestamp'), '%b %d, %Y'),
            'day_key': fmt_ist(h.get('timestamp'), '%Y-%m-%d'),
        })

    # Build daily chart data (last 30 days)
    from collections import defaultdict
    daily = defaultdict(int)
    for h in result:
        if h['type'] == 'earn':
            daily[h['day_key']] += h['points']

    # Sort by date
    daily_sorted = sorted(daily.items())
    chart_data = [{'date': d, 'xp': x} for d, x in daily_sorted]

    return jsonify({
        'history': result,
        'chart_data': chart_data,
        'total_earned': sum(h['points'] for h in result if h['type'] == 'earn'),
        'total_deducted': abs(sum(h['points'] for h in result if h['type'] == 'deduct')),
        'total_transactions': len(result)
    })


@app.route('/api/community/<community_id>/messages')
@login_required
def get_community_messages(community_id):
    """Returns messages since a given timestamp for real-time polling"""
    since = request.args.get('since', None)
    query = {"community_id": ObjectId(community_id)}
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is not None:
                since_dt = since_dt.replace(tzinfo=None)
            query["timestamp"] = {"$gt": since_dt}
        except Exception as e:
            print(f"Since parse error: {e}")

    messages = list(community_messages_collection.find(query).sort("timestamp", 1))
    result = []
    for msg in messages:
        ts = msg.get('timestamp')
        if not ts or not isinstance(ts, datetime):
            ts = now_ist()
        reactions_raw = msg.get('reactions', {})
        reactions = {k: (len(v) if isinstance(v, list) else int(v)) for k, v in reactions_raw.items()}
        result.append({
            'id':            str(msg['_id']),
            'sender_id':     str(msg.get('sender_id', '')),
            'sender_name':   msg.get('sender_name', 'Unknown'),
            'message':       msg.get('message', ''),
            # ts is already IST naive (saved by now_ist()) - strftime directly.
            'timestamp':     ts.strftime('%I:%M %p') + ' IST',
            'timestamp_iso': ts.strftime('%Y-%m-%dT%H:%M:%S.') + f'{ts.microsecond:06d}',
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


# ════════════════════════════════════════════════════════════
# ONBOARDING ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/api/onboarding/status')
@login_required
def onboarding_status():
    """Returns onboarding checklist status for current user"""
    user = users_collection.find_one({'_id': ObjectId(current_user.id)})

    # If already completed, return immediately
    if user.get('onboarding_complete'):
        return jsonify({'show': False})

    uid = ObjectId(current_user.id)

    # Check each step
    profile_fields = ['about_me', 'location', 'education_college', 'education_degree', 'github_url']
    profile_done = all(user.get(f) for f in profile_fields)

    project_done = projects_collection.count_documents({'created_by_id': uid}) > 0
    roadmap_done = roadmaps_collection.count_documents({'user_id': uid}) > 0

    from bson.objectid import ObjectId as OID
    community_done = communities_collection.count_documents({'members': uid}) > 0

    steps = [
        {
            'id': 'profile',
            'title': 'Complete your profile',
            'desc': 'Add bio, location, education & GitHub URL',
            'xp': '+30 XP',
            'done': profile_done,
            'link': '/profile',
            'icon': 'fa-user-circle'
        },
        {
            'id': 'project',
            'title': 'Create your first project',
            'desc': 'Share what you are building with the community',
            'xp': '+10 XP',
            'done': project_done,
            'link': '/create_project',
            'icon': 'fa-layer-group'
        },
        {
            'id': 'roadmap',
            'title': 'Generate a learning roadmap',
            'desc': 'Use AI to map out your learning path',
            'xp': '+5 XP per stage',
            'done': roadmap_done,
            'link': '/roadmap_generator',
            'icon': 'fa-magic'
        },
        {
            'id': 'community',
            'title': 'Join a community',
            'desc': 'Connect with builders who share your interests',
            'xp': '+10 XP',
            'done': community_done,
            'link': '/communities',
            'icon': 'fa-users'
        },
    ]

    all_done = all(s['done'] for s in steps)
    if all_done:
        users_collection.update_one({'_id': uid}, {'$set': {'onboarding_complete': True}})
        return jsonify({'show': False})

    return jsonify({'show': True, 'steps': steps})

@app.route('/api/onboarding/dismiss', methods=['POST'])
@login_required
def onboarding_dismiss():
    """Mark onboarding as complete (dismissed by user)"""
    users_collection.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'onboarding_complete': True}}
    )
    return jsonify({'success': True})

# ════════════════════════════════════════════════════════════
# NOTIFICATION ROUTES
# ════════════════════════════════════════════════════════════

@app.route('/api/notifications')
@login_required
def get_notifications():
    """Get all notifications for current user"""
    notifs = list(notifications_collection.find(
        {'user_id': ObjectId(current_user.id)}
    ).sort('created_at', -1).limit(20))
    result = []
    for n in notifs:
        result.append({
            'id': str(n['_id']),
            'type': n.get('type', 'info'),
            'message': n.get('message', ''),
            'link': n.get('link', '#'),
            'is_read': n.get('is_read', False),
            'created_at': fmt_ist(n.get('created_at'), '%b %d, %I:%M %p')
        })
    unread_count = notifications_collection.count_documents(
        {'user_id': ObjectId(current_user.id), 'is_read': False}
    )
    return jsonify({'notifications': result, 'unread_count': unread_count})

@app.route('/api/notifications/read/<notif_id>', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    """Mark a single notification as read"""
    notifications_collection.update_one(
        {'_id': ObjectId(notif_id), 'user_id': ObjectId(current_user.id)},
        {'$set': {'is_read': True}}
    )
    return jsonify({'success': True})

@app.route('/api/notifications/read_all', methods=['POST'])
@login_required
def mark_all_read():
    """Mark all notifications as read"""
    notifications_collection.update_many(
        {'user_id': ObjectId(current_user.id), 'is_read': False},
        {'$set': {'is_read': True}}
    )
    return jsonify({'success': True})

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
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$unset': {'pending_levelup': '$set'}})
        return jsonify({'levelup': True, 'badge': pending})
    return jsonify({'levelup': False})

@app.route('/search')
@login_required
def search():
    """Search users by name or @username"""
    query = request.args.get('q', '').strip()
    results = []
    if query:
        search_query = query.lstrip('@')
        users = list(users_collection.find(
            {'$or': [
                {'name': {'$regex': search_query, '$options': 'i'}},
                {'username': {'$regex': search_query, '$options': 'i'}},
            ]},
            {'name': 1, 'username': 1, 'title': 1, 'profile_pic': 1,
             'xp': 1, 'badge': 1, 'level': 1, 'known_skills': 1}
        ).limit(20))
        for u in users:
            if str(u['_id']) != current_user.id:  # exclude self
                results.append({
                    '_id': str(u['_id']),
                    'name': u.get('name', 'User'),
                    'username': u.get('username', ''),
                    'title': u.get('title', 'SkillBridge Member'),
                    'profile_pic': u.get('profile_pic', 'default.jpg'),
                    'xp': u.get('xp', 0),
                    'badge': u.get('badge', 'Bronze Builder'),
                    'known_skills': u.get('known_skills', [])[:4],
                })
    return render_template('search.html', results=results, query=query)

@app.route('/api/search_users')
@login_required
def api_search_users():
    """Live search API — called by dashboard search bar dropdown"""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})
    search_q = query.lstrip('@')
    users = list(users_collection.find(
        {'$or': [
            {'name': {'$regex': search_q, '$options': 'i'}},
            {'username': {'$regex': search_q, '$options': 'i'}},
        ]},
        {'name': 1, 'username': 1, 'title': 1, 'profile_pic': 1, 'badge': 1, 'xp': 1}
    ).limit(8))
    results = []
    for u in users:
        if str(u['_id']) != current_user.id:
            results.append({
                '_id': str(u['_id']),
                'name': u.get('name', 'User'),
                'username': u.get('username', ''),
                'title': u.get('title', 'SkillBridge Member'),
                'profile_pic': u.get('profile_pic', 'default.jpg'),
                'badge': u.get('badge', 'Bronze Builder'),
                'xp': u.get('xp', 0),
            })
    return jsonify({'results': results})

@app.route('/api/ai_usage')
@login_required
def api_ai_usage():
    """Returns today's AI usage for the current user (IST)"""
    usage = get_ai_usage_today(current_user.id)
    return jsonify(usage)

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

# ════════════════════════════════════════════════════════════
# ADMIN PANEL ROUTES
# ════════════════════════════════════════════════════════════

from functools import wraps

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in.", "error")
            return redirect(url_for('login'))
        user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
        if not user_data or not user_data.get('is_admin'):
            flash("Access denied. Admins only.", "error")
            return redirect(url_for('main_page'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    # Users
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime('%Y-%m-%d')

    users = list(users_collection.find({}, {
        'name': 1, 'email': 1, 'xp': 1, 'level': 1, 'badge': 1,
        'streak_count': 1, 'is_admin': 1, 'is_banned': 1,
        'created_at': 1, 'profile_pic': 1, 'ai_usage': 1, 'username': 1,
        'current_status': 1, 'work_experience': 1, 'experience_years': 1
    }).sort('xp', -1))
    from datetime import timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime('%Y-%m-%d')

    for u in users:
        u['_id'] = str(u['_id'])
        u['xp'] = u.get('xp', 0)
        u['streak_count'] = u.get('streak_count', 0)
        u['username'] = u.get('username', '')
        pic = u.get('profile_pic', 'default.jpg')
        u['profile_pic_url'] = f"/static/profile_pics/{pic}"
        ai = u.get('ai_usage', {})
        u['ai_chat_today'] = ai.get(f'chat_{today_ist}', 0)
        u['ai_roadmap_today'] = ai.get(f'roadmap_{today_ist}', 0)
        ai = u.get('ai_usage', {})
        u['ai_chat_today'] = ai.get(f'chat_{today_ist}', 0)
        u['ai_roadmap_today'] = ai.get(f'roadmap_{today_ist}', 0)

    # Projects — include views, likes, and comment counts
    projects = list(projects_collection.find().sort('created_at', -1))
    all_project_ids = [p['_id'] for p in projects]

    # Single aggregation for comment counts across all projects
    cmt_agg = comments_collection.aggregate([
        {'$match': {'project_id': {'$in': all_project_ids}, 'is_deleted': {'$ne': True}}},
        {'$group': {'_id': '$project_id', 'count': {'$sum': 1}}}
    ])
    cmt_counts = {str(doc['_id']): doc['count'] for doc in cmt_agg}

    for p in projects:
        p['_id'] = str(p['_id'])
        # Fetch username BEFORE converting created_by_id to string
        _creator_oid = p.get('created_by_id')
        creator = users_collection.find_one({'_id': _creator_oid}, {'username': 1}) if _creator_oid else None
        p['created_by_username'] = creator.get('username', '') if creator else ''
        p['created_by_id'] = str(_creator_oid or '')
        p['views'] = p.get('views', 0)
        p['like_count'] = len(p.get('likes', []))
        p['comment_count'] = cmt_counts.get(p['_id'], 0)
        p['bookmark_count'] = p.get('bookmark_count', 0)

    # Pending version approvals
    pending_commits = list(commits_collection.find({'xp_status': 'pending'}).sort('timestamp', -1))
    for c in pending_commits:
        c['_id'] = str(c['_id'])
        _proj_oid = c.get('project_id')
        _user_oid = c.get('user_id')
        c['project_id'] = str(_proj_oid or '')
        c['user_id'] = str(_user_oid or '')
        proj = projects_collection.find_one({'_id': _proj_oid}) if _proj_oid else None
        c['project_title'] = proj.get('title', 'Unknown') if proj else 'Unknown'
        # Add username to pending approval
        uploader = users_collection.find_one({'_id': _user_oid}, {'username': 1}) if _user_oid else None
        c['user_username'] = uploader.get('username', '') if uploader else ''

    # Communities
    communities = list(communities_collection.find().sort('created_at', -1))
    for c in communities:
        c['_id'] = str(c['_id'])
        c['owner_id'] = str(c.get('owner_id', ''))
        c['member_count'] = len(c.get('members', []))
        _owner = users_collection.find_one({'_id': c.get('owner_id') if not isinstance(c.get('owner_id'), str) else ObjectId(c['owner_id'])}, {'username': 1}) if c.get('owner_id') else None
        c['owner_username'] = _owner.get('username', '') if _owner else ''

    # Roadmaps
    roadmaps = list(roadmaps_collection.find().sort('created_at', -1))
    for r in roadmaps:
        r['_id'] = str(r['_id'])
        r['user_id'] = str(r.get('user_id', ''))
        # Get user name
        user = users_collection.find_one({'_id': ObjectId(r['user_id'])}, {'name': 1, 'username': 1}) if r['user_id'] else None
        r['user_name'] = user.get('name', 'Unknown') if user else 'Unknown'
        r['username'] = user.get('username', '') if user else ''
        content = r.get('roadmap_content', {})
        if isinstance(content, str):
            try:
                import json as _json
                content = _json.loads(content)
            except:
                content = {}
        stages = content.get('stages', []) if isinstance(content, dict) else []
        r['stage_count'] = len(stages)
        r['completed_stages'] = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))

    # Comments — fetch all, enrich with project title and user info
    raw_comments = list(comments_collection.find().sort('created_at', -1))
    # Build project_id -> title map for all referenced projects
    comment_project_ids = list({c.get('project_id') for c in raw_comments if c.get('project_id')})
    proj_map = {str(p['_id']): p.get('title', '—') for p in projects_collection.find({'_id': {'$in': comment_project_ids}}, {'title': 1})}

    all_comments = []
    for c in raw_comments:
        c['_id'] = str(c['_id'])
        c['project_id'] = str(c.get('project_id', ''))
        c['user_id'] = str(c.get('user_id', ''))
        c['parent_id'] = str(c.get('parent_id', '')) if c.get('parent_id') else None
        c['like_count'] = len(c.get('likes', []))
        c['project_title'] = proj_map.get(c['project_id'], '—')
        dt = c.get('created_at')
        c['created_at_str'] = dt.strftime('%b %d, %Y') if dt and hasattr(dt, 'strftime') else '—'
        all_comments.append(c)

    total_comments = comments_collection.count_documents({})

    # Stats
    all_projects_stats = list(projects_collection.find({}, {'views': 1, 'likes': 1}))
    stats = {
        'total_users': users_collection.count_documents({}),
        'total_projects': projects_collection.count_documents({}),
        'total_roadmaps': roadmaps_collection.count_documents({}),
        'total_communities': communities_collection.count_documents({}),
        'pending_approvals': commits_collection.count_documents({'xp_status': 'pending'}),
        'banned_users': users_collection.count_documents({'is_banned': True}),
        'total_xp_awarded': sum(u.get('xp', 0) for u in users_collection.find({}, {'xp': 1})),
        'total_views': sum(p.get('views', 0) for p in all_projects_stats),
        'total_likes': sum(len(p.get('likes', [])) for p in all_projects_stats),
        'total_comments': total_comments,
    }

    return render_template('admin.html',
        users=users,
        projects=projects,
        pending_commits=pending_commits,
        communities=communities,
        roadmaps=roadmaps,
        all_comments=all_comments,
        stats=stats
    )


@app.route('/admin/approve_commit/<commit_id>', methods=['POST'])
@login_required
@admin_required
def admin_approve_commit(commit_id):
    """Admin approves a project version upload — user gets +50 XP"""
    commit = commits_collection.find_one({'_id': ObjectId(commit_id)})
    if not commit:
        return jsonify({'success': False, 'error': 'Commit not found'}), 404
    if commit.get('xp_status') == 'approved':
        return jsonify({'success': False, 'error': 'Already approved'}), 400
    commits_collection.update_one({'_id': ObjectId(commit_id)}, {'$set': {'xp_status': 'approved'}})
    add_xp(str(commit['user_id']), 50, "Project version approved by admin")
    create_notification(
        str(commit['user_id']), 'approved',
        f'Your project upload "{commit.get("message", "version")}" was approved! +50 XP 🎉',
        url_for('view_project', project_id=str(commit['project_id']))
    )
    return jsonify({'success': True})


@app.route('/admin/reject_commit/<commit_id>', methods=['POST'])
@login_required
@admin_required
def admin_reject_commit(commit_id):
    """Admin rejects a project version upload — no XP awarded"""
    commit = commits_collection.find_one({'_id': ObjectId(commit_id)})
    if not commit:
        return jsonify({'success': False, 'error': 'Commit not found'}), 404
    commits_collection.update_one({'_id': ObjectId(commit_id)}, {'$set': {'xp_status': 'rejected'}})
    create_notification(
        str(commit['user_id']), 'declined',
        f'Your project upload "{commit.get("message", "version")}" was rejected. No XP awarded.',
        url_for('view_project', project_id=str(commit['project_id']))
    )
    return jsonify({'success': True})


@app.route('/admin/adjust_xp/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_adjust_xp(user_id):
    """Admin manually adjusts a user's XP"""
    data = request.get_json()
    points = int(data.get('points', 0))
    reason = data.get('reason', 'Admin adjustment').strip() or 'Admin adjustment'
    if points == 0:
        return jsonify({'success': False, 'error': 'Points cannot be 0'}), 400
    if points > 0:
        add_xp(user_id, points, f"Admin: {reason}")
    else:
        deduct_xp(user_id, abs(points), f"Admin: {reason}")
    action = f"+{points}" if points > 0 else str(points)
    create_notification(
        user_id,
        'approved' if points > 0 else 'declined',
        f'An admin adjusted your XP: {action} XP. Reason: {reason}',
        url_for('settings')
    )
    user = users_collection.find_one({'_id': ObjectId(user_id)}, {'xp': 1, 'badge': 1})
    return jsonify({'success': True, 'new_xp': user.get('xp', 0), 'badge': user.get('badge', '')})


@app.route('/admin/ban_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_ban_user(user_id):
    users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': {'is_banned': True}})
    create_notification(
        user_id, 'declined',
        'Your account has been suspended by an admin. Contact support if you think this is a mistake.',
        '#'
    )
    return jsonify({'success': True})


@app.route('/admin/unban_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_unban_user(user_id):
    """Unban a user"""
    users_collection.update_one({'_id': ObjectId(user_id)}, {'$unset': {'is_banned': '$set'}})
    return jsonify({'success': True})


@app.route('/admin/delete_user/<user_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    """Admin deletes a user and all their data"""
    uid = ObjectId(user_id)
    projects_collection.delete_many({'created_by_id': uid})
    roadmaps_collection.delete_many({'user_id': uid})
    commits_collection.delete_many({'user_id': uid})
    chat_history_collection.delete_one({'user_id': uid})
    notifications_collection.delete_many({'user_id': uid})
    xp_history_collection.delete_many({'user_id': uid})
    comments_collection.delete_many({'user_id': uid})  # delete all comments & replies
    users_collection.delete_one({'_id': uid})
    return jsonify({'success': True})


@app.route('/admin/delete_project/<project_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_project(project_id):
    project = projects_collection.find_one({'_id': ObjectId(project_id)})
    if project:
        create_notification(
            str(project['created_by_id']), 'declined',
            f'Your project "{project.get("title", "Untitled")}" was removed by an admin.',
            url_for('my_projects')
        )
    projects_collection.delete_one({'_id': ObjectId(project_id)})
    commits_collection.delete_many({'project_id': ObjectId(project_id)})
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
            result.append({
                '_id': str(c['_id']),
                'user_name': c.get('user_name', ''),
                'username': c.get('username', ''),
                'profile_pic': c.get('profile_pic', 'default.jpg'),
                'content': c.get('content', ''),
                'parent_id': str(c['parent_id']) if c.get('parent_id') else None,
                'like_count': len(c.get('likes', [])),
                'is_deleted': c.get('is_deleted', False),
                'created_at_str': dt.strftime('%b %d, %Y at %I:%M %p') if dt and hasattr(dt, 'strftime') else '—',
            })
        return jsonify({'success': True, 'comments': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/user/<user_id>')
@login_required
@admin_required
def admin_user_detail(user_id):
    """Full detail view for a single user"""
    uid = ObjectId(user_id)
    user = users_collection.find_one({'_id': uid})
    if not user:
        flash("User not found.", "error")
        return redirect(url_for('admin_panel'))

    # Profile pic
    pic = user.get('profile_pic', 'default.jpg')
    user['profile_pic_url'] = f"/static/profile_pics/{pic}"
    user['_id'] = str(user['_id'])

    # XP info
    xp = user.get('xp', 0)
    level_info = get_level_info(xp)
    progress_pct = int((xp / level_info['next_xp']) * 100) if level_info['next_xp'] != 'Max' else 100
    user['xp'] = xp
    user['level_info'] = level_info
    user['progress_pct'] = progress_pct
    user['streak_count'] = user.get('streak_count', 0)
    user['member_since'] = to_ist(user.get('created_at')).strftime('%B %Y')

    # Projects
    projects = list(projects_collection.find({'created_by_id': uid}).sort('created_at', -1))
    for p in projects:
        p['_id'] = str(p['_id'])

    # Roadmaps
    roadmaps = list(roadmaps_collection.find({'user_id': uid}).sort('created_at', -1))
    for r in roadmaps:
        r['_id'] = str(r['_id'])
        content = r.get('roadmap_content', {})
        if isinstance(content, str):
            try:
                import json as _json
                content = _json.loads(content)
            except:
                content = {}
        stages = content.get('stages', []) if isinstance(content, dict) else []
        r['stage_count'] = len(stages)
        r['completed_stages'] = sum(1 for s in stages if isinstance(s, dict) and s.get('completed'))

    # Certificates
    certificates = user.get('certificates', [])

    # Work Experience
    work_experience = user.get('work_experience', [])

    # Communities
    user_communities = list(communities_collection.find({'members': uid}))
    for c in user_communities:
        c['_id'] = str(c['_id'])
        c['is_owner'] = c.get('owner_id') == uid
        c['member_count'] = len(c.get('members', []))

    # XP History
    xp_history = list(xp_history_collection.find(
        {'user_id': uid}
    ).sort('timestamp', -1).limit(50))

    return render_template('admin_user.html',
        user=user,
        projects=projects,
        roadmaps=roadmaps,
        certificates=certificates,
        communities=user_communities,
        xp_history=xp_history,
        work_experience=work_experience
    )

    
# --- Main Execution ---
if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'profile_pics'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_img'), exist_ok=True)
    os.makedirs(os.path.join(app.root_path, 'static', 'portfolio_assets'), exist_ok=True)
    print(f"Ensured upload folder exists at: {app.config['UPLOAD_FOLDER']}")
    try:
        app.run(debug=False)
    finally:
        scheduler.shutdown()
        print("Scheduler shut down cleanly.")