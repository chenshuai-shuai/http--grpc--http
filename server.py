from fastapi import FastAPI, Form
from pydantic import BaseModel
import grpc

import traini_pb2, traini_pb2_grpc

app = FastAPI()

@app.get("/ping")
def ping_get():
    return "ok"

@app.post("/ping")
def ping_post(msg: str = Form(default="")):
    return "ok"

class HumanToDogRequestHTTP(BaseModel):
    audio_base64: str
    audio_format: str

def grpc_translate(audio_base64: str, audio_format: str) -> traini_pb2.HumanToDogResponse:
    target = "127.0.0.1:50051"
    with grpc.insecure_channel(target) as channel:
        stub = traini_pb2_grpc.TrainiServiceStub(channel)
        req = traini_pb2.HumanToDogRequest(audio_base64=audio_base64, audio_format=audio_format)
        return stub.TranslateHumanToDog(req, timeout=10.0)

@app.post("/translate/h2d")
def translate_h2d(req: HumanToDogRequestHTTP):
    try:
        resp = grpc_translate(req.audio_base64, req.audio_format)
        return {
            "success": bool(resp.success),
            "error_message": resp.error_message,
            "audio_url": resp.audio_url,
        }
    except grpc.RpcError as e:
        return {
            "success": False,
            "error_message": f"gRPC error: {e.code().name}: {e.details()}",
            "audio_url": "",
        }
    except Exception as e:
        return {
            "success": False,
            "error_message": f"internal error: {type(e).__name__}: {e}",
            "audio_url": "",
        }
