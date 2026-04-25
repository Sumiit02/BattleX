import sqlite3
from werkzeug.security import generate_password_hash

DB_NAME = "gamezone.db"

def add_demo_player():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Remove any existing demo player
    cur.execute("DELETE FROM users WHERE username = ? AND role = 'player'", ('demoplayer',))
    password_hash = generate_password_hash('DEMO1234')
    cur.execute("""
        INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        'demoplayer',
        'demo@example.com',
        password_hash,
        'player',
        'DEMOID123',
        '9000000000',
        None
    ))
    conn.commit()
    conn.close()
    print("Demo player added successfully.")

if __name__ == "__main__":
    add_demo_player()