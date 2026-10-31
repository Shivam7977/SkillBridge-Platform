import re
from flask import request
from flask_login import current_user
from flask_limiter.util import get_remote_address
import bleach


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


def safe_experience_years(value, default=0, max_years=50):
    """Always returns an int in 0..max_years. Handles None, '', '-1', '1e1', '5.5',
    floats, bools, huge numbers and garbage strings without raising."""
    try:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        number = float(value)
        if number != number or number in (float('inf'), float('-inf')):
            return default
        years = int(number)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(years, 0), max_years)


def clean_cert_payload(data):
    """Validates + sanitizes certificate fields. Returns (clean_dict, error_message)."""
    if not isinstance(data, dict):
        return None, 'Certificate details are required.'
    def txt(key, limit):
        return sanitize(str(data.get(key) or ''))[:limit]
    clean = {
        'name': txt('name', 120), 'issuer': txt('issuer', 120),
        'issue_date': txt('issue_date', 10), 'expiry_date': txt('expiry_date', 10),
        'credential_id': txt('credential_id', 100), 'cert_url': str(data.get('cert_url') or '').strip()[:500],
    }
    if not clean['name'] or not clean['issuer']:
        return None, 'Name and issuer are required'
    import re as _re
    from datetime import datetime as _dt
    parsed = {}
    for key in ('issue_date', 'expiry_date'):
        if clean[key]:
            if not _re.fullmatch(r'\d{4}-\d{2}-\d{2}', clean[key]):
                return None, 'Dates must be valid (YYYY-MM-DD).'
            try:
                parsed[key] = _dt.strptime(clean[key], '%Y-%m-%d')
            except ValueError:
                return None, 'Dates must be valid (YYYY-MM-DD).'
            if parsed[key].year < 1980 or parsed[key].year > _dt.now().year + 50:
                return None, 'Certificate year looks invalid.'
    if 'issue_date' in parsed and parsed['issue_date'] > _dt.now():
        return None, 'Issue date cannot be in the future.'
    if 'issue_date' in parsed and 'expiry_date' in parsed and parsed['expiry_date'] < parsed['issue_date']:
        return None, 'Expiry date cannot be before the issue date.'
    if clean['cert_url'] and not _re.match(r'^https?://', clean['cert_url'], _re.I):
        return None, 'Certificate link must start with http:// or https://'
    return clean, None


SOCIAL_HOSTS = {
    'linkedin': ('linkedin.com',), 'github': ('github.com',),
    'instagram': ('instagram.com',), 'facebook': ('facebook.com', 'fb.com'),
}
SOCIAL_LABELS = {'linkedin': 'LinkedIn', 'github': 'GitHub', 'instagram': 'Instagram', 'facebook': 'Facebook'}
SOCIAL_EXAMPLES = {
    'linkedin': 'https://www.linkedin.com/in/yourname', 'github': 'https://github.com/yourname',
    'instagram': 'https://www.instagram.com/yourname', 'facebook': 'https://www.facebook.com/yourname',
}


def clean_social_url(value, platform):
    """Returns (clean_url, error). Empty is allowed. Only real http(s) links on the platform's own domain pass,
    so javascript:/data: links (stored XSS) and look-alike sites are rejected."""
    from urllib.parse import urlparse
    label, example = SOCIAL_LABELS[platform], SOCIAL_EXAMPLES[platform]
    raw = str(value or '').strip()
    if not raw:
        return '', None
    if len(raw) > 300:
        return None, f'{label} link is too long (max 300 characters, yours is {len(raw)}).'
    if re.search(r'\s', raw):
        return None, f'{label} link cannot contain spaces. Example: {example}'
    if not re.match(r'^[a-z][a-z0-9+.\-]*:', raw, re.I):       # no scheme typed -> assume https
        raw = 'https://' + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or '').lower()
    except ValueError:
        return None, f'{label} link is not a valid URL. Example: {example}'
    if parsed.scheme.lower() not in ('http', 'https'):
        return None, f'{label} link must start with http:// or https:// (you entered "{raw[:30]}").'
    if not any(host == d or host.endswith('.' + d) for d in SOCIAL_HOSTS[platform]):
        return None, f'{label} link must be a {SOCIAL_HOSTS[platform][0]} address. Example: {example}'
    if parsed.username or parsed.password:
        return None, f'{label} link cannot contain a username or password.'
    return raw, None