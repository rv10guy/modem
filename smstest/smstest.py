from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from gsmmodem.modem import GsmModem

app = Flask(__name__)
socketio = SocketIO(app, websocket=True, engineio_logger=False, cors_allowed_origins="*")

modem = GsmModem('/dev/ttyUSB2', 115200)
modem.connect()
print("Modem Connected")

# Serve HTML page at root
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/sms', methods=['GET'])
def get_sms():
    messages = modem.listStoredSms(delete=False)
    decoded_messages = []
    for msg in messages:
        status = "read" if msg.status == 0 else "unread"
        decoded_messages.append({
            "index": msg.index,
            "number": msg.number,
            "text": msg.text,
            "time": msg.time,
            "status": status
        })
    return jsonify(decoded_messages)

# API 2: Mark a stored message as read
@app.route('/sms/<int:id>/read', methods=['POST'])
def mark_as_read(id):
    modem.processStoredSms(False)
    return jsonify({"success": True})

# API 3: Delete a stored message
@app.route('/sms/<int:id>', methods=['DELETE'])
def delete_sms(id):
    modem.deleteStoredSms(id)
    return jsonify({"success": True})


# API 4: Send a text message
@app.route('/sms', methods=['POST'])
def send_sms():
    number = request.json.get('number')
    message = request.json.get('message')
    modem.sendSms(number, message)
    return jsonify({"success": True})

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8080)
