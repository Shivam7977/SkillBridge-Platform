from bson.objectid import ObjectId
from flask_login import UserMixin

from extensions import login_manager, users_collection


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
        # Banned or pending-deletion accounts are rejected here too — this runs on
        # EVERY request, so it kills an already-open session immediately, not just
        # future login attempts.
        if user_data and not user_data.get('is_banned') and not user_data.get('pending_deletion'):
            return User(user_data)
    except Exception as e:
        print(f"Error loading user {user_id}: {e}")
    return None
