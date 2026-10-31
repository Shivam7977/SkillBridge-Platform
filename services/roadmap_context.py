"""Builds the 'learner context' that is shared with the AI when generating a personalised roadmap.

PRIVACY RULES (enforced here, in one place):
  * Only the fields listed in USER_FIELDS are ever read from the user document.
  * Name, email, location, social links, ids and photos are NEVER read or sent.
  * Existing roadmaps are NEVER read or sent.
  * Links and e-mail addresses inside free text are stripped.
  * The text is hard-capped, and the caller wraps it as untrusted DATA in the prompt.
"""
import re

from bson.objectid import ObjectId

from services.utils import parse_skills, safe_experience_years

# The ONLY user fields this module is allowed to read.
USER_FIELDS = {
    'current_status': 1, 'experience_years': 1, 'career_goal': 1, 'known_skills': 1, 'learning_skills': 1,
    'education': 1, 'work_experience': 1, 'certificates': 1, 'github_langs': 1, 'github_repos': 1,
}
MAX_TOTAL_CHARS = 3500


def _clean(value, limit):
    """Single-line, link-free, delimiter-safe text capped at `limit` characters."""
    text = str(value if value is not None else '')
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text)                 # control chars / newlines
    text = re.sub(r'https?://\S+|www\.\S+', '', text)             # links
    text = re.sub(r'\S+@\S+\.\S+', '', text)                      # e-mail addresses
    text = text.replace('```', ' ').replace('<', '(').replace('>', ')')   # can't break our <learner_data> tags
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def _skills(value, max_items=25):
    seen, out = set(), []
    for s in parse_skills(value):
        s = _clean(s, 40)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _profile_section(user):
    lines = []
    if user.get('current_status'):
        lines.append(f"Current status: {_clean(user['current_status'], 40)}")
    years = safe_experience_years(user.get('experience_years'))
    lines.append(f"Years of professional experience: {years}")
    if user.get('career_goal'):
        lines.append(f"Career goal: {_clean(user['career_goal'], 120)}")
    known = _skills(user.get('known_skills'))
    if known:
        lines.append("Skills they already know: " + ', '.join(known))
    learning = _skills(user.get('learning_skills'))
    if learning:
        lines.append("Skills they are currently learning: " + ', '.join(learning))
    for edu in (user.get('education') or [])[:3]:
        if not isinstance(edu, dict):
            continue
        degree, field = _clean(edu.get('degree'), 60), _clean(edu.get('field'), 60)
        if degree or field:
            end = 'present' if edu.get('is_current') else _clean(edu.get('to_year'), 4)
            lines.append(f"Education: {degree} {('in ' + field) if field else ''} (ends {end or 'n/a'})".replace('  ', ' '))
    # a profile that only has the default "0 years" line has nothing real in it
    meaningful = len(lines) > 1
    return lines if meaningful else []


def _resume_section(user):
    lines = []
    for job in (user.get('work_experience') or [])[:4]:
        if not isinstance(job, dict):
            continue
        role = _clean(job.get('role'), 60)
        if not role:
            continue          # company names are intentionally left out
        span = f"{_clean(job.get('from_year'), 4)}-{'present' if job.get('is_current') else _clean(job.get('to_year'), 4)}"
        bullets = [_clean(b, 150) for b in (job.get('bullets') or [])[:3] if isinstance(b, str) and b.strip()]
        lines.append(f"Work: {role} ({span})" + (": " + "; ".join(bullets) if bullets else ''))
    certs = []
    for cert in (user.get('certificates') or [])[:8]:
        if isinstance(cert, dict) and cert.get('name'):
            issuer = _clean(cert.get('issuer'), 40)
            certs.append(_clean(cert['name'], 70) + (f" ({issuer})" if issuer else ''))
    if certs:
        lines.append("Certificates: " + '; '.join(certs))
    langs = _skills(user.get('github_langs'), 12)
    if langs:
        lines.append("GitHub languages: " + ', '.join(langs))
    repos = [r for r in (user.get('github_repos') or []) if isinstance(r, dict) and r.get('name')]
    repos.sort(key=lambda r: r.get('stars') if isinstance(r.get('stars'), int) else 0, reverse=True)
    for repo in repos[:6]:
        desc = _clean(repo.get('description'), 100)
        if desc.lower() == 'no description':
            desc = ''
        lines.append(f"GitHub repo: {_clean(repo['name'], 50)} [{_clean(repo.get('language'), 20) or 'n/a'}]" + (f" - {desc}" if desc else ''))
    return lines


def _projects_section(projects_collection, uid):
    lines = []
    cursor = projects_collection.find(
        {'created_by_id': uid},
        {'title': 1, 'description': 1, 'skills_needed': 1, 'is_completed': 1}
    ).sort('created_at', -1).limit(8)
    for project in cursor:
        title = _clean(project.get('title'), 70)
        if not title:
            continue
        state = 'completed' if project.get('is_completed') else 'in progress'
        skills = ', '.join(_skills(project.get('skills_needed'), 10))
        desc = _clean(project.get('description'), 200)
        lines.append(f"Project: {title} [{state}]" + (f" | skills: {skills}" if skills else '') + (f" | {desc}" if desc else ''))
    return lines


def build_learner_context(users_collection, projects_collection, user_id,
                          use_profile=True, use_resume=True, use_projects=True):
    """Returns (context_text, used) where `used` describes what was shared, for the UI.
    context_text is '' when there is nothing useful to share."""
    uid = ObjectId(user_id)
    user = users_collection.find_one({'_id': uid}, USER_FIELDS) or {}
    sections, used = [], {'profile': False, 'resume': False, 'projects': 0}

    if use_profile:
        lines = _profile_section(user)
        if lines:
            sections.append("PROFILE:\n" + '\n'.join(lines))
            used['profile'] = True
    if use_resume:
        lines = _resume_section(user)
        if lines:
            sections.append("RESUME DETAILS:\n" + '\n'.join(lines))
            used['resume'] = True
    if use_projects:
        lines = _projects_section(projects_collection, uid)
        if lines:
            sections.append("THEIR EXISTING PROJECTS:\n" + '\n'.join(lines))
            used['projects'] = len(lines)

    text = '\n\n'.join(sections)
    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS].rsplit('\n', 1)[0]
    return text, used