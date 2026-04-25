import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import init_db, get_events, app
init_db()
with app.test_request_context('/'):
    ev = get_events()
    print('Loaded', len(ev), 'events')
    if ev:
        print(ev[0])
