from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room 
import serial, re, time, paho.mqtt.client as mqtt, threading, json, queue, speedtest, atexit, sys, logging, serial.tools.list_ports, configparser
from serial import SerialException
from datetime import datetime
from flask_cors import CORS

config = configparser.ConfigParser()
config.read('modem_mgmt.ini')

# Read config file
flask_port = int(config.get('Flask', 'port', fallback=8080))
flask_secret_key = config.get('Flask', 'secret_key', fallback='secret!')
mqtt_broker = config.get('MQTT', 'broker')
mqtt_port = int(config.get('MQTT', 'port'))
mqtt_username = config.get('MQTT', 'username')
mqtt_password = config.get('MQTT', 'password')
modem_port = config.get('Modem', 'port', fallback=None)
modem_baudrate = int(config.get('Modem','baudrate', fallback=115200))

# Read Identities from config file
identity = {}
for section in config.sections():
    if section == 'Identities':
        for option in config.options(section):
            if option.startswith('identity'):
                identity_number = option.split('_')[0][8:]
                attribute = option.split('_')[1]
                value = config.get(section, option)
                if identity_number not in identity:
                    identity[identity_number] = {}
                identity[identity_number][attribute] = value

# Flask setup
app = Flask(__name__)
app.config['SECRET_KEY'] = flask_secret_key
CORS(app)
#socketio = SocketIO(app, cors_allowed_origins="*")
socketio = SocketIO(app, async_mode='gevent', websocket=True, engineio_logger=False, cors_allowed_origins="*")

# Configure logging for Flask
app.logger.setLevel(logging.ERROR)

# Configure logging for SocketIO
socketio.server_options['logger_level'] = logging.ERROR

# Disable access logs for Flask
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
socketio.server_options['logger_level'] = logging.ERROR

# Load the MCC-MNC table
with open('mcc-mnc-list.json', 'r') as f:
    mcc_mnc_table = json.load(f)

client= mqtt.Client()  
latest_status_values = {}

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

# Thread to setup the command queue
command_queue = queue.Queue()

def mqtt_connect(client, mqtt_broker, mqtt_port, mqtt_username, mqtt_password, max_retries=5, retry_interval=30):
    client.username_pw_set(mqtt_username, mqtt_password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    retries = 0
    while retries < max_retries:
        try:
            client.connect(mqtt_broker, mqtt_port, 60)
            client.loop_start()
            return
        except Exception as e:
            print(f"Failed to connect to MQTT broker. Retrying in {retry_interval} seconds. Error: {e}")
            time.sleep(retry_interval)
            retries += 1

    print("Failed to connect to MQTT broker after maximum retries.")

def mqtt_disconnect(client):
    try:
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"Failed to disconnect from MQTT broker. Error: {e}")

def serial_disconnect(ser):
    try:
        ser.close()
    except Exception as e:
        print(f"Failed to disconnect from serial port. Error: {e}")

# MQTT on_connect callback
def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result code: {rc}")

# MQTT on_disconnect callback
def on_disconnect(client, userdata, rc):
    print("Disconnected from MQTT broker with result code: " + str(rc))


def safe_socketio_emit(event, data=None, room=None):
    try:
        socketio.emit(event, data, room=room)
    except Exception as e:
        print(f"Error while emitting event '{event}': {str(e)}")
        # Consider using a logging library to log the errors

def safe_mqtt_publish(topic, payload, qos=0, retain=False):
    try:
        client.publish(topic, payload, qos, retain)
    except Exception as e:
        print(f"Error while publishing MQTT message to topic '{topic}': {str(e)}")
        # Consider using a logging library to log the errors

def send_at_command(command, ser, timeout=10, message=None):
    response = ''

    try:
        if message:
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

# Process queued commands
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

# Determine the carrier by MCC-MNC
def get_carrier(mcc, mnc):
    for item in mcc_mnc_table:
        if item['mcc'] == mcc and item['mnc'] == mnc:
            return item['operator']
    return None

def process_at_command(response):
    safe_socketio_emit('display_response', response)

