"""A file over the size limit must give a clear message, never a silent page refresh."""
import io
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, flash, get_flashed_messages, jsonify, redirect, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support


def build_app():
    app = Flask(__name__)
    app.secret_key = 'test'
    app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024                     # 1 MB is enough to test the behaviour

    @app.route('/')
    def main_page():
        return 'home'

    @app.route('/project/1/upload', methods=['GET', 'POST'])
    def upload_version():
        if request.method == 'POST':
            request.files.get('project_file')                            # reading the upload triggers the size check
            return 'uploaded'
        return ' | '.join(get_flashed_messages())

    @app.route('/profile/save/identity', methods=['POST'])
    def save_identity():
        request.files.get('profile_pic')
        return 'saved'

    namespace = dict(app=app, request=request, redirect=redirect, url_for=url_for, flash=flash, jsonify=jsonify, urlparse=urlparse)
    support.load_functions('routes/errors_hooks.py', ['handle_file_too_large'], namespace)
    return app


def upload(client, size, path='/project/1/upload', field='project_file', **kwargs):
    return client.post(path, data={field: (io.BytesIO(b'x' * size), 'file.zip')}, content_type='multipart/form-data', **kwargs)


class TooLargeUploads(unittest.TestCase):
    def setUp(self):
        self.client = build_app().test_client()

    def test_small_upload_is_fine(self):
        self.assertEqual(upload(self.client, 1000).get_data(as_text=True), 'uploaded')

    def test_big_upload_goes_back_to_the_same_page_with_a_message(self):
        response = upload(self.client, 2 * 1024 * 1024, headers={'Referer': 'http://localhost/project/1/upload'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], 'http://localhost/project/1/upload')
        message = self.client.get('/project/1/upload').get_data(as_text=True)
        self.assertIn('File is too large', message)
        self.assertIn('1 MB', message)

    def test_redirect_never_leaves_the_site(self):
        response = upload(self.client, 2 * 1024 * 1024, headers={'Referer': 'http://evil.example/x'})
        self.assertEqual(response.headers['Location'], '/')

    def test_no_referrer_goes_home(self):
        self.assertEqual(upload(self.client, 2 * 1024 * 1024).headers['Location'], '/')

    def test_fetch_style_requests_get_json_not_a_redirect(self):
        response = upload(self.client, 2 * 1024 * 1024, path='/profile/save/identity', field='profile_pic')
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('File is too large', response.get_json()['error'])
        response = upload(self.client, 2 * 1024 * 1024, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
