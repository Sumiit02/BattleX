import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db
import requests

init_db()
client = app.test_client()
# credentials from add_admin_user.py
payload = {
    'username': 'admin1',
    'password': 'ADMINPASS123',
    'admin_code': 'ADMIN2025'
}
resp = client.post('/admin_login', data=payload, follow_redirects=False)
print('Status', resp.status_code)
# print location header if any
print('Location:', resp.headers.get('Location'))
# If it redirected, fetch the redirect target with follow_redirects to inspect final page
resp_follow = client.post('/admin_login', data=payload, follow_redirects=True)
print('Final status after follow:', resp_follow.status_code)
print(resp_follow.data.decode()[:1200])
