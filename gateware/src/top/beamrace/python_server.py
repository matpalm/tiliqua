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
RESAMPLE_BILINEAR = getattr(getattr(Image, "Resampling", Image), "BILINEAR")


def _shift_variants(byte, max_shift=1):
    # Only accept small bit-slip signatures; broad shifts create false positives
    # on idle lines (for example 0x00) and can mask real link failures.
    vals = {byte}
    for n in range(1, max_shift + 1):
        vals.add((byte << n) & 0xFF)
        vals.add((byte >> n) & 0xFF)
    vals.discard(0x00)
    vals.discard(0xFF)
    return vals


ACK_SHIFTED = _shift_variants(ACK)
ERR_LEN_SHIFTED = _shift_variants(ERR_LEN)
ERR_CRC_SHIFTED = _shift_variants(ERR_CRC)


def decode_status(tail, profiled_ack_byte=None):
    # Prefer exact codes first.
    if ACK in tail:
        return ACK, "exact", ACK
    if profiled_ack_byte is not None and profiled_ack_byte in tail:
        return ACK, f"profiled(0x{profiled_ack_byte:02X})", profiled_ack_byte
    if ERR_LEN in tail:
        return ERR_LEN, "exact", ERR_LEN
    if ERR_CRC in tail:
        return ERR_CRC, "exact", ERR_CRC

    # Tolerate bit-shifted readback at high-speed/asynchronous boundaries.
    for b in tail:
        if b in ACK_SHIFTED:
            return ACK, f"shifted(0x{b:02X})", b
        if b in ERR_LEN_SHIFTED:
            return ERR_LEN, f"shifted(0x{b:02X})", b
        if b in ERR_CRC_SHIFTED:
            return ERR_CRC, f"shifted(0x{b:02X})", b

    return tail[-1], "unknown", tail[-1]


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
    status_dummy_bytes,
    retry_backoff_s,
):
    # TODO: maybe better to do per plane?
    if len(rgb_bytes) != N_PIXELS * 3:
        raise ValueError(f"Expected {N_PIXELS*3} bytes, got {len(rgb_bytes)}")

    rgb_view = memoryview(rgb_bytes)
    status_dummy = [0x00] * status_dummy_bytes
    n_chunks = (N_PIXELS + chunk_pixels - 1) // chunk_pixels
    shifted_ack_chunks = 0
    shifted_ack_kinds = {}
    profiled_ack_byte = None
    profiled_ack_announced = False
    for chunk_i in range(n_chunks):
        pix_off = chunk_i * chunk_pixels
        pix_len = min(chunk_pixels, N_PIXELS - pix_off)
        payload = rgb_view[pix_off * 3 : (pix_off + pix_len) * 3]
        packet = packet_for_chunk(frame_id, pix_off, payload)
        tx = list(packet) + status_dummy

        ok = False
        saw_shifted = False
        for attempt in range(1, retries + 1):
            rx = spi.xfer2(tx)
            tail = rx[-status_dummy_bytes:]
            status, status_kind, status_byte = decode_status(tail, profiled_ack_byte)
            if status == ACK:
                ok = True
                just_profiled = False
                if status_kind.startswith("shifted(") and profiled_ack_byte is None:
                    profiled_ack_byte = status_byte
                    just_profiled = True
                is_profiled = (
                    profiled_ack_byte is not None
                    and status_kind == f"profiled(0x{profiled_ack_byte:02X})"
                )
                if status_kind != "exact" and not is_profiled and not just_profiled:
                    saw_shifted = True
                break
            print(
                f"Chunk {chunk_i+1}/{n_chunks} off={pix_off} len={pix_len} "
                f"attempt={attempt} status=0x{status:02X} {status_kind} tail={tail}"
            )
            if status not in (ERR_LEN, ERR_CRC) and retry_backoff_s > 0:
                time.sleep(retry_backoff_s)

        if not ok:
            raise RuntimeError(
                f"Chunk {chunk_i+1}/{n_chunks} failed after {retries} retries"
            )

        # if chunk_i % 16 == 0 or chunk_i == n_chunks - 1:
        #     print(f"Uploaded {chunk_i+1}/{n_chunks} chunks")
        if profiled_ack_byte is not None and not profiled_ack_announced:
            print(
                f"Using profiled ACK signature 0x{profiled_ack_byte:02X} "
                "for this frame (deterministic slave TX bit alignment)."
            )
            profiled_ack_announced = True
        if saw_shifted:
            shifted_ack_chunks += 1
            shifted_ack_kinds[status_kind] = shifted_ack_kinds.get(status_kind, 0) + 1

    if shifted_ack_chunks:
        pct = 100.0 * shifted_ack_chunks / n_chunks
        if shifted_ack_chunks == n_chunks and len(shifted_ack_kinds) == 1:
            shifted_sig = next(iter(shifted_ack_kinds))
            print(
                f"Shifted ACK on {shifted_ack_chunks}/{n_chunks} chunks ({pct:.1f}%), "
                f"consistently {shifted_sig}. This is often a fixed SPI phase issue "
                "instead of random link noise."
            )
            print("Try toggling --spi-mode between 0 and 1 to remove the bit shift.")
        else:
            print(
                f"Shifted ACK on {shifted_ack_chunks}/{n_chunks} chunks ({pct:.1f}%). "
                "Link is working but timing margin is low."
            )
    elif profiled_ack_byte is not None:
        print(
            f"ACK signature stable at 0x{profiled_ack_byte:02X} for all chunks. "
            "No intermittent timing errors detected."
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hz", type=int, default=1_000_000)
    parser.add_argument("--spi-mode", type=int, default=0, choices=[0, 1, 2, 3])
    parser.add_argument("--chunk-pixels", type=int, default=128)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--status-dummy-bytes", type=int, default=2)
    parser.add_argument("--retry-backoff-ms", type=float, default=0.5)
    args = parser.parse_args()

    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = args.hz
    spi.mode = args.spi_mode
    print(
        f"SPI config: mode={spi.mode} hz={spi.max_speed_hz} "
        f"dummy={args.status_dummy_bytes} chunk_pixels={args.chunk_pixels}"
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
                args.status_dummy_bytes,
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
