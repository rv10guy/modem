#!/usr/bin/env python

import logging
from gsmmodem.modem import GsmModem

# PORT = 'COM5' # ON WINDOWS, Port is from COM1 to COM9 ,
# We can check using the 'mode' command in cmd
PORT = '/dev/ttyUSB2'
BAUDRATE = 115200

PIN = None  # SIM card PIN (if any)


def main():
    print('Initializing modem...')
    modem = GsmModem(PORT, BAUDRATE)
    modem.connect(PIN)
    messages = modem.listStoredSms(delete=False)
    print(messages) 
    modem.close()


if __name__ == '__main__':
    main()