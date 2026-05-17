from picamera2 import Picamera2
import time
from PIL import Image
import spidev
import random

spi = spidev.SpiDev()
spi.open(0, 0)  # Opens /dev/spidev0.0 (Bus 0, Device 0)
spi.max_speed_hz = 1000000  # 1 MHz (Safe starting speed)
spi.mode = 0  # SPI Mode 0 (CPOL=0, CPHA=0)`

try:
    while True:
        if random.random() < 0.5:
            to_send = [0x01, 0x02, 0x03, 0x00]
        else:
            to_send = [0x11, 0x12, 0x33, 0x00]
        response = spi.xfer2(to_send.copy())
        reply = response[-1]

        print(f"Sent: {to_send} | Raw RX: {response} | Reply byte: 0x{reply:02X}")
        time.sleep(1)

except KeyboardInterrupt:
    spi.close()
    print("\nSPI connection closed.")
