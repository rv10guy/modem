from flask import Flask, request, jsonify
from flask_socketio import SocketIO
from gsmmodem import GSMModem  # Assuming gsmmodem.py is in the same directory as your Flask app

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize modem
modem = GSMModem('/dev/ttyUSB0', 9600)

@app.route('/messages', methods=['GET', 'POST', 'DELETE'])
def messages():
    if request.method == 'GET':
        modem.sync_messages()
        return jsonify(modem.list_messages())
    elif request.method == 'POST':
        number = request.json.get('number')
        text = request.json.get('text')
        modem.send_sms(number, text)
        return '', 204
    elif request.method == 'DELETE':
        msg_id = request.json.get('id')
        modem.delete_message(msg_id)
        return '', 204

@app.route('/messages/<int:msg_id>/read', methods=['POST'])
def mark_as_read(msg_id):
    modem.mark_as_read(msg_id)
    return '', 204

if __name__ == '__main__':
    socketio.run(app, debug=True)
