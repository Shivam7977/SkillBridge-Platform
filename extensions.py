import os
from datetime import datetime, timezone

import pytz
from bson.objectid import ObjectId
from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer
from pymongo import MongoClient
from dotenv import load_dotenv
import resend
from apscheduler.schedulers.background import BackgroundScheduler

from ai_roadmap_generator import configure_ai

load_dotenv(override=True)
try:
    if not os.getenv('GROQ_API_KEY_1'):
     print("WARNING: GROQ_API_KEY_1 not found in .env file. AI features will likely fail.")
    if not os.getenv('GROQ_API_KEY_2'):
     print("NOTE: GROQ_API_KEY_2 not set — running on a single key with no automatic fallback if it hits a rate limit.")
    configure_ai()
    print("AI configured successfully.")
except ValueError as e:
    print(f"AI Configuration Error: {e}")
except Exception as e_ai_config:
     print(f"An unexpected error occurred during AI configuration: {e_ai_config}")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'project_uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
bcrypt = Bcrypt(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24))
s = URLSafeTimedSerializer(app.secret_key)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

resend.api_key = os.getenv("RESEND_API_KEY")

try:
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
    interview_sessions_collection = db["interview_sessions"]
    deleted_accounts_collection = db["deleted_accounts"]
    comments_collection.create_index([("project_id", 1), ("created_at", -1)])
    comments_collection.create_index([("parent_id", 1)])
    interview_sessions_collection.create_index([("user_id", 1), ("created_at", -1)])
except Exception as e:
    print(f"ERROR: Could not connect to MongoDB. Please ensure it's running. Details: {e}")
    exit()

UTC_TZ = pytz.utc
DEFAULT_USER_TZ = 'UTC'
scheduler = BackgroundScheduler(timezone=UTC_TZ)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"
