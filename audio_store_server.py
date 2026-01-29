"""audio_store_server.py

FastAPI 服务：接收 nRF52840 -> ESP-AT -> TCP 的 HTTP POST /audio 二进制音频数据。

支持格式：
- pcm_s16le: PCM 16-bit little endian, mono
- mulaw_u8 : G.711 μ-law 8-bit, mono（服务器会解码成 PCM16 写入 WAV）

Header:
    X-SR: 采样率(Hz)，例如 6250（mulaw_u8 时就是压缩后的 sr）
    X-Seq: 段序号(可选)
    X-Format: pcm_s16le / mulaw_u8(可选，默认 pcm_s16le)
    X-Stream: 流 ID(可选，默认 default)

- 服务器将数据持续追加写入：audio_store/<stream>/stream.wav
- /audio/{stream}/wav 下载 wav
- POST /audio 响应为 204 No Content
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

try:
    import grpc
    import conversation_pb2 as conversation_pb2
    import conversation_pb2_grpc as conversation_pb2_grpc

    GRPC_AVAILABLE = True
except Exception as exc:
    GRPC_AVAILABLE = False
    conversation_pb2 = None
    conversation_pb2_grpc = None
    logging.warning("gRPC client disabled: %s", exc)

app = FastAPI(title="Audio Store Server", version="2.1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DATA_DIR = Path(os.environ.get("AUDIO_STORE_DIR", "audio_store")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SR = 12500
DEFAULT_STREAM = "default"
WAV_HEADER_SIZE = 44


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name, str(default)) or "").strip())
    except Exception:
        return default


GRPC_SERVER_HOST = os.environ.get("GRPC_SERVER_HOST", "35.168.3.114")
GRPC_SERVER_PORT = _env_int("GRPC_SERVER_PORT", 50051)
GRPC_ENABLED = os.environ.get("GRPC_ENABLED", "1").strip() != "0"
GRPC_AUDIO_ENCODING = os.environ.get("GRPC_AUDIO_ENCODING", "pcm16")
GRPC_AUDIO_CHANNELS = _env_int("GRPC_AUDIO_CHANNELS", 1)
GRPC_AUDIO_BIT_DEPTH = _env_int("GRPC_AUDIO_BIT_DEPTH", 16)
GRPC_SESSION_IDLE_SEC = _env_int("GRPC_SESSION_IDLE_SEC", 120)

DOWNLINK_ENABLED = os.environ.get("DOWNLINK_ENABLED", "1").strip() != "0"
DOWNLINK_SAMPLE_RATE = _env_int("DOWNLINK_SAMPLE_RATE", DEFAULT_SR)
DOWNLINK_CHANNELS = _env_int("DOWNLINK_CHANNELS", 1)
DOWNLINK_BITS = _env_int("DOWNLINK_BITS", 16)
DOWNLINK_HW_CHUNK_SIZE = _env_int("DOWNLINK_HW_CHUNK_SIZE", 2048)
DOWNLINK_PACKET_MAGIC = os.environ.get("DOWNLINK_PACKET_MAGIC", "ATGW")
DOWNLINK_STORE_DIR = os.environ.get("DOWNLINK_STORE_DIR", "").strip()
DOWNLINK_STORE_LAST = os.environ.get("DOWNLINK_STORE_LAST", "1").strip() != "0"

DOWNLINK_PACKET_VERSION = 1

MSG_CONTROL = 1
MSG_AUDIO = 2

FLAG_FIRST = 0x01
FLAG_LAST = 0x02

CTRL_PLAYBACK_START = 1
CTRL_PLAYBACK_DONE = 2
CTRL_UPLINK_RESUME = 3


def _downlink_magic_bytes() -> bytes:
    raw = (DOWNLINK_PACKET_MAGIC or "ATGW").encode("ascii", "ignore")[:4]
    return raw.ljust(4, b"_")


DOWNLINK_MAGIC_BYTES = _downlink_magic_bytes()
DOWNLINK_HEADER = struct.Struct("<4sBBHIBBHII")

_CONTROL_SEQ = 0
_CONTROL_SEQ_LOCK = threading.Lock()


def _next_control_seq() -> int:
    global _CONTROL_SEQ
    with _CONTROL_SEQ_LOCK:
        _CONTROL_SEQ += 1
        return _CONTROL_SEQ


def _build_packet_header(
    msg_type: int,
    flags: int,
    seq: int,
    payload_len: int,
    sr: int = 0,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    return DOWNLINK_HEADER.pack(
        DOWNLINK_MAGIC_BYTES,
        DOWNLINK_PACKET_VERSION,
        msg_type,
        max(0, int(flags)) & 0xFFFF,
        max(0, int(sr)),
        max(0, min(255, int(channels))),
        max(0, min(255, int(bits))),
        0,
        max(0, int(seq)),
        max(0, int(payload_len)),
    )


def _build_control_packet(cmd: int, seq: int) -> bytes:
    payload = bytes([max(0, min(255, int(cmd)))])
    header = _build_packet_header(
        MSG_CONTROL,
        0,
        seq,
        len(payload),
        sr=0,
        channels=0,
        bits=0,
    )
    return header + payload


def _build_audio_packet(
    pcm: bytes,
    seq: int,
    first: bool,
    last: bool,
    sr: int,
    channels: int,
    bits: int,
) -> bytes:
    flags = 0
    if first:
        flags |= FLAG_FIRST
    if last:
        flags |= FLAG_LAST
    header = _build_packet_header(
        MSG_AUDIO,
        flags,
        seq,
        len(pcm),
        sr=sr,
        channels=channels,
        bits=bits,
    )
    return header + pcm


def _downlink_pcm_path(stream_id: str) -> Path:
    if DOWNLINK_STORE_DIR:
        out_dir = Path(DOWNLINK_STORE_DIR).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{stream_id}_downlink_last.pcm"
    return _ensure_stream_dir(stream_id) / "downlink_last.pcm"


def _send_hw_control(stream_id: str, cmd: int) -> None:
    seq = _next_control_seq()
    packet = _build_control_packet(cmd, seq)
    logging.info("hw control queued stream=%s cmd=%s seq=%s bytes=%s", stream_id, cmd, seq, len(packet))


def _send_hw_audio(stream_id: str, pcm: bytes, sr: int, channels: int, bits: int) -> None:
    if not pcm:
        return
    chunk_size = max(1, int(DOWNLINK_HW_CHUNK_SIZE))
    total_packets = 0
    total_chunks = (len(pcm) + chunk_size - 1) // chunk_size
    for idx in range(total_chunks):
        start = idx * chunk_size
        end = min(len(pcm), start + chunk_size)
        _build_audio_packet(
            pcm[start:end],
            seq=idx + 1,
            first=(idx == 0),
            last=(idx == total_chunks - 1),
            sr=sr,
            channels=channels,
            bits=bits,
        )
        total_packets += 1
    if DOWNLINK_STORE_LAST:
        try:
            _downlink_pcm_path(stream_id).write_bytes(pcm)
        except Exception as exc:
            logging.warning("downlink pcm store failed stream=%s err=%s", stream_id, exc)
    logging.info(
        "hw audio queued stream=%s bytes=%s packets=%s",
        stream_id,
        len(pcm),
        total_packets,
    )


class _DownlinkState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = "uplink"
        self._stream_id: Optional[str] = None
        self._buffer = bytearray()
        self._chunks = 0
        self._last_start = None
        self._last_complete = None
        self._last_bytes = 0
        self._last_total_chunks = 0

    def mode(self) -> str:
        with self._lock:
            return self._mode

    def is_uplink(self) -> bool:
        with self._lock:
            return self._mode == "uplink"

    def start_if_needed(self, stream_id: str) -> bool:
        with self._lock:
            if self._mode != "uplink":
                return False
            self._mode = "downlink_collecting"
            self._stream_id = stream_id
            self._buffer.clear()
            self._chunks = 0
            self._last_start = time.time()
            logging.info("downlink state uplink -> downlink_collecting stream=%s", stream_id)
            return True

    def append(self, stream_id: str, data: bytes) -> bool:
        with self._lock:
            if self._mode != "downlink_collecting" or self._stream_id != stream_id:
                return False
            self._buffer.extend(data)
            self._chunks += 1
            return True

    def finish(self, stream_id: str, total_chunks: int) -> Optional[bytes]:
        with self._lock:
            if self._mode != "downlink_collecting" or self._stream_id != stream_id:
                return None
            payload = bytes(self._buffer)
            self._buffer.clear()
            self._mode = "downlink_playing"
            self._last_complete = time.time()
            self._last_bytes = len(payload)
            self._last_total_chunks = total_chunks
            logging.info(
                "downlink state downlink_collecting -> downlink_playing stream=%s bytes=%s chunks=%s",
                stream_id,
                len(payload),
                total_chunks,
            )
            return payload

    def playback_done(self, stream_id: Optional[str]) -> bool:
        with self._lock:
            if self._mode != "downlink_playing":
                return False
            if stream_id and self._stream_id != stream_id:
                return False
            self._mode = "uplink"
            self._stream_id = None
            self._buffer.clear()
            self._chunks = 0
            logging.info("downlink state downlink_playing -> uplink stream=%s", stream_id or "current")
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "stream": self._stream_id,
                "buffer_bytes": len(self._buffer),
                "chunks_received": self._chunks,
                "last_start": self._last_start,
                "last_complete": self._last_complete,
                "last_bytes": self._last_bytes,
                "last_total_chunks": self._last_total_chunks,
            }


def _sanitize_stream_id(raw: str) -> str:
    raw = (raw or DEFAULT_STREAM).strip()
    if not raw:
        return DEFAULT_STREAM
    raw = re.sub(r"[^a-zA-Z0-9_\-]", "_", raw)
    return raw[:64] or DEFAULT_STREAM


def _wav_header(sr: int, channels: int = 1, bits_per_sample: int = 16, data_bytes: int = 0) -> bytes:
    if bits_per_sample not in (8, 16, 24, 32):
        raise ValueError("bits_per_sample invalid")
    if channels < 1:
        raise ValueError("channels invalid")
    byte_rate = sr * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)

    riff_size = 36 + data_bytes

    def le_u32(x: int) -> bytes:
        return int(x).to_bytes(4, "little", signed=False)

    def le_u16(x: int) -> bytes:
        return int(x).to_bytes(2, "little", signed=False)

    return b"".join(
        [
            b"RIFF",
            le_u32(riff_size),
            b"WAVE",
            b"fmt ",
            le_u32(16),
            le_u16(1),
            le_u16(channels),
            le_u32(sr),
            le_u32(byte_rate),
            le_u16(block_align),
            le_u16(bits_per_sample),
            b"data",
            le_u32(data_bytes),
        ]
    )


def _ensure_stream_dir(stream_id: str) -> Path:
    d = DATA_DIR / stream_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_wav(path: Path, sr: int, channels: int = 1, bits: int = 16) -> None:
    if not path.exists():
        path.write_bytes(_wav_header(sr, channels, bits, 0))


def _append_wav(path: Path, pcm: bytes, sr: int, channels: int = 1, bits: int = 16) -> int:
    _ensure_wav(path, sr, channels, bits)

    with path.open("r+b") as f:
        f.seek(0, os.SEEK_END)
        f.write(pcm)
        file_size = f.tell()
        data_bytes = max(0, file_size - WAV_HEADER_SIZE)

        f.seek(4)
        f.write(int(36 + data_bytes).to_bytes(4, "little", signed=False))
        f.seek(40)
        f.write(int(data_bytes).to_bytes(4, "little", signed=False))

    return data_bytes


def _write_meta(stream_dir: Path, sr: int, fmt: str) -> None:
    meta_path = stream_dir / "meta.json"
    meta = {
        "sr": sr,
        "format": fmt,
        "channels": 1,
        "bits": 16,  # wav 统一写成 PCM16
        "updated_at": int(time.time()),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ===== μ-law 解码：mulaw_u8 -> pcm_s16le =====
def _mulaw_to_pcm16_one(mu: int) -> int:
    mu = (~mu) & 0xFF
    sign = mu & 0x80
    exponent = (mu >> 4) & 0x07
    mantissa = mu & 0x0F

    # 还原幅度（与编码的 BIAS=0x84 对应）
    magnitude = ((mantissa << 3) + 0x84) << exponent
    sample = magnitude - 0x84
    if sign:
        sample = -sample
    # clamp to int16
    if sample > 32767:
        sample = 32767
    if sample < -32768:
        sample = -32768
    return sample


def _mulaw_u8_to_pcm_s16le(data: bytes) -> bytes:
    # 每个字节 -> int16 little endian
    out = bytearray(len(data) * 2)
    j = 0
    for b in data:
        s = _mulaw_to_pcm16_one(b)
        out[j] = s & 0xFF
        out[j + 1] = (s >> 8) & 0xFF
        j += 2
    return bytes(out)


class _GrpcConversationSession:
    def __init__(self, stream_id: str, stub: "conversation_pb2_grpc.ConversationServiceStub"):
        self.stream_id = stream_id
        self._stub = stub
        self._queue: "queue.Queue[Optional[conversation_pb2.AudioChunk]]" = queue.Queue()
        self._lock = threading.Lock()
        self._seq_counter = 0
        self._last_activity = time.time()
        self._closing = False
        self._closed = False
        self._broken = False
        self._thread = threading.Thread(target=self._run, name=f"grpc-stream-{stream_id}", daemon=True)
        self._thread.start()

    def _request_iter(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item

    def _handle_event(self, event: "conversation_pb2.ConversationEvent") -> None:
        kind = event.WhichOneof("event")
        if kind == "audio_output":
            audio = event.audio_output
            logging.info(
                "grpc audio_output stream=%s seq=%s bytes=%s",
                self.stream_id,
                audio.sequence_number,
                len(audio.audio_data),
            )
            if DOWNLINK_ENABLED:
                started = DOWNLINK_STATE.start_if_needed(self.stream_id)
                if started:
                    _send_hw_control(self.stream_id, CTRL_PLAYBACK_START)
                accepted = DOWNLINK_STATE.append(self.stream_id, audio.audio_data)
                if not accepted:
                    logging.info(
                        "downlink drop audio_output stream=%s mode=%s",
                        self.stream_id,
                        DOWNLINK_STATE.mode(),
                    )
        elif kind == "audio_complete":
            logging.info(
                "grpc audio_complete stream=%s total_chunks=%s",
                self.stream_id,
                event.audio_complete.total_chunks,
            )
            if DOWNLINK_ENABLED:
                payload = DOWNLINK_STATE.finish(self.stream_id, event.audio_complete.total_chunks)
                if payload is None:
                    logging.info(
                        "downlink ignore audio_complete stream=%s mode=%s",
                        self.stream_id,
                        DOWNLINK_STATE.mode(),
                    )
                else:
                    _send_hw_audio(
                        self.stream_id,
                        payload,
                        DOWNLINK_SAMPLE_RATE,
                        DOWNLINK_CHANNELS,
                        DOWNLINK_BITS,
                    )
        elif kind == "error":
            err = event.error
            logging.warning(
                "grpc error stream=%s code=%s message=%s",
                self.stream_id,
                err.code,
                err.message,
            )
        else:
            logging.info("grpc event stream=%s type=%s", self.stream_id, kind)

    def _run(self) -> None:
        try:
            responses = self._stub.StreamConversation(self._request_iter())
            for event in responses:
                self._handle_event(event)
        except grpc.RpcError as exc:
            logging.error(
                "grpc stream error stream=%s code=%s detail=%s",
                self.stream_id,
                exc.code().name if exc.code() else "UNKNOWN",
                exc.details() if hasattr(exc, "details") else str(exc),
            )
        except Exception as exc:
            logging.exception("grpc stream unexpected error stream=%s: %s", self.stream_id, exc)
        finally:
            self._closed = True
            if not self._closing:
                self._broken = True

    def next_seq(self) -> int:
        with self._lock:
            self._seq_counter += 1
            return self._seq_counter

    def send_chunk(self, chunk: "conversation_pb2.AudioChunk") -> bool:
        if self._closed:
            return False
        self._last_activity = time.time()
        self._queue.put(chunk)
        return True

    def close(self) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        self._queue.put(None)

    def is_dead(self) -> bool:
        return self._closed or self._broken

    def is_idle(self, now: float, idle_sec: int) -> bool:
        if idle_sec <= 0:
            return False
        return (now - self._last_activity) > idle_sec


class _GrpcConversationManager:
    def __init__(self):
        self._enabled = bool(GRPC_AVAILABLE and GRPC_ENABLED and GRPC_SERVER_HOST and GRPC_SERVER_PORT)
        self._lock = threading.Lock()
        self._sessions: dict[str, _GrpcConversationSession] = {}
        self._channel = None
        self._stub = None
        if self._enabled:
            target = f"{GRPC_SERVER_HOST}:{GRPC_SERVER_PORT}"
            self._channel = grpc.insecure_channel(target)
            self._stub = conversation_pb2_grpc.ConversationServiceStub(self._channel)
            logging.info("grpc enabled target=%s", target)
        else:
            logging.info("grpc disabled enabled=%s available=%s", GRPC_ENABLED, GRPC_AVAILABLE)

    def _cleanup_idle_locked(self, now: float) -> None:
        if GRPC_SESSION_IDLE_SEC <= 0:
            return
        stale = [sid for sid, sess in self._sessions.items() if sess.is_idle(now, GRPC_SESSION_IDLE_SEC)]
        for sid in stale:
            sess = self._sessions.pop(sid, None)
            if sess:
                sess.close()

    def _get_or_create(self, stream_id: str) -> "_GrpcConversationSession":
        with self._lock:
            now = time.time()
            self._cleanup_idle_locked(now)
            sess = self._sessions.get(stream_id)
            if sess is None or sess.is_dead():
                sess = _GrpcConversationSession(stream_id, self._stub)
                self._sessions[stream_id] = sess
            return sess

    def send_audio(self, stream_id: str, pcm16: bytes, sr: int, seq: Optional[int]) -> None:
        if not self._enabled:
            return
        sess = self._get_or_create(stream_id)
        seq_val = seq if seq is not None else sess.next_seq()
        now = time.time()
        timestamp = conversation_pb2.Timestamp(
            seconds=int(now),
            nanos=int((now - int(now)) * 1_000_000_000),
        )
        fmt = conversation_pb2.AudioFormat(
            sample_rate=sr,
            channels=GRPC_AUDIO_CHANNELS,
            bit_depth=GRPC_AUDIO_BIT_DEPTH,
            encoding=GRPC_AUDIO_ENCODING,
        )
        chunk = conversation_pb2.AudioChunk(
            audio_data=pcm16,
            format=fmt,
            sequence_number=seq_val,
            timestamp=timestamp,
        )
        if not sess.send_chunk(chunk):
            with self._lock:
                self._sessions.pop(stream_id, None)
            sess = self._get_or_create(stream_id)
            sess.send_chunk(chunk)

    def close_stream(self, stream_id: str) -> None:
        with self._lock:
            sess = self._sessions.pop(stream_id, None)
        if sess:
            sess.close()


GRPC_MANAGER = _GrpcConversationManager()
DOWNLINK_STATE = _DownlinkState()


@app.get("/ping")
async def ping():
    return {"ok": True}


@app.get("/downlink/status")
async def downlink_status():
    return DOWNLINK_STATE.snapshot()


@app.post("/downlink/complete")
async def downlink_complete(
    stream_id: Optional[str] = None,
    x_stream: Optional[str] = Header(None, alias="X-Stream"),
):
    raw = stream_id or x_stream
    sid = _sanitize_stream_id(raw) if raw else None
    if DOWNLINK_STATE.playback_done(sid):
        logging.info("downlink complete stream=%s -> uplink", sid or "current")
        return JSONResponse({"ok": True, "stream": sid, "mode": DOWNLINK_STATE.mode()})
    raise HTTPException(status_code=409, detail="downlink not active")


@app.post("/audio")
async def upload_audio(
    request: Request,
    x_sr: Optional[str] = Header(None, alias="X-SR"),
    x_seq: Optional[str] = Header(None, alias="X-Seq"),
    x_format: Optional[str] = Header(None, alias="X-Format"),
    x_stream: Optional[str] = Header(None, alias="X-Stream"),
):
    try:
        body = await request.body()
    except ClientDisconnect:
        logging.warning("client disconnected mid-upload")
        return Response(status_code=204)

    if DOWNLINK_ENABLED and not DOWNLINK_STATE.is_uplink():
        logging.info("uplink blocked: downlink active")
        raise HTTPException(status_code=409, detail="downlink active")

    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    fmt = (x_format or "pcm_s16le").strip().lower()

    try:
        sr = int((x_sr or str(DEFAULT_SR)).strip())
    except Exception:
        raise HTTPException(status_code=400, detail="bad X-SR")

    if sr <= 0 or sr > 200000:
        raise HTTPException(status_code=400, detail="X-SR out of range")

    # 统一转成 pcm_s16le 写 wav
    if fmt == "pcm_s16le":
        if (len(body) % 2) != 0:
            raise HTTPException(status_code=400, detail="pcm_s16le body length must be even")
        pcm16 = body
        store_fmt = "pcm_s16le"
    elif fmt == "mulaw_u8":
        # mulaw 每字节一个采样点
        pcm16 = _mulaw_u8_to_pcm_s16le(body)
        store_fmt = "mulaw_u8->pcm_s16le"
    else:
        raise HTTPException(status_code=415, detail=f"unsupported format: {fmt}")

    stream_id = _sanitize_stream_id(x_stream or DEFAULT_STREAM)
    stream_dir = _ensure_stream_dir(stream_id)

    wav_path = stream_dir / "stream.wav"
    data_bytes = _append_wav(wav_path, pcm16, sr, channels=1, bits=16)
    _write_meta(stream_dir, sr, store_fmt)

    try:
        seq_val = int(x_seq) if x_seq is not None else None
    except Exception:
        seq_val = None

    seglog = {
        "ts": int(time.time()),
        "seq": seq_val,
        "bytes_in": len(body),
        "bytes_pcm16": len(pcm16),
        "sr": sr,
        "format": fmt,
        "stream": stream_id,
        "total_data_bytes": data_bytes,
    }
    with (stream_dir / "segments.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(seglog, ensure_ascii=False) + "\n")

    try:
        GRPC_MANAGER.send_audio(stream_id, pcm16, sr, seq_val)
    except Exception:
        logging.exception("grpc send failed stream=%s", stream_id)

    return Response(status_code=204)


@app.get("/audio/{stream_id}/info")
async def stream_info(stream_id: str):
    sid = _sanitize_stream_id(stream_id)
    d = _ensure_stream_dir(sid)
    wav_path = d / "stream.wav"
    meta_path = d / "meta.json"

    meta = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None

    if not wav_path.exists():
        return JSONResponse({"stream": sid, "exists": False, "meta": meta}, status_code=200)

    size = wav_path.stat().st_size
    data_bytes = max(0, size - WAV_HEADER_SIZE)
    info = {
        "stream": sid,
        "exists": True,
        "wav_bytes": size,
        "data_bytes": data_bytes,
        "meta": meta,
    }
    return JSONResponse(info)


@app.get("/audio/{stream_id}/wav")
async def download_wav(stream_id: str):
    sid = _sanitize_stream_id(stream_id)
    wav_path = (DATA_DIR / sid / "stream.wav").resolve()
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="wav not found")

    return FileResponse(
        path=str(wav_path),
        media_type="audio/wav",
        filename=f"{sid}.wav",
    )


@app.post("/audio/{stream_id}/reset")
async def reset_stream(stream_id: str):
    sid = _sanitize_stream_id(stream_id)
    d = _ensure_stream_dir(sid)

    for name in ["stream.wav", "segments.jsonl", "meta.json"]:
        p = d / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    try:
        GRPC_MANAGER.close_stream(sid)
    except Exception:
        logging.exception("grpc close failed stream=%s", sid)

    return Response(status_code=204)
