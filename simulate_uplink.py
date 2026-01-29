"""simulate_uplink.py

Simulate hardware uplink (/audio) and playback complete (/downlink/complete).
It polls /downlink/status to follow mode changes and logs state transitions.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name, str(default)) or "").strip())
    except Exception:
        return default


GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
STATUS_URL = f"{GATEWAY_URL}/downlink/status"
COMPLETE_URL = f"{GATEWAY_URL}/downlink/complete"
AUDIO_URL = f"{GATEWAY_URL}/audio"

AUDIO_PATH = Path(os.environ.get("SIM_AUDIO_PATH", "audio_example.pcm")).resolve()
UPLINK_SR = _env_int("SIM_UPLINK_SR", 12500)
UPLINK_FORMAT = os.environ.get("SIM_UPLINK_FORMAT", "pcm_s16le").strip()
UPLINK_STREAM = os.environ.get("SIM_UPLINK_STREAM", "default").strip()
UPLINK_CHUNK_SIZE = _env_int("SIM_UPLINK_CHUNK_SIZE", 1024)
UPLINK_CHUNK_INTERVAL_MS = _env_int("SIM_UPLINK_CHUNK_INTERVAL_MS", 40)

STATUS_POLL_INTERVAL_MS = _env_int("SIM_STATUS_POLL_INTERVAL_MS", 300)
PLAYBACK_DONE_DELAY_SEC = _env_int("SIM_PLAYBACK_DONE_DELAY_SEC", 2)


def _http_get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("GET %s failed: %s", url, exc)
        return None


def _http_post(url: str, data: bytes | None, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, method="POST", data=data or b"")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8") if resp.length else ""
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        return exc.code, body
    except Exception as exc:
        logging.warning("POST %s failed: %s", url, exc)
        return 0, ""


def _read_audio() -> bytes:
    try:
        data = AUDIO_PATH.read_bytes()
        if len(data) % 2 == 1:
            logging.warning("audio length odd, trimming last byte")
            data = data[:-1]
        return data
    except FileNotFoundError:
        logging.error("audio file missing: %s", AUDIO_PATH)
        return b""
    except Exception as exc:
        logging.error("audio read failed: %s", exc)
        return b""


def _iter_chunks(data: bytes, size: int):
    step = max(2, int(size))
    if step % 2 == 1:
        step -= 1
    idx = 0
    while True:
        if not data:
            yield b""
            return
        end = min(len(data), idx + step)
        yield data[idx:end]
        idx = end
        if idx >= len(data):
            idx = 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    audio = _read_audio()
    if not audio:
        return

    chunks = _iter_chunks(audio, UPLINK_CHUNK_SIZE)
    last_mode = None
    downlink_started_at = None

    while True:
        status = _http_get_json(STATUS_URL)
        mode = status.get("mode") if status else None
        stream = status.get("stream") if status else None

        if mode and mode != last_mode:
            logging.info("mode change: %s -> %s (stream=%s)", last_mode, mode, stream)
            last_mode = mode
            if mode == "downlink_playing":
                downlink_started_at = time.time()
            if mode == "uplink":
                downlink_started_at = None

        if mode == "uplink":
            chunk = next(chunks)
            headers = {
                "X-SR": str(UPLINK_SR),
                "X-Format": UPLINK_FORMAT,
                "X-Stream": UPLINK_STREAM,
            }
            status_code, _ = _http_post(AUDIO_URL, chunk, headers=headers)
            if status_code == 204:
                logging.info("uplink sent bytes=%s", len(chunk))
            elif status_code == 409:
                logging.info("uplink blocked by downlink")
            elif status_code:
                logging.warning("uplink unexpected status=%s", status_code)
            time.sleep(max(1, UPLINK_CHUNK_INTERVAL_MS) / 1000.0)
        elif mode == "downlink_playing":
            if downlink_started_at and (time.time() - downlink_started_at) >= max(0, PLAYBACK_DONE_DELAY_SEC):
                headers = {"X-Stream": stream} if stream else None
                status_code, body = _http_post(COMPLETE_URL, b"", headers=headers)
                if status_code == 200:
                    logging.info("downlink complete sent, response=%s", body.strip())
                else:
                    logging.warning("downlink complete failed status=%s body=%s", status_code, body.strip())
                downlink_started_at = None
            time.sleep(max(1, STATUS_POLL_INTERVAL_MS) / 1000.0)
        else:
            time.sleep(max(1, STATUS_POLL_INTERVAL_MS) / 1000.0)


if __name__ == "__main__":
    main()
