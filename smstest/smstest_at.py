from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import serial, time, serial.tools.list_ports, atexit, threading, queue
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, websocket=True, engineio_logger=False, cors_allowed_origins="*")

modem_port = '/dev/ttyUSB2'
modem_baudrate = 115200
# Thread to setup the command queue
command_queue = queue.Queue()

# Find the modem and configure it
if modem_port == None:
    print('Modem port not specified. Searching for modem...')
    modem = None
    for port in serial.tools.list_ports.comports():
        if port.device.startswith('/dev/ttyUSB'):
            try:
                ser = serial.Serial(port.device, modem_baudrate)
                # send a command to the modem to verify that it's the correct device
                ser.write(b'AT\r\n')
                response = b''
                start_time = time.monotonic()
                while not response.endswith(b'OK\r\n'):
                    c = ser.read()
                    response += c
                    if time.monotonic() - start_time > 1:
                        # timeout occurred, move on to next port
                        break
                if response.endswith(b'OK\r\n'):
                    modem_port = port.device
                    break
                else:
                    ser.close()
            except serial.SerialException:
                # could not open port, skip it
                pass

    if modem_port is None:
        print('Modem not found.')
    else:
        print(f'Modem found on port {modem_port}.')
else:
    print(f'Modem port specified as {modem_port}.')
    ser = serial.Serial(modem_port, baudrate=modem_baudrate)

ser.reset_input_buffer()

def serial_disconnect(ser):
    try:
        ser.close()
    except Exception as e:
        print(f"Failed to disconnect from serial port. Error: {e}")

def send_at_command(command, ser, timeout=10, message=None):
    response = ''

    try:
        if message:
            print("Sending message in send_at_command)")
            print(command)
            print(message)  
            ser.write((command + '\r\n').encode())
            time.sleep(0.5)
            ser.write(message.encode())
            ser.write(chr(26).encode())
        else:
            ser.write((command + '\r\n').encode())
            ser.flush()

        end_time = time.time() + timeout

        while time.time() < end_time:
            while ser.inWaiting() > 0:
                response += ser.readline().decode()

            if response.endswith('OK\r\n') or response.endswith('ERROR\r\n'):
                break

            time.sleep(0.1)  # short delay before the next read attempt
    except serial.SerialException as e:
        print(f"Serial communication error: {str(e)}")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")

    return response

def process_command_queue():
    while True:
        # get the next command from the queue
        command, ser, timeout, message, callback = command_queue.get()

        # send the command and process the response
        response = send_at_command(command, ser, timeout, message)

        # process the response here, e.g., by publishing to MQTT or updating the Flask server
        if callback:
            with app.app_context():  # Create a new application context
                callback(response)

        # indicate that the task is done
        command_queue.task_done()

def parse_date(date_str):
    # Remove the leading comma
    date_str = date_str.lstrip(',')
    # Remove the last three characters ("-28")
    date_str = date_str[:-3]
    # Parse the date and time from the string
    dt = datetime.strptime(date_str, "%y/%m/%d,%H:%M:%S")
    # Convert the datetime to a standardized format
    return dt.strftime("%I:%M:%S%p %B %d, %Y")


def parse_status(status_str):
    if status_str == 'REC READ':
        return 'Read'
    elif status_str == 'REC UNREAD':
        return 'New'
    else:
        return status_str  # If we don't recognize the status, just return it as is

def process_get_sms_response(response):
    print(response)
    decoded_messages = []

    lines = response.splitlines()
    message_data = None
    for line in lines:
        line = line.strip()
        if line.startswith('+CMGL:'):
            parts = line.split(',')
            index = parts[0].split(':')[1].strip()
            status = parse_status(parts[1].replace('"', '').strip())
            number = parts[2].replace('"', '').strip()
            time = parse_date(','.join(parts[3:]).replace('"', '').strip())
            message_data = {
                "index": index,
                "number": number,
                "time": time,
                "status": status,
                "text": ""  # Empty text for now, we'll fill this in the next line
            }
        elif message_data is not None:
            # The line after +CMGL: should be the message text
            message_data["text"] = line
            # Once we have the text, we can add the message data to the decoded messages
            decoded_messages.append(message_data)
            message_data = None  # Reset for the next message

    print(decoded_messages)

    # emit the messages to the client
    socketio.emit('messages', decoded_messages)

    return decoded_messages


def get_sms(ser):
    command_queue.put(("AT+CMGF=1", ser, 10, None, None))
    command_queue.put(('AT+CMGL="ALL"', ser, 10, None, process_get_sms_response))

def process_delete_sms_response(response):
    # Existing code to delete an SMS message
    return jsonify({"success": True})

def delete_sms(id):
    # Existing code to delete an SMS message
    print(f"Deleting message with index {id}")
    command = f'AT+CMGD={id}'  # Replace with the appropriate AT command for deleting an SMS message by index
    command_queue.put((command, ser, 10, None, process_delete_sms_response))

def process_sms_response(response):
    if 'OK' in response:
        return jsonify({"success": True})
    else:
        # Handle the failure case
        return jsonify({"success": False, "error": response}), 500

def send_sms():
    command_queue.put(("AT+CMGF=1", ser, 10, None, None))
    number = request.json.get('number')
    message = request.json.get('message')
    print(f"Sending message to {number}")
    print(f"Message content: {message}")

    # Create the AT+CMGS command
    command = f'AT+CMGS="{number}"'

    # Add the command to the queue with the message
    command_queue.put((command, ser, 10, message, process_sms_response))
    return jsonify({"success": True})


def get_sms_on_connect():
    messages = get_sms(ser)  # Call the existing get_sms function and pass ser as an argument


# Serve HTML page at root
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sms', methods=['GET'])
def get_sms_route():
    messages = get_sms(ser)  # Call the existing get_sms function and pass ser as an argument
    return jsonify(messages)

@app.route('/api/sms/<int:id>', methods=['DELETE'])
def delete_sms_route(id):
    result = delete_sms(id)
    get_sms_on_connect()
    return result

@app.route('/api/sms', methods=['POST'])
def send_sms_route():
    print(request.json) 
    print("Sending SMS")
    result = send_sms()
    return result

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    get_sms_on_connect()  # Call the get_sms_on_connect function when a client connects


@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

# Register the serial_disconnect function to be called when the program exits
atexit.register(serial_disconnect, ser)

command_queue_thread = threading.Thread(target=process_command_queue)
command_queue_thread.daemon = True
command_queue_thread.start()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8080)