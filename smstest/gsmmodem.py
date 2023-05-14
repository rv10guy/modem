import sqlite3
from gsmmodem.modem import GsmModem

class GSMModem:
    def __init__(self, port, baudrate):
        self.modem = GsmModem(port, baudrate)
        self.modem.connect()

        self.conn = sqlite3.connect('messages.db')
        c = self.conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS messages
            (id INTEGER PRIMARY KEY, number TEXT, text TEXT, status TEXT)
        ''')
        self.conn.commit()

    def sync_messages(self):
        c = self.conn.cursor()

        # Delete all messages from the database
        c.execute('DELETE FROM messages')

        # Fetch all messages from the modem and insert them into the database
        messages = self.modem.listSms(status='ALL')
        for msg in messages:
            c.execute('INSERT INTO messages (number, text, status) VALUES (?, ?, ?)',
                      (msg.number, msg.text, msg.status))

        self.conn.commit()

    def list_messages(self):
        c = self.conn.cursor()
        c.execute('SELECT * FROM messages')
        return c.fetchall()

    def mark_as_read(self, msg_id):
        c = self.conn.cursor()
        c.execute('UPDATE messages SET status = "REC READ" WHERE id = ?', (msg_id,))
        self.conn.commit()

    def delete_message(self, msg_id):
        c = self.conn.cursor()
        c.execute('DELETE FROM messages WHERE id = ?', (msg_id,))
        self.conn.commit()