# Process AT+QSCAN response for available networks    
def process_scan_response(response):
    # split the response into lines
    response_lines = response.strip().split('\n')
    
    # define the names of the data
    data_names = ["network_type", "MCC", "MNC", "frequency", "Cell ID", "RSRP", "RSRQ", "srvlev", "squal", "cellID", "TAC", "Bandwidth", "Band"]
    
    # list to hold the results
    result = []

    # process each line
    for line in response_lines:
        if '+QSCAN:' in line:
            # remove '+QSCAN:' and strip leading and trailing spaces
            line = line.replace('+QSCAN:', '').strip()

            # split the line by comma
            line_values = line.split(',')

            # create a dictionary with the data names as keys and the line values as values
            full_data_dict = dict(zip(data_names, line_values))

            # remove quotation marks from the network type
            network_type = full_data_dict['network_type'].replace('"', '')

            # create a new dictionary with only the desired fields
            data_dict = {
                "Carrier": get_carrier(full_data_dict['MCC'], full_data_dict['MNC']),
                "Network Type": full_data_dict['network_type'],
                "Band": int(full_data_dict['Band']),
                "RSRP": int(full_data_dict['RSRP']),
                "RSRQ": int(full_data_dict['RSRQ'])
                
            }
           # check if data_dict already exists in result list
            if str(data_dict) not in [str(d) for d in result]:
                # add the dictionary to the result list
                result.append(data_dict)

    # Sort the result by "Carrier" and then by "Band"
    result = sorted(result, key=lambda k: (k['Carrier'], k['Band']))

    # emit the result to the client
    print("Network scan complete.")
    safe_socketio_emit('network_scan_result', result)

    # when scan is done, emit a message to hide "Please wait..."
    safe_socketio_emit('hide_overlay', room= "global")


# Scan for available networks
def perform_network_scan(scan_type):
    print("Performing network scan")
    # send the appropriate AT command based on scan_type
    if scan_type == 'LTE':
        command = "AT+QSCAN=1,1"  # replace with the correct AT command
    elif scan_type == '5G':
        command = "AT+QSCAN=2,1"  # replace with the correct AT command
    elif scan_type == 'Both':
        command = "AT+QSCAN=3,1"  # replace with the correct AT command
    else:
        return 'Invalid scan type'
    
    # Put the command, process_scan_response function, ser, and timeout into the queue
    command_queue.put((command, ser, 180, None, process_scan_response))
    pass

# Process the AT+QNWINFO response for current band and registration status
def process_qnwinfo_response(response):
    response_lines = response.splitlines()

    band = None
    status = "Not registered or invalid response"  # Move the default status assignment outside the loop

    for line in response_lines:
        match = re.search(r'\+QNWINFO:\s+"(.+?)","(.+?)","(.+?)",(\d+)', line)

        if match:
            mode, operator, band, channel = match.groups()
            status = f"Registered, {mode}"
            break  # Add a break statement to exit the loop once a match is found

    safe_socketio_emit("cellular/band", band , room= "global")
    safe_mqtt_publish("cellular/band", band, 0, True)
    latest_status_values["band"] = band
    safe_socketio_emit("cellular/registration_status", status, room= "global")
    safe_mqtt_publish("cellular/registration_status", status, 0, True)
    latest_status_values["registration_status"] = status
    pass

# Process the AT+QCSQ response for signal strength and quality
def process_qcsq_response(response):
    response_lines = response.strip().split('\n')
    qcsq_line = None

    for line in response_lines:
        if '+QCSQ:' in line:
            qcsq_line = line.strip()
            break

    if qcsq_line:
        parts = qcsq_line.split(',')
        sysmode = parts[0].split(':')[-1].strip().strip('"')
        values = [int(val.strip()) for val in parts[1:] if val.strip().isdigit() or (val.strip()[0] == '-' and val.strip()[1:].isdigit())]

        if sysmode == "LTE":
            rsrp, rsrq = values[1], values[3]
        elif sysmode == "NR5G":
            rsrp, rsrq = values[0], values[2]
        else:
            return None, None, None, None

        signal_strength = ((rsrp + 140) / (140 - 44)) * 100
        signal_quality = ((rsrq + 20) / (20 - 3)) * 100
    
    if sysmode and rsrp and rsrq and signal_strength and signal_quality:
            safe_socketio_emit('cellular/rsrp', str(rsrp), room= "global")
            safe_socketio_emit('cellular/rsrq', str(rsrq), room= "global")
            safe_socketio_emit('cellular/signal_strength', f"{signal_strength:.2f}%", room= "global")
            safe_socketio_emit('cellular/signal_quality', f"{signal_quality:.2f}%", room= "global")
            safe_socketio_emit('cellular/system_mode', sysmode, room= "global")

            safe_mqtt_publish("cellular/rsrp", str(rsrp), 0, True)
            safe_mqtt_publish("cellular/rsrq", str(rsrq), 0, True)
            safe_mqtt_publish("cellular/signal_strength", f"{signal_strength:.2f}%", 0, True)
            safe_mqtt_publish("cellular/signal_quality", f"{signal_quality:.2f}%", 0, True)
            safe_mqtt_publish("cellular/system_mode", sysmode, 0, True)

            latest_status_values["rsrp"] = str(rsrp)
            latest_status_values["rsrq"] = str(rsrq)
            latest_status_values["signal_strength"] = f"{signal_strength:.2f}%"
            latest_status_values["signal_quality"] = f"{signal_quality:.2f}%"
            latest_status_values["system_mode"] = sysmode

# Process the AT+GSN response for IMEI
def process_imei_response(response):
    imei = re.search(r'^\d{15}', response, re.MULTILINE)
    if imei:
        imei = imei.group(0)
    safe_socketio_emit("cellular/imei", imei, room= "global")
    safe_mqtt_publish("cellular/imei", imei, 0, True)
    latest_status_values["imei"] = imei

def format_iccid(raw_iccid):
    formatted_iccid = ""
    for i in range(0, len(raw_iccid), 2):
        formatted_iccid += raw_iccid[i + 1:i + 2] + raw_iccid[i:i + 1]
    
    # Remove the trailing 'F' if it exists
    if formatted_iccid[-1] == 'F':
        formatted_iccid = formatted_iccid[:-1]

    return formatted_iccid

def process_iccid_response(response):
    iccid_match = re.search(r'\+CRSM: \d+,\d+,"(\d{18,20}[A-F\d]{0,2})"', response, re.MULTILINE)
    iccid = "Invalid response"
    if iccid_match:
        raw_iccid = iccid_match.group(1)
        iccid = format_iccid(raw_iccid)

    safe_socketio_emit("cellular/iccid", iccid, room="global")
    safe_mqtt_publish("cellular/iccid", iccid, 0, True)
    latest_status_values["iccid"] = iccid    

# Process the AT+CGDCONT response for APN    
def process_apn_response(response):
    apn = re.search(r'CGDCONT: \d+,"[^"]+","([^"]+)', response)
    if apn:
        apn = apn.group(1)
    safe_socketio_emit("cellular/apn", apn, room= "global")
    safe_mqtt_publish("cellular/apn", apn, 0, True)
    latest_status_values["apn"] = apn

# Process Identity Response 
def process_identity_response(response):
    safe_mqtt_publish("cellular/identity", latest_status_values["identity"], 0, True)
    safe_socketio_emit('identity', latest_status_values["identity"])

# Process User Setting LTE Mode Response
def process_mode_lte_response(response):
    safe_mqtt_publish("cellular/usermode", "LTE", 0, True)
    latest_status_values["usermode"] = "LTE"

# Process User Setting Auto-Auto Mode Response
def process_mode_fullauto_response(response):
    safe_mqtt_publish("cellular/usermode", "Full Auto", 0, True)
    latest_status_values["usermode"] = "Full Auto"

# Process User Setting Auto-5GSA Mode Response
def process_mode_auto_5gsa_response(response):
    safe_mqtt_publish("cellular/usermode", "Auto - 5G SA", 0, True)
    latest_status_values["usermode"] = "Auto - 5G SA"

# Process User Setting Auto-5GNSA Mode Response
def process_mode_auto_5gnsa_response(response):
    safe_mqtt_publish("cellular/usermode", "Auto - 5G NSA", 0, True)
    latest_status_values["usermode"] = "Auto - 5G NSA"

