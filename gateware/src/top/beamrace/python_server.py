#!/usr/bin/env python3
"""Capture a camera frame and upload it to beamrace over SPI.

Wiring (matches spi_test): EX1 PMOD CS=1, SCK=7, MOSI=9, MISO=8.
Protocol:
- Packet: SOF(0xA5,0x5A), frame_id, pix_off_hi, pix_off_lo, pix_len_hi, pix_len_lo,
  payload (RGB bytes, 3*pix_len), crc_hi, crc_lo.
- Host appends one dummy byte to clock out status.
- Status: 0x11 ACK, 0xE1 length/range error, 0xE2 CRC error.
"""

import argparse
import time
from pathlib import Path

from picamera2 import Picamera2
from PIL import Image
import spidev

IMAGE_W = 128
IMAGE_H = 75
N_PIXELS = IMAGE_W * IMAGE_H
SOF0 = 0xA5
SOF1 = 0x5A
ACK = 0x11


def crc16_ccitt(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def capture_rgb_bytes(picam, tmp_path):
    picam.capture_file(tmp_path)

    with Image.open(tmp_path).convert("RGB") as img:
        print("full size", img.size)
        img_small = img.resize((IMAGE_W, IMAGE_H))
        return list(img_small.tobytes())


def packet_for_chunk(frame_id, pix_off, rgb_payload):
    pix_len = len(rgb_payload) // 3
    header = [
        frame_id & 0xFF,
        (pix_off >> 8) & 0xFF,
        pix_off & 0xFF,
        (pix_len >> 8) & 0xFF,
        pix_len & 0xFF,
    ]
    crc = crc16_ccitt(header + rgb_payload)
    return [SOF0, SOF1] + header + rgb_payload + [(crc >> 8) & 0xFF, crc & 0xFF]


def upload_frame(spi, rgb_bytes, frame_id, chunk_pixels, retries):
    if len(rgb_bytes) != N_PIXELS * 3:
        raise ValueError(f"Expected {N_PIXELS*3} bytes, got {len(rgb_bytes)}")

    n_chunks = (N_PIXELS + chunk_pixels - 1) // chunk_pixels
    for chunk_i in range(n_chunks):
        pix_off = chunk_i * chunk_pixels
        pix_len = min(chunk_pixels, N_PIXELS - pix_off)
        payload = rgb_bytes[pix_off * 3 : (pix_off + pix_len) * 3]
        packet = packet_for_chunk(frame_id, pix_off, payload)

        ok = False
        for attempt in range(1, retries + 1):
            rx = spi.xfer2(packet + [0x00])
            status = rx[-1]
            if status == ACK:
                ok = True
                break
            print(
                f"Chunk {chunk_i+1}/{n_chunks} off={pix_off} len={pix_len} "
                f"attempt={attempt} status=0x{status:02X}"
            )

        if not ok:
            raise RuntimeError(
                f"Chunk {chunk_i+1}/{n_chunks} failed after {retries} retries"
            )

        if chunk_i % 4 == 0 or chunk_i == n_chunks - 1:
            print(f"Uploaded {chunk_i+1}/{n_chunks} chunks")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus", type=int, default=0)
    parser.add_argument("--dev", type=int, default=0)
    parser.add_argument("--hz", type=int, default=1_000_000)
    parser.add_argument("--chunk-pixels", type=int, default=128)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--tmp-image", default="beamrace_capture.full.png")
    args = parser.parse_args()

    spi = spidev.SpiDev()
    spi.open(args.bus, args.dev)
    spi.max_speed_hz = args.hz
    spi.mode = 0

    frame_id = 0
    tmp_path = Path(args.tmp_image)

    picam = Picamera2()
    # we want to capture as lo res as possible
    config = picam.create_still_configuration(main={"size": (320, 240)})
    picam.configure(config)
    picam.start()
    time.sleep(2.0)

    try:
        while True:
            t0 = time.time()
            rgb = capture_rgb_bytes(picam, tmp_path)
            t1 = time.time()
            upload_frame(spi, rgb, frame_id, args.chunk_pixels, args.retries)
            t2 = time.time()
            print(
                f"Frame {frame_id}: capture={t1-t0:.2f}s upload={t2-t1:.2f}s "
                f"total={t2-t0:.2f}s"
            )
            frame_id = (frame_id + 1) & 0xFF
    finally:
        spi.close()
        picam.stop()


if __name__ == "__main__":
    main()
