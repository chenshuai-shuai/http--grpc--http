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
GRPC_SERVER_HOST=35.168.3.114 GRPC_SERVER_PORT=50051 GRPC_ENABLED=1 \
uvicorn audio_store_server:app --host 0.0.0.0 --port 8080
```

## Endpoints
- `GET /ping` -> health check
- `POST /audio` -> upload raw audio bytes (PCM16 or mulaw_u8)
- `GET /downlink/status` -> current uplink/downlink status
- `POST /downlink/complete` -> simulate hardware playback done (resume uplink)
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

## Mock gRPC server (local)
```bash
python mock_grpc_server.py
```
Then run the gateway with `GRPC_SERVER_HOST=127.0.0.1` and `GRPC_SERVER_PORT=50051`.

Env:
- `MOCK_GRPC_HOST` (default `0.0.0.0`)
- `MOCK_GRPC_PORT` (default `50051`)
- `MOCK_GRPC_INTERVAL_SEC` (default `10`)
- `MOCK_GRPC_CHUNK_SIZE` (default `2048`)
- `MOCK_GRPC_CHUNK_INTERVAL_MS` (default `20`)
- `MOCK_GRPC_AUDIO_PATH` (default `audio_example.pcm`)

## Simulate hardware (uplink + playback done)
```bash
python simulate_uplink.py
```

Env:
- `GATEWAY_URL` (default `http://127.0.0.1:8080`)
- `SIM_AUDIO_PATH` (default `audio_example.pcm`)
- `SIM_UPLINK_SR` (default `12500`)
- `SIM_UPLINK_FORMAT` (default `pcm_s16le`)
- `SIM_UPLINK_STREAM` (default `default`)
- `SIM_UPLINK_CHUNK_SIZE` (default `1024`)
- `SIM_UPLINK_CHUNK_INTERVAL_MS` (default `40`)
- `SIM_STATUS_POLL_INTERVAL_MS` (default `300`)
- `SIM_PLAYBACK_DONE_DELAY_SEC` (default `2`)

## Quick verify (local)
1) Start mock gRPC:
```bash
python mock_grpc_server.py
```
2) Start gateway (point to mock):
```bash
GRPC_SERVER_HOST=127.0.0.1 GRPC_SERVER_PORT=50051 GRPC_ENABLED=1 \
uvicorn audio_store_server:app --host 0.0.0.0 --port 8080
```
3) Start simulator:
```bash
python simulate_uplink.py
```
You should see logs of mode transitions and `/audio` uploads being blocked during downlink.

## Downlink (gateway -> hardware, simulated)
When gRPC sends `audio_output`, the gateway switches to downlink, buffers chunks,
and after `audio_complete` it prepares the PCM for hardware playback. It then
waits for `/downlink/complete` to resume uplink.

Env:
- `DOWNLINK_ENABLED` (default `1`)
- `DOWNLINK_SAMPLE_RATE` (default `12500`)
- `DOWNLINK_CHANNELS` (default `1`)
- `DOWNLINK_BITS` (default `16`)
- `DOWNLINK_HW_CHUNK_SIZE` (default `2048`)
- `DOWNLINK_PACKET_MAGIC` (default `ATGW`, 4 bytes)
- `DOWNLINK_STORE_DIR` (default empty -> `audio_store/<stream>/downlink_last.pcm`)
- `DOWNLINK_STORE_LAST` (default `1`)

Packet header (24 bytes, little-endian):
`magic(4s) + version(u8=1) + msg_type(u8) + flags(u16) + sample_rate(u32) + channels(u8) + bits(u8) + reserved(u16=0) + seq(u32) + payload_len(u32)`

- `msg_type`: 1=control, 2=audio
- `flags`: bit0=first, bit1=last
- control payload: 1 byte command (1=PLAYBACK_START, 2=PLAYBACK_DONE, 3=UPLINK_RESUME)