# Process User Setting 5GSAOnly Mode Response
def process_mode_5g_sa_response(response):
    safe_mqtt_publish("cellular/usermode", "5G SA", 0, True)
    latest_status_values["usermode"] = "5G SA"

# Process User Setting 5GNSAOnly Mode Response
def process_mode_5g_nsa_response(response):
    safe_mqtt_publish("cellular/usermode", "5G NSA", 0, True)
    latest_status_values["usermode"] = "5G NSA"

# Process User Setting 5G Auto Mode Response
def process_mode_5g_auto_response(response):
    safe_mqtt_publish("cellular/usermode", "5G Auto", 0, True)
    latest_status_values["usermode"] = "5G Auto"

# Process User Setting LTE Mode
def modem_mode_lte():
    command_queue.put(('AT+QNWPREFCFG="mode_pref",LTE', ser, 10, None, process_mode_lte_response))
    safe_socketio_emit('modem_mode', "LTE")

# Process User Setting Full Auto Mode
def modem_mode_fullauto():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",1', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",AUTO', ser, 10, None, process_mode_fullauto_response))
    safe_socketio_emit('modem_mode', "Auto-Auto")

# Process User Setting Auto - 5g SA Mode
def modem_mode_auto_5gsa():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",2', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",AUTO', ser, 10, None, process_mode_auto_5gsa_response))
    safe_socketio_emit('modem_mode', "Auto-5GSA")

# Process User Setting Auto - 5g NSA Mode
def modem_mode_auto_5gnsa():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",1', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",AUTO', ser, 10, None, process_mode_auto_5gnsa_response))
    safe_socketio_emit('modem_mode', "Auto-5GNSA")

# Process User Setting 5G Auto Mode
def modem_mode_5g_auto():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",0', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",NR5G', ser, 10, None, process_mode_5g_auto_response))
    safe_socketio_emit('modem_mode', "5G Auto")

# Process User Setting 5G Only - SA Mode
def modem_mode_5g_sa():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",2', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",NR5G', ser, 10, None, process_mode_5g_sa_response))
    safe_socketio_emit('modem_mode', "5G SA") 

# Process User Setting 5G Only - NSA Mode
def modem_mode_5g_nsa():
    command_queue.put(('AT+QNWPREFCFG="nr5g_disable_mode",1', ser, 10, None, None))
    command_queue.put(('AT+QNWPREFCFG="mode_pref",NR5G', ser, 10, None, process_mode_5g_nsa_response))
    safe_socketio_emit('modem_mode', "5G NSA")

# Process the AT+COPS response for network operator
def process_cops_response(response):
    match = re.search(r'\+COPS: (\d+),(\d+),\"(.+)\",(\d+)', response)
    if match:
        network_operator = match.group(3)
    else:
        network_operator = "Invalid response"
    safe_socketio_emit("cellular/network_operator", network_operator, room= "global")
    safe_mqtt_publish("cellular/network_operator", network_operator, 0, True)
    latest_status_values["network_operator"] = network_operator

# Process the AT+CNUM response for phone number    
def process_phone_number_response(response):
    phone_number = re.search(r'(?<=\").*(?=\",)', response)
    if phone_number:
        phone_number = phone_number.group(0)
    safe_socketio_emit("cellular/phone_number", phone_number, room= "global")
    safe_mqtt_publish("cellular/phone_number", phone_number, 0, True)
    latest_status_values["phone_number"] = phone_number

def process_lockedband_response(response):
    # Initialize an empty dictionary to store the bands
    bands_dict = {}

    # Split the response into lines
    response_lines = response.strip().split('\n')

    # Process each line
    for line in response_lines:
        # Skip the line if it doesn't start with '+QNWPREFCFG:'
        if not line.startswith('+QNWPREFCFG:'):
            continue

        # Remove '+QNWPREFCFG:' and strip leading and trailing spaces
        line = line.replace('+QNWPREFCFG:', '').strip()

        # Split the line by comma
        line_parts = line.split(',')

        # The first part is the band type and the second part is the bands
        band_type = line_parts[0].replace('"', '').strip()
        bands = line_parts[1].split(':')
        # Add the bands to the dictionary
        bands_dict[band_type] = bands

    safe_socketio_emit('locked_bands', bands_dict, room= "global")
    # Publish the bands to the MQTT topics
    for band_type, bands in bands_dict.items():
        # Create the MQTT topic
        topic = f"cellular/{band_type}"

        # Join the bands with commas
        bands_string = ','.join(bands)

        # Publish the bands to the MQTT topic
        safe_mqtt_publish(topic, bands_string, 0, True)
        latest_status_values[band_type] = bands

