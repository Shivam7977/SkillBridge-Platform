"""Profile page rules: experience, education (marks, years), work experience, social links, and the page view itself.

Every message must name the field AND the allowed range, so these tests check the wording as well as the status.
"""
import json
import sys
import unittest
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
from support import ObjectId, utils, xp

FUNCS = ['_bad', '_text_check', '_year_check', 'EXPERIENCE_YEARS_RANGE', 'EDU_MAX_ENTRIES', 'WORK_MAX_ENTRIES',
         'EDU_GRAD_YEARS_AHEAD', 'MIN_CALENDAR_YEAR', 'WORK_MAX_BULLETS', 'WORK_BULLET_MAX_LEN',
         'profile_save_career', 'profile_save_education', 'profile_save_experience', 'profile_save_social']
CY = datetime.now().year



class ProfileRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.request = support.FakeRequest()
        cls.user_id, _ = None, None
        ns = support.route_namespace(
            request=cls.request, json=json, Decimal=Decimal, InvalidOperation=InvalidOperation, datetime=datetime,
            users_collection=MagicMock(), current_user=type('U', (), {'id': '0' * 24}),
            sanitize=lambda s: str(s), clean_social_url=utils.clean_social_url,
            check_and_award_profile_complete=lambda uid: False)
        cls.ns = support.load_functions('routes/profile.py', FUNCS, ns)

    def call(self, name, form):
        self.request.form = form
        result = self.ns[name]()
        body, code = result if isinstance(result, tuple) else (result, 200)
        return code, body.get('error', 'OK')

    # ------------------------------------------------------------------ experience (years)
    def test_experience_accepts_the_whole_range(self):
        for value in ('0', '3', '50', ' 7 ', ''):
            with self.subTest(value=value):
                self.assertEqual(self.call('profile_save_career', {'current_status': 'Fresher', 'experience_years': value})[0], 200)

    def test_experience_rejections_say_the_range(self):
        cases = {'51': 'between 0 and 50', '-1': 'between 0 and 50', '5.5': 'whole number', 'abc': 'between 0 and 50',
                 '1e1': 'between 0 and 50', '9' * 30: 'between 0 and 50'}
        for value, fragment in cases.items():
            with self.subTest(value=value[:12]):
                code, message = self.call('profile_save_career', {'current_status': 'x', 'experience_years': value})
                self.assertEqual(code, 400)
                self.assertIn(fragment, message)

    # ------------------------------------------------------------------ education (marks + years)
    @staticmethod
    def edu(**overrides):
        entry = dict(institution='VIVA Institute', degree='B.E.', field='CSE (AI/ML)', grade_type='CGPA', grade_value='8.82',
                     from_year='2023', to_year='2027', is_current=False)
        entry.update(overrides)
        return entry

    def save_edu(self, *entries):
        return self.call('profile_save_education', {'education_json': json.dumps(list(entries))})

    def test_valid_education_including_expected_graduation_year(self):
        self.assertEqual(self.save_edu(self.edu())[0], 200)
        self.assertEqual(self.save_edu(self.edu(is_current=True, to_year=''))[0], 200)
        self.assertEqual(self.save_edu()[0], 200)                                      # empty card is allowed

    def test_grade_limits(self):
        accepted = [('CGPA', '0'), ('CGPA', '10'), ('CGPA', '8.77'), ('Percentage', '100'), ('Percentage', '85.25')]
        rejected = {('CGPA', '10.01'): 'between 0 and 10', ('CGPA', '11'): 'between 0 and 10', ('CGPA', '-1'): 'cannot be negative',
                    ('CGPA', '8.777'): '2 decimal', ('CGPA', 'abc'): 'must be a number', ('CGPA', ''): 'required',
                    ('Percentage', '100.5'): 'between 0 and 100', ('Percentage', '101'): 'between 0 and 100'}
        for grade_type, value in accepted:
            with self.subTest(accepted=(grade_type, value)):
                self.assertEqual(self.save_edu(self.edu(grade_type=grade_type, grade_value=value))[0], 200)
        for (grade_type, value), fragment in rejected.items():
            with self.subTest(rejected=(grade_type, value)):
                code, message = self.save_edu(self.edu(grade_type=grade_type, grade_value=value))
                self.assertEqual(code, 400)
                self.assertIn(fragment, message)

    def test_education_year_limits(self):
        rejected = {'from 1979': self.edu(from_year='1979'), 'from in the future': self.edu(from_year=str(CY + 1)),
                    'to before from': self.edu(to_year='2020'), 'to too far ahead': self.edu(to_year=str(CY + 9)),
                    'to is text': self.edu(to_year='soon')}
        for name, entry in rejected.items():
            with self.subTest(case=name):
                code, message = self.save_edu(entry)
                self.assertEqual(code, 400)
                self.assertRegex(message, r'(From|To) Year')
        self.assertEqual(self.save_edu(self.edu(from_year='1980'))[0], 200)
        self.assertEqual(self.save_edu(self.edu(to_year=str(CY + 8)))[0], 200)

    def test_education_text_limits_and_entry_count(self):
        self.assertIn('at least 2', self.save_edu(self.edu(institution='A'))[1])
        self.assertIn('at most 120', self.save_edu(self.edu(institution='x' * 121))[1])
        self.assertIn('Education #2', self.save_edu(self.edu(), self.edu(grade_value='99'))[1])
        self.assertIn('at most 10', self.save_edu(*[self.edu()] * 11)[1])
        self.assertEqual(self.call('profile_save_education', {'education_json': '{oops'})[0], 400)

    # ------------------------------------------------------------------ work experience
    @staticmethod
    def job(**overrides):
        entry = dict(company='Acme', role='ML Intern', from_year='2025', to_year=str(CY), is_current=False, bullets=['Built a model'])
        entry.update(overrides)
        return entry

    def save_jobs(self, *entries):
        return self.call('profile_save_experience', {'work_experience_json': json.dumps(list(entries))})

    def test_valid_work_experience(self):
        self.assertEqual(self.save_jobs(self.job())[0], 200)
        self.assertEqual(self.save_jobs(self.job(is_current=True, to_year=''))[0], 200)
        self.assertEqual(self.save_jobs()[0], 200)

    def test_work_experience_limits(self):
        cases = {'to in the future': (self.job(to_year=str(CY + 1)), 'To Year'), 'from 1979': (self.job(from_year='1979'), 'From Year'),
                 'role empty': (self.job(role=''), 'Role is required'),
                 '11 achievements': (self.job(bullets=['b'] * 11), 'at most 10'),
                 'achievement too long': (self.job(bullets=['x' * 301]), 'at most 300')}
        for name, (entry, fragment) in cases.items():
            with self.subTest(case=name):
                code, message = self.save_jobs(entry)
                self.assertEqual(code, 400)
                self.assertIn(fragment, message)
        self.assertIn('at most 15', self.save_jobs(*[self.job()] * 16)[1])

    # ------------------------------------------------------------------ social links
    def test_social_links_are_checked_and_cleaned(self):
        ok = {'linkedin_url': 'linkedin.com/in/shivam', 'github_url': 'https://github.com/Shivam7977', 'instagram_url': '', 'facebook_url': ''}
        self.assertEqual(self.call('profile_save_social', ok)[0], 200)
        for url in ('javascript:alert(1)', 'https://github.com.evil.com/x', 'ftp://github.com/x'):
            with self.subTest(url=url):
                self.assertEqual(self.call('profile_save_social', dict(ok, github_url=url))[0], 400)


