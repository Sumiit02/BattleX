import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db, save_registration, DB_NAME
import sqlite3, json
from werkzeug.security import generate_password_hash
from flask import session

print('Initializing DB...')
init_db()
conn = sqlite3.connect(DB_NAME)
cur = conn.cursor()
# create test user
cur.execute("INSERT OR IGNORE INTO users (username,email,password,role) VALUES (?,?,?,?)", ('testuser','t@example.com',generate_password_hash('pass'), 'player'))
conn.commit()
# create event with 1 slot
# cleanup any previous test artifacts
cur.execute("DELETE FROM registrations WHERE order_id IN ('ORD1','ORD2') OR payment_id IN ('PAY1','PAY2')")
cur.execute("DELETE FROM events WHERE slug = ?", ('test-event',))
cur.execute("INSERT INTO events (slug,title,mode,date,prize,max_slots,slots_left,is_open,entry_fee,prize_pool,description) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ('test-event','Test Event','BR','2025-10-24','\u20b9100',1,1,1,0,'','test event'))
conn.commit()
cur.execute("SELECT id, slots_left, is_open FROM events WHERE slug = ?", ('test-event',))
row = cur.fetchone()
if not row:
    print('Failed to create event')
    conn.close()
    raise SystemExit(1)
eid = row[0]
print('Created event id', eid, 'slots_left=', row[1], 'is_open=', row[2])
conn.close()

# Prepare registration payload
payload = {
    'mode': 'BR',
    'email': 't@example.com',
    'game_id': 'GTEST',
    'phone': '9999999999',
    'team_size': '1',
    'event_id': str(eid)
}

with app.test_request_context('/'):
    # set session user to testuser
    session['user'] = 'testuser'
    print('Attempting first save_registration...')
    ok = save_registration(payload, payment_id='PAY1', order_id='ORD1')
    print('First save_registration returned', ok)

    # read event state
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT slots_left, is_open FROM events WHERE id = ?', (eid,))
    r = cur.fetchone()
    print('After first registration, slots_left=', r[0], 'is_open=', r[1])
    conn.close()

    print('Attempting second save_registration (should fail)')
    ok2 = save_registration(payload, payment_id='PAY2', order_id='ORD2')
    print('Second save_registration returned', ok2)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT slots_left, is_open FROM events WHERE id = ?', (eid,))
    r2 = cur.fetchone()
    print('After second attempt, slots_left=', r2[0], 'is_open=', r2[1])
    conn.close()
