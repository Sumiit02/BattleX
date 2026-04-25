import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db

init_db()
client = app.test_client()
# need an admin user in session; simulate by creating a user and setting session via login route (we'll just test GET of page without auth check by temporarily not logged in)
resp = client.get('/admin/events')
print('Status', resp.status_code)
print(resp.data.decode()[:400])
