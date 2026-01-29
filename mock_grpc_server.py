"""mock_grpc_server.py

Mock ConversationService gRPC server.
Every interval it streams audio_example.pcm back to the client in chunks,
then sends audio_complete.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent import futures
from pathlib import Path
from typing import Iterable

import grpc

import conversation_pb2 as conversation_pb2
import conversation_pb2_grpc as conversation_pb2_grpc


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name, str(default)) or "").strip())
    except Exception:
        return default


HOST = os.environ.get("MOCK_GRPC_HOST", "0.0.0.0")
PORT = _env_int("MOCK_GRPC_PORT", 50051)
INTERVAL_SEC = _env_int("MOCK_GRPC_INTERVAL_SEC", 10)
CHUNK_SIZE = _env_int("MOCK_GRPC_CHUNK_SIZE", 2048)
CHUNK_INTERVAL_MS = _env_int("MOCK_GRPC_CHUNK_INTERVAL_MS", 20)
AUDIO_PATH = Path(os.environ.get("MOCK_GRPC_AUDIO_PATH", "audio_example.pcm")).resolve()


def _chunk_bytes(data: bytes, size: int) -> Iterable[bytes]:
    step = max(1, int(size))
    for idx in range(0, len(data), step):
        yield data[idx : idx + step]


def _read_audio() -> bytes:
    try:
        return AUDIO_PATH.read_bytes()
    except FileNotFoundError:
        logging.warning("mock audio file missing path=%s", AUDIO_PATH)
        return b""
    except Exception as exc:
        logging.warning("mock audio read failed path=%s err=%s", AUDIO_PATH, exc)
        return b""


class MockConversationService(conversation_pb2_grpc.ConversationServiceServicer):
    def StreamConversation(self, request_iterator, context):
        stop = threading.Event()

        def _consume_requests() -> None:
            try:
                for _ in request_iterator:
                    if stop.is_set():
                        break
            except Exception:
                pass
            finally:
                stop.set()

        threading.Thread(target=_consume_requests, name="grpc-mock-consumer", daemon=True).start()

        next_send = time.time() + max(1, int(INTERVAL_SEC))
        sent_count = 0

        while context.is_active() and not stop.is_set():
            now = time.time()
            wait = next_send - now
            if wait > 0:
                time.sleep(wait)
            if not context.is_active() or stop.is_set():
                break

            data = _read_audio()
            if not data:
                next_send = time.time() + max(1, int(INTERVAL_SEC))
                continue

            total_chunks = 0
            seq = 1
            for chunk in _chunk_bytes(data, CHUNK_SIZE):
                if not context.is_active() or stop.is_set():
                    break
                total_chunks += 1
                yield conversation_pb2.ConversationEvent(
                    audio_output=conversation_pb2.AudioOutput(
                        audio_data=chunk,
                        sequence_number=seq,
                    )
                )
                seq += 1
                if CHUNK_INTERVAL_MS > 0:
                    time.sleep(CHUNK_INTERVAL_MS / 1000)

            if not context.is_active() or stop.is_set():
                break

            yield conversation_pb2.ConversationEvent(
                audio_complete=conversation_pb2.AudioComplete(total_chunks=total_chunks)
            )
            sent_count += 1
            logging.info(
                "mock audio response sent count=%s chunks=%s bytes=%s",
                sent_count,
                total_chunks,
                len(data),
            )
            next_send = time.time() + max(1, int(INTERVAL_SEC))

    def EndConversation(self, request, context):
        return conversation_pb2.SessionSummary(session_id=request.session_id)


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    conversation_pb2_grpc.add_ConversationServiceServicer_to_server(MockConversationService(), server)
    server.add_insecure_port(f"{HOST}:{PORT}")
    server.start()
    logging.info("mock grpc server listening host=%s port=%s", HOST, PORT)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logging.info("mock grpc server shutting down")
        server.stop(0)


if __name__ == "__main__":
    serve()
