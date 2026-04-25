import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db
init_db()
client = app.test_client()
# login as admin
payload = {'username':'admin1','password':'ADMINPASS123','admin_code':'ADMIN2025'}
resp = client.post('/login', data=payload, follow_redirects=True)
# now GET admin events
r = client.get('/admin/events')
print('admin/events status', r.status_code)
print(r.data.decode()[:800])