# Main loop to update cellular info every 60 seconds    
def update_cellular_info():
    while True:
        # Get & Process Current Band
        command_queue.put(("AT+QNWINFO", ser, 10, None, process_qnwinfo_response))    

        # Get signal strength and quality
        command_queue.put(("AT+QCSQ", ser, 10, None, process_qcsq_response))
 
        # Get ICCID
        command_queue.put(("AT+CRSM=176,12258,0,0,10", ser, 10, None, process_iccid_response))

        # Get IMEI
        command_queue.put(("AT+GSN", ser, 10, None, process_imei_response))

        # Get APN
        command_queue.put(("AT+CGDCONT?", ser, 10, None, process_apn_response))

        # Get network operator
        command_queue.put(("AT+COPS?", ser, 10, None, process_cops_response))
        
        # Get phone number
        command_queue.put(("AT+CNUM", ser, 10, None, process_phone_number_response))

        # Emit the latest modem mode
        #safe_socketio_emit('modem_mode', latest_status_values["usermode"])

        time.sleep(60)

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

    # emit the messages to the client
    socketio.emit('messages', decoded_messages)

    return decoded_messages

def get_sms(ser):
    command_queue.put(("AT+CMGF=1", ser, 10, None, None))
    command_queue.put(('AT+CMGL="ALL"', ser, 10, None, process_get_sms_response))

def process_delete_sms_response(response):
    
    return jsonify({"success": True})

def delete_sms(id):
    # Existing code to delete an SMS message
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

    # Create the AT+CMGS command
    command = f'AT+CMGS="{number}"'

    # Add the command to the queue with the message
    command_queue.put((command, ser, 10, message, process_sms_response))
    return jsonify({"success": True})


def get_sms_on_connect():
    messages = get_sms(ser)  # Call the existing get_sms function and pass ser as an argument


@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('send_command')
def handle_send_command(command):
    # Add the command to the queue for processing
    command_queue.put((command, ser, 10, None, process_at_command))

#@app.route('/api/sms', methods=['GET'])
#def get_sms_route():
#    messages = get_sms(ser)  # Call the existing get_sms function and pass ser as an argument
#    return jsonify(messages)

@app.route('/api/sms/<int:id>', methods=['DELETE'])
def delete_sms_route(id):
    result = delete_sms(id)
    get_sms_on_connect()
    return jsonify({"success": True})

@app.route('/api/sms/', methods=['POST', 'OPTIONS'])
def api_sms_route():
    if request.method == 'OPTIONS':
        # Pre-flight request. Reply successfully:
        response = app.make_default_options_response()

        # Allow the possibility of CORS
        headers = response.headers
        headers['Access-Control-Allow-Origin'] = '*'
        headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        headers['Access-Control-Allow-Methods'] = 'OPTIONS, HEAD, GET, POST, DELETE, PUT'

        return response
    elif request.method == 'POST':
        result = send_sms()
        return result

@app.route('/api/select_identity', methods=['POST'])
def select_identity():
    selected_identity = request.json.get('identity')
    
    for key, identity_detail in identity.items():  # Iterating over key-value pairs in the identity dictionary
        if identity_detail['name'] == selected_identity:
            selected_imei = identity_detail['imei']
            selected_apn = identity_detail['apn']
            command = f'AT+EGMR=1,7,"{selected_imei}";+CFUN=1,1;+CGDCONT=1,"IP","{selected_apn}";+CGACT=1,1'
            latest_status_values["identity"] = selected_identity
            command_queue.put((command, ser, 10, None, process_identity_response))
            return "Identity selection received" 
    
    return "No matching identity found", 404

