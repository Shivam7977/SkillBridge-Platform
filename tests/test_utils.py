"""Pure helpers: experience years, certificate details, social links."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import utils


class ExperienceYears(unittest.TestCase):
    def test_always_returns_an_int_between_0_and_50(self):
        expected = {None: 0, '': 0, '-1': 0, -1: 0, '1e1': 10, '5.5': 5, 5.9: 5, 'abc': 0, '99': 50, '51': 50, 10 ** 30: 50,
                    float('nan'): 0, float('inf'): 0, True: 0, ' 7 ': 7, '50': 50, '0': 0, 3: 3}
        for value, years in expected.items():
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(utils.safe_experience_years(value), years)


class CertificatePayload(unittest.TestCase):
    base = {'name': 'AWS Cloud Practitioner', 'issuer': 'Amazon'}

    def check(self, **extra):
        return utils.clean_cert_payload(dict(self.base, **extra))

    def test_minimum_valid_certificate(self):
        clean, error = self.check()
        self.assertIsNone(error)
        self.assertEqual(clean['name'], 'AWS Cloud Practitioner')

    def test_rejections(self):
        cases = {'bad date': dict(issue_date='2024-02-30'), 'expiry before issue': dict(issue_date='2024-05-01', expiry_date='2023-01-01'),
                 'future issue date': dict(issue_date='2999-01-01'), 'javascript link': dict(cert_url='javascript:alert(1)'),
                 'year 1970': dict(issue_date='1970-01-01'), 'name missing': dict(name=' ')}
        for name, extra in cases.items():
            with self.subTest(case=name):
                clean, error = self.check(**extra)
                self.assertIsNone(clean)
                self.assertTrue(error)

    def test_not_a_dict(self):
        self.assertEqual(utils.clean_cert_payload(None)[0], None)

    def test_valid_link_and_dates(self):
        clean, error = self.check(issue_date='2024-05-01', expiry_date='2026-05-01', cert_url='https://example.com/c')
        self.assertIsNone(error)
        self.assertEqual(clean['cert_url'], 'https://example.com/c')


class SocialLinks(unittest.TestCase):
    def test_accepted(self):
        cases = [('linkedin', 'https://www.linkedin.com/in/x', 'https://www.linkedin.com/in/x'),
                 ('linkedin', 'linkedin.com/in/x', 'https://linkedin.com/in/x'),            # https:// is added
                 ('github', 'https://github.com/Shivam7977', 'https://github.com/Shivam7977'),
                 ('facebook', 'https://fb.com/me', 'https://fb.com/me'),
                 ('instagram', 'HTTPS://WWW.INSTAGRAM.COM/me', 'HTTPS://WWW.INSTAGRAM.COM/me'),
                 ('github', '', ''), ('github', '   ', ''), ('github', None, '')]
        for platform, value, expected in cases:
            with self.subTest(platform=platform, value=value):
                self.assertEqual(utils.clean_social_url(value, platform), (expected, None))

    def test_rejected(self):
        cases = [('github', 'javascript:alert(1)'), ('github', 'data:text/html,<script>1</script>'), ('github', 'https://github.com.evil.com/x'),
                 ('github', 'https://evilgithub.com/x'), ('linkedin', 'https://github.com/x'), ('instagram', 'https://user:pw@instagram.com/x'),
                 ('github', 'https://github.com/a b'), ('github', 'https://github.com/' + 'x' * 300), ('github', 'ftp://github.com/x')]
        for platform, value in cases:
            with self.subTest(platform=platform, value=value[:40]):
                clean, error = utils.clean_social_url(value, platform)
                self.assertIsNone(clean)
                self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()