class ProfilePageView(unittest.TestCase):
    """The page must render, with safe numbers, whatever junk is stored for the user."""

    @classmethod
    def setUpClass(cls):
        cls.rendered = {}
        cls.users = MagicMock()
        ns = support.route_namespace(
            users_collection=cls.users, current_user=type('U', (), {'id': str(ObjectId())}), url_for=lambda ep, **k: '/' + ep,
            redirect=lambda url: ('REDIRECT', url), logout_user=lambda: None, json=json,
            render_template=lambda template, **kw: cls.rendered.update(kw) or 'RENDERED',
            safe_experience_years=utils.safe_experience_years, get_level_info=xp.get_level_info)
        cls.ns = support.load_functions('routes/profile.py', ['_safe_int', 'profile'], ns)

    def setUp(self):
        self.rendered.clear()

    def view(self, **fields):
        self.users.find_one.return_value = dict(fields, _id=ObjectId())      # what "the database" returns for this user
        return self.ns['profile']()

    def test_normal_user(self):
        self.assertEqual(self.view(xp=1085, streak_count=4, experience_years='3', github_followers=8, github_public_repos=8), 'RENDERED')
        self.assertEqual((self.rendered['xp'], self.rendered['streak'], self.rendered['user']['experience_years']), (1085, 4, '3'))

    def test_garbage_stored_for_the_user(self):
        self.view(xp=None, streak_count='abc', experience_years='-1', github_followers=None, github_public_repos='x')
        self.assertEqual((self.rendered['xp'], self.rendered['streak'], self.rendered['user']['experience_years']), (0, 0, '0'))
        self.assertEqual((self.rendered['user']['github_followers'], self.rendered['user']['github_public_repos']), (0, 0))

    def test_progress_bar_stays_between_0_and_100(self):
        self.view(xp=99999, streak_count=-5)
        self.assertTrue(0 <= self.rendered['progress_pct'] <= 100)
        self.assertEqual(self.rendered['streak'], 0)

    def test_deleted_account_is_sent_to_login(self):
        self.users.find_one.return_value = None
        self.assertEqual(self.ns['profile'](), ('REDIRECT', '/login'))


if __name__ == '__main__':
    unittest.main()
