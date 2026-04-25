"""
Simple DB test script to initialize DB, list events, create a test user, and perform a registration.
Run: python tools\db_test.py
"""
import os
import sys
# Ensure project root is on sys.path so we can import app when running from tools/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import init_db, get_events, save_registration, DB_NAME, app
import sqlite3
from werkzeug.security import generate_password_hash

def ensure_user(username='testplayer', email='test@example.com'):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = ?", (username,))
    if not cur.fetchone():
        cur.execute("INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, 'player')",
                    (username, email, generate_password_hash('password123')))
        conn.commit()
    conn.close()

if __name__ == '__main__':
    print('Initializing DB...')
    init_db()
    with app.app_context():
        print('Events:')
        events = get_events()
        for e in events:
            print(f" - [{e['id']}] {e['title']} slots_left={e['slots_left']}/{e['max_slots']}")

        ensure_user()
        print('Ensured test user exists')

        # Simulate a registration (without real payment)
        test_event = events[0] if events else None
        if test_event:
            payload = {
                'mode': 'BR' if 'BR' in test_event['mode'] else 'CS',
                'email': 'test@example.com',
                'game_id': 'TEST123',
                'phone': '9999999999',
                'team_size': 1,
                'event_id': test_event['id']
            }
            print('Attempting save_registration...')
            ok = save_registration(payload, payment_id='TESTPAY123', order_id='TESTORDER123')
            print('save_registration result:', ok)
            events = get_events()
            for e in events:
                if e['id'] == test_event['id']:
                    print('After registration slots_left=', e['slots_left'])
                    break
        else:
            print('No events available to test')
