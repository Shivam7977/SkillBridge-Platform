import os

import resend

from extensions import s


def app_base_url():
    """Base URL for links generated outside a request context, such as scheduler emails."""
    return (
        os.getenv("APP_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or "https://skillbridge-ai.tech"
    ).rstrip("/")


def make_unsubscribe_token(user_id):
    return s.dumps({'user_id': str(user_id)}, salt='email-reminders-unsubscribe')


def unsubscribe_url_for(user_id):
    return f"{app_base_url()}/unsubscribe/{make_unsubscribe_token(user_id)}"


def send_email(to_email, subject, body):
    """Send email via Resend API (works on Render — uses HTTPS not SMTP)."""
    try:
        resend.Emails.send({
            "from": "SkillBridge <otp@skillbridge-ai.tech>",
            "to": [to_email],
            "subject": subject,
            "text": body,
        })
        return True
    except Exception as e:
        print(f"❌ Resend email failed: {e}")
        return False


def send_reminder_digest_email(user_email, user_name, items, user_id=None):
    """Send one roadmap inactivity email for all stale roadmaps in this run."""
    if len(items) == 1:
        rm, _, days = items[0]
        goal = rm.get('goal', 'roadmap')
        subject = f"Your '{goal}' roadmap is waiting for you!"
        body = (
            f"Hi {user_name},\n\n"
            f"You haven't made progress on your '{goal}' roadmap in {days} days. "
            f"Pick up where you left off and keep earning XP!\n\n"
            f"- SkillBridge"
        )
    else:
        lines = "\n".join(f"- {rm.get('goal', 'roadmap')} ({days} days inactive)" for rm, _, days in items)
        subject = f"{len(items)} of your roadmaps need attention"
        body = (
            f"Hi {user_name},\n\n"
            f"The following roadmaps haven't seen progress in a while:\n\n"
            f"{lines}\n\n"
            f"Jump back in and keep your learning moving forward.\n\n"
            f"- SkillBridge"
        )
    if user_id:
        body += (
            "\n\nYou are receiving this because roadmap email reminders are enabled. "
            f"Unsubscribe: {unsubscribe_url_for(user_id)}"
        )
    return send_email(user_email, subject, body)
