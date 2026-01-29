from concurrent import futures
import time
import grpc

import traini_pb2, traini_pb2_grpc

class TrainiServiceServicer(traini_pb2_grpc.TrainiServiceServicer):
    def TranslateHumanToDog(self, request, context):
        # 最小校验：便于确认网关确实把字段传过来了
        if not request.audio_base64 or not request.audio_format:
            return traini_pb2.HumanToDogResponse(
                success=False,
                error_message="audio_base64 or audio_format is empty",
                audio_url="",
            )

        # mock 返回：用一个明显不同于 HTTP mock 的字符串，作为验收标记
        return traini_pb2.HumanToDogResponse(
            success=True,
            error_message="",
            audio_url="mock://dog-audio-url-from-grpc",
        )
def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    traini_pb2_grpc.add_TrainiServiceServicer_to_server(TrainiServiceServicer(), server)

    # 只监听本机：外网不可直接访问
    server.add_insecure_port("127.0.0.1:50051")
    server.start()
    print("gRPC mock server listening on 127.0.0.1:50051")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop(0)

if __name__ == "__main__":
    serve()
