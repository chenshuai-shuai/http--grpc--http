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
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

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


@app.get("/ping")
async def ping():
    return {"ok": True}


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

    return Response(status_code=204)
