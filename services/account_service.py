from bson.objectid import ObjectId

from extensions import (users_collection, projects_collection, comments_collection, communities_collection,
                        deleted_accounts_collection, community_messages_collection, roadmaps_collection,
                        commits_collection, chat_history_collection, notifications_collection, xp_history_collection)
from services.time_utils import now_utc

def is_sole_active_admin(user_id):
    """True if user_id is an admin and no OTHER active (non-banned, non-trashed) admin exists."""
    user_doc = users_collection.find_one({'_id': user_id}, {'is_admin': 1})
    if not user_doc or not user_doc.get('is_admin'):
        return False
    other_admins = users_collection.count_documents({
        'is_admin': True, '_id': {'$ne': user_id},
        'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}
    })
    return other_admins == 0



def sync_hidden_flag(user_id):
    """hidden = True if the user is banned OR pending deletion, else False.
    Cascades onto their own content (projects, comments). For owned communities:
    if another active (non-banned, non-trashed) member exists, ownership transfers
    to them so the community stays alive for everyone else — only hidden as a last
    resort when no other active member exists. A transferred community is never
    clawed back on unban/restore; only communities they STILL own get un-hidden."""
    user = users_collection.find_one({'_id': user_id}, {'is_banned': 1, 'pending_deletion': 1})
    if not user:
        return
    should_hide = bool(user.get('is_banned')) or bool(user.get('pending_deletion'))
    projects_collection.update_many({'created_by_id': user_id}, {'$set': {'hidden': should_hide}})
    comments_collection.update_many({'user_id': user_id}, {'$set': {'hidden': should_hide}})

    if should_hide:
        owned = list(communities_collection.find({'owner_id': user_id}))
        for c in owned:
            admin_ids = [m for m in c.get('admins', []) if m != user_id]
            member_ids = [m for m in c.get('members', []) if m != user_id and m not in admin_ids]
            # Co-admins get first refusal on ownership, then regular members
            new_owner_id, new_owner = None, None
            for cand_id in admin_ids + member_ids:
                cand = users_collection.find_one(
                    {'_id': cand_id, 'is_banned': {'$ne': True}, 'pending_deletion': {'$ne': True}},
                    {'name': 1}
                )
                if cand:
                    new_owner_id, new_owner = cand_id, cand
                    break
            if new_owner:
                communities_collection.update_one(
                    {'_id': c['_id']},
                    {'$set': {'owner_id': new_owner_id, 'owner_name': new_owner.get('name', 'Unknown'), 'hidden': False},
                     '$pull': {'admins': new_owner_id}}
                )
            else:
                communities_collection.update_one({'_id': c['_id']}, {'$set': {'hidden': True}})
    else:
        # Unban/restore — only un-hide communities they still currently own; a
        # community already transferred to someone else stays with its new owner.
        communities_collection.update_many({'owner_id': user_id}, {'$set': {'hidden': False}})



def log_account_deletion(user_doc, deleted_by):
    """Audit record kept AFTER the user document is hard-deleted.
    deleted_by: 'Self' (user deleted their own account) or the admin's name."""
    try:
        deleted_accounts_collection.insert_one({
            'name': user_doc.get('name', 'Unknown'),
            'email': user_doc.get('email', ''),
            'username': user_doc.get('username', ''),
            'was_banned': user_doc.get('is_banned', False),
            'deleted_by': deleted_by,
            'deleted_at': now_utc(),
        })
    except Exception as e:
        print(f"Deletion log error: {e}")


def purge_expired_accounts():
    """Runs daily. Permanently deletes accounts whose 30-day trash period has expired —
    only place a permanent, irreversible delete happens. Reuses the full cascade-cleanup
    (orphaned likes/bookmarks/comments/community-membership) worked out earlier."""
    try:
        now = now_utc()
        expired = list(users_collection.find({'pending_deletion': True, 'purge_at': {'$lte': now}}))
        for user_doc in expired:
            uid = user_doc['_id']
            log_account_deletion(user_doc, deleted_by=user_doc.get('deletion_requested_by', 'Self'))

            own_project_ids = [p['_id'] for p in projects_collection.find({'created_by_id': uid}, {'_id': 1})]
            if own_project_ids:
                users_collection.update_many({}, {'$pull': {'bookmarks': {'$in': own_project_ids}}})
                comments_collection.delete_many({'project_id': {'$in': own_project_ids}})
            projects_collection.update_many({'likes': uid}, {'$pull': {'likes': uid}})

            owned_community_ids = [c['_id'] for c in communities_collection.find({'owner_id': uid}, {'_id': 1})]
            if owned_community_ids:
                community_messages_collection.delete_many({'community_id': {'$in': owned_community_ids}})
                communities_collection.delete_many({'_id': {'$in': owned_community_ids}})

            communities_collection.update_many(
                {'owner_id': {'$ne': uid}},
                {'$pull': {'members': uid, 'admins': uid, 'pending_requests': uid}}
            )

            projects_collection.delete_many({'created_by_id': uid})
            roadmaps_collection.delete_many({'user_id': uid})
            commits_collection.delete_many({'user_id': uid})
            chat_history_collection.delete_one({'user_id': uid})
            notifications_collection.delete_many({'user_id': uid})
            xp_history_collection.delete_many({'user_id': uid})
            comments_collection.delete_many({'user_id': uid})
            users_collection.delete_one({'_id': uid})
    except Exception as e:
        print(f"Purge expired accounts error: {e}")


