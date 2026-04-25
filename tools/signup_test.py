"""
Simple test to POST to /signup using Flask test client and confirm a new user is in the DB.
Run: python tools\signup_test.py
"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import app, init_db, DB_NAME
import sqlite3

TEST_USER = {
    'username': 'ci_test_user',
    'email': 'ci_test_user@example.com',
    'password': 'TestPass123',
    'password2': 'TestPass123',
    'role': 'player',
    'game_id': 'CI123',
    'phone': '9000000000'
}

if __name__ == '__main__':
    init_db()
    client = app.test_client()
    # Make sure test user is not already present
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('DELETE FROM users WHERE username = ?', (TEST_USER['username'],))
    conn.commit()
    conn.close()

    # Perform signup POST
    resp = client.post('/signup', data=TEST_USER, follow_redirects=True)
    print('Signup status code:', resp.status_code)
    # Check DB
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT username, email, role FROM users WHERE username = ?', (TEST_USER['username'],))
    row = cur.fetchone()
    conn.close()
    if row:
        print('User found in DB:', row)
    else:
        print('User not found in DB')