@socketio.on('connect')
def handle_connect():
    try:
        join_room("global")
    except Exception as e:
        print(f"Error joining room: {str(e)}")

    get_sms_on_connect()  # Call the get_sms_on_connect function when a client connects

    identity_names = []
    for identity_number, identity_data in identity.items():
        if 'name' in identity_data:
            identity_names.append(identity_data['name'])

    try:
        safe_socketio_emit('identity_names', identity_names, room="global")
    except Exception as e:
        print(f"Error while emitting identity_names event: {str(e)}")

    for status, value in latest_status_values.items():
        if not isinstance(status, str):
            continue

        if "_band" in status:
            try:
                bands_dict = {status: value}
                safe_socketio_emit('locked_bands', bands_dict, room="global")
            except Exception as e:
                print(f"Error while emitting locked_bands event for {status}: {str(e)}")
        else:
            try:
                safe_socketio_emit(f'cellular/{status}', str(value), room="global")
            except Exception as e:
                print(f"Error while emitting cellular/{status} event: {str(e)}")



@socketio.on('selected_bands')
def handle_selected_bands(selected_bands):
    for band_type, bands in selected_bands.items():
        # Join the bands with colons
        bands_string = ':'.join(bands)

        # Create the AT command
        at_command = f'AT+QNWPREFCFG="{band_type}",{bands_string}'

        # Add the command to the queue
        if band_type == "lte_band":
            command_queue.put((at_command, ser, 10, None, process_lockedband_response))
        elif band_type == "nr5g_band":
            command_queue.put((at_command, ser, 10, None, process_lockedband_response))
            at_command2 = f'AT+QNWPREFCFG="nsa_nr5g_band",{bands_string}'
            command_queue.put((at_command2, ser, 10, None, process_lockedband_response)) 
    
    command_queue.put(('AT+QNWPREFCFG="lte_band"', ser, 10, None, process_lockedband_response))
    command_queue.put(('AT+QNWPREFCFG="nr5g_band"', ser, 10, None, process_lockedband_response))
    print("New band locking applied.")


@socketio.on('network_scan')
def handle_network_scan(scan_type):
    # emit a message to show "Please wait..."
    safe_socketio_emit('show_overlay', {"message": "Performing network scan, please wait..."}, room= "global")

    # perform network scan based on scan_type
    perform_network_scan(scan_type)

@socketio.on('mode')
def handle_modem_mode(mode):
    action_map = {
        "LTE" : modem_mode_lte,
        "Auto-Auto" : modem_mode_fullauto,
        "Auto-SA" : modem_mode_auto_5gsa,
        "Auto-NSA" : modem_mode_auto_5gnsa,
        "5G-SA" : modem_mode_5g_sa,
        "5G-NSA" : modem_mode_5g_nsa,
        "5G-Auto": modem_mode_5g_auto,
    }
    action=action_map.get(mode, "Invalid mode")
    action()

@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)

@socketio.on("run_speedtest")
def handle_run_speedtest():
    safe_socketio_emit("show_overlay", {"message": "Performing Speedtest, please wait..."}, room = "global")
    print("Running speedtest.")
    st = speedtest.Speedtest()
    st.get_best_server()
    st.download()
    st.upload()
    results = st.results.dict()
    safe_socketio_emit("hide_overlay", room = "global")
    safe_socketio_emit("speedtest_results", results, room = "global")
    latest_status_values[speedtest] = results
    print("Speedtest complete.")

# Connect to the MQTT broker
mqtt_connect(client, mqtt_broker, mqtt_port, mqtt_username, mqtt_password)

# Register the mqtt_disconnect function to be called when the program exits
atexit.register(mqtt_disconnect, client)

# Register the serial_disconnect function to be called when the program exits
atexit.register(serial_disconnect, ser)

command_queue.put(('AT+QNWPREFCFG="lte_band"', ser, 10, None, process_lockedband_response))
command_queue.put(('AT+QNWPREFCFG="nr5g_band"', ser, 10, None, process_lockedband_response))

command_queue_thread = threading.Thread(target=process_command_queue)
command_queue_thread.daemon = True
command_queue_thread.start()

update_thread = threading.Thread(target=update_cellular_info)
update_thread.daemon = True
update_thread.start()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=flask_port)  
