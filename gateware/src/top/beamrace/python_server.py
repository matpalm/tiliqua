#!/usr/bin/env python3
"""Capture a camera frame and upload it to beamrace over SPI.

Wiring (matches spi_test): EX1 PMOD CS=1, SCK=7, MOSI=9, MISO=8.
Protocol:
- Packet: SOF(0xA5,0x5A), frame_id, pix_off_hi, pix_off_lo, pix_len_hi, pix_len_lo,
  payload (RGB bytes, 3*pix_len), crc_hi, crc_lo.
- Host appends 3 dummy bytes to clock out status.
- Status: 0x11 ACK, 0xE1 length/range error, 0xE2 CRC error.
"""

import argparse
import binascii
import time

from picamera2 import Picamera2
from PIL import Image
import spidev

IMAGE_W = 128
IMAGE_H = 75
CAPTURE_W = 320
CAPTURE_H = 240
N_PIXELS = IMAGE_W * IMAGE_H
SOF0 = 0xA5
SOF1 = 0x5A
ACK = 0x11
ERR_LEN = 0xE1
ERR_CRC = 0xE2
SPI_MODE = 0
STATUS_DUMMY_BYTES = 3
RESAMPLE_BILINEAR = getattr(getattr(Image, "Resampling", Image), "BILINEAR")


# Deterministic ACK variants observed with +/-1 bit alignment.
ACK_SHIFTED = {0x11, 0x22, 0x08}


def decode_status(tail):
    if ACK in tail:
        return ACK
    for b in tail:
        if b in ACK_SHIFTED:
            return ACK
    if ERR_LEN in tail:
        return ERR_LEN
    if ERR_CRC in tail:
        return ERR_CRC
    return tail[-1]


def crc16_ccitt(data):
    return binascii.crc_hqx(data, 0xFFFF)


def capture_rgb_bytes(picam):
    frame = picam.capture_array("main")
    if frame.ndim == 3 and frame.shape[2] >= 3:
        # Picamera2 low-latency path provides BGR-ordered channels here.
        frame = frame[..., [2, 1, 0]]
    img = Image.fromarray(frame, mode="RGB")
    if img.size != (IMAGE_W, IMAGE_H):
        img = img.resize((IMAGE_W, IMAGE_H), RESAMPLE_BILINEAR)
    return img.tobytes()


def packet_for_chunk(frame_id, pix_off, rgb_payload):
    pix_len = len(rgb_payload) // 3
    header = bytes(
        [
            frame_id & 0xFF,
            (pix_off >> 8) & 0xFF,
            pix_off & 0xFF,
            (pix_len >> 8) & 0xFF,
            pix_len & 0xFF,
        ]
    )
    crc = crc16_ccitt(header)
    crc = binascii.crc_hqx(rgb_payload, crc)

    packet = bytearray(2 + len(header) + len(rgb_payload) + 2)
    packet[0] = SOF0
    packet[1] = SOF1
    packet[2:7] = header
    packet[7 : 7 + len(rgb_payload)] = rgb_payload
    packet[-2] = (crc >> 8) & 0xFF
    packet[-1] = crc & 0xFF
    return packet


def upload_frame(
    spi,
    rgb_bytes,
    frame_id,
    chunk_pixels,
    retries,
    retry_backoff_s,
):
    if len(rgb_bytes) != N_PIXELS * 3:
        raise ValueError(f"Expected {N_PIXELS*3} bytes, got {len(rgb_bytes)}")

    rgb_view = memoryview(rgb_bytes)
    status_dummy = [0x00] * STATUS_DUMMY_BYTES
    n_chunks = (N_PIXELS + chunk_pixels - 1) // chunk_pixels

    for chunk_i in range(n_chunks):
        pix_off = chunk_i * chunk_pixels
        pix_len = min(chunk_pixels, N_PIXELS - pix_off)
        payload = rgb_view[pix_off * 3 : (pix_off + pix_len) * 3]
        packet = packet_for_chunk(frame_id, pix_off, payload)
        tx = list(packet) + status_dummy

        ok = False
        for attempt in range(1, retries + 1):
            rx = spi.xfer2(tx)
            tail = rx[-STATUS_DUMMY_BYTES:]
            status = decode_status(tail)
            if status == ACK:
                ok = True
                break
            print(
                f"Chunk {chunk_i+1}/{n_chunks} off={pix_off} len={pix_len} "
                f"attempt={attempt} status=0x{status:02X} tail={tail}"
            )
            if status not in (ERR_LEN, ERR_CRC) and retry_backoff_s > 0:
                time.sleep(retry_backoff_s)

        if not ok:
            raise RuntimeError(
                f"Chunk {chunk_i+1}/{n_chunks} failed after {retries} retries"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hz", type=int, default=1_000_000)
    parser.add_argument("--chunk-pixels", type=int, default=128)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-backoff-ms", type=float, default=0.5)
    args = parser.parse_args()

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = args.hz
    spi.mode = SPI_MODE
    print(
        f"SPI config: mode={SPI_MODE} hz={spi.max_speed_hz} "
        f"dummy={STATUS_DUMMY_BYTES} chunk_pixels={args.chunk_pixels}"
    )

    frame_id = 0

    picam = Picamera2()
    config = picam.create_video_configuration(
        main={"size": (CAPTURE_W, CAPTURE_H), "format": "RGB888"},
        queue=False,
    )
    picam.configure(config)
    picam.start()
    time.sleep(0.3)

    try:
        while True:
            t0 = time.time()
            rgb = capture_rgb_bytes(picam)
            t1 = time.time()
            upload_frame(
                spi,
                rgb,
                frame_id,
                args.chunk_pixels,
                args.retries,
                args.retry_backoff_ms / 1000.0,
            )
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
