import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db
init_db()
client = app.test_client()
# Try admin login via /login (single page)
payload = {'username':'admin1','password':'ADMINPASS123','admin_code':'ADMIN2025'}
r = client.post('/login', data=payload, follow_redirects=False)
print('Status', r.status_code)
print('Location:', r.headers.get('Location'))
resp = client.post('/login', data=payload, follow_redirects=True)
print('Final status', resp.status_code)
print(resp.data.decode()[:400])
