import sqlite3
DB='gamezone.db'
conn=sqlite3.connect(DB)
cur=conn.cursor()
# insert a fake registration (minimal fields matching table)
cur.execute("INSERT INTO registrations (username, email, game_id, phone, mode, amount, status) VALUES (?, ?, ?, ?, ?, ?, ?)", ('testuser','t@example.com','GAME123','9999999999','BR',10000,'completed'))
reg_id=cur.lastrowid
# insert notification
import json
msg=f"New registration: BR by testuser (reg id {reg_id})"
meta=json.dumps({'registration_id':reg_id,'username':'testuser','mode':'BR'})
cur.execute("INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)", ('registration', msg, meta))
conn.commit()
print('Inserted reg', reg_id)
# show latest notifications
cur.execute('SELECT id, type, message, metadata, is_read, created_at FROM notifications ORDER BY created_at DESC LIMIT 5')
print(cur.fetchall())
conn.close()
