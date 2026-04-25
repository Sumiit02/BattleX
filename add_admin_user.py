import sqlite3

DB_NAME = "gamezone.db"

def add_admin():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Remove any existing admin1 user
    cur.execute("DELETE FROM users WHERE username = ? AND role = 'admin'", ('admin1',))
    from werkzeug.security import generate_password_hash
    password_hash = generate_password_hash('ADMINPASS123')
    cur.execute("""
        INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        'admin1',
        'admin1@example.com',
        password_hash,
        'admin',
        None,
        '9876543210',
        'ADMIN2025'
    ))
    conn.commit()
    conn.close()
    print("Admin user reset and added successfully.")

if __name__ == "__main__":
    add_admin()