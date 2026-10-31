from datetime import datetime, timezone

from bson.objectid import ObjectId

from extensions import notifications_collection

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

