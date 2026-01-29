# traini_gateway

HTTP audio gateway that stores incoming audio and forwards PCM16 chunks to a
ConversationService gRPC server (bidirectional stream).

## Requirements
- Python 3.9+
- pip
- protoc (optional if you want to regenerate protobuf code)

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn[standard] grpcio protobuf
```

## Protobuf
The active protocol is `proto/conversation.proto`.

If you need to regenerate `conversation_pb2.py`:
```bash
protoc --proto_path=proto --python_out=. proto/conversation.proto
```

## Run
```bash
GRPC_SERVER_HOST=<your-grpc-host> GRPC_SERVER_PORT=50051 GRPC_ENABLED=1 \
uvicorn audio_store_server:app --host 0.0.0.0 --port 8080
```

## Endpoints
- `GET /ping` -> health check
- `POST /audio` -> upload raw audio bytes (PCM16 or mulaw_u8)
- `GET /audio/{stream}/info` -> stream info and metadata
- `GET /audio/{stream}/wav` -> download WAV
- `POST /audio/{stream}/reset` -> reset stream and close gRPC session

## Headers for /audio
- `X-SR`: sample rate (e.g. 16000)
- `X-Seq`: optional sequence number
- `X-Format`: `pcm_s16le` or `mulaw_u8` (default `pcm_s16le`)
- `X-Stream`: stream id (default `default`)

## Env Vars
- `GRPC_SERVER_HOST` (required; no default)
- `GRPC_SERVER_PORT` (default `50051`)
- `GRPC_ENABLED` (default `1`)
- `GRPC_AUDIO_ENCODING` (default `pcm16`)
- `GRPC_AUDIO_CHANNELS` (default `1`)
- `GRPC_AUDIO_BIT_DEPTH` (default `16`)
- `GRPC_SESSION_IDLE_SEC` (default `120`)
