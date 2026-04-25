import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db
init_db()
client = app.test_client()
# ensure admin exists
import subprocess
subprocess.run(['python','add_admin_user.py'])
# get edit page for event 1
resp = client.get('/admin/events/1/edit')
print('Status', resp.status_code)
print(resp.data.decode()[:400])
