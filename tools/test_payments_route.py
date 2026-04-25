import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db

init_db()
client = app.test_client()
resp = client.get('/admin/payments')
print('Status', resp.status_code)
print(resp.data.decode()[:800])
