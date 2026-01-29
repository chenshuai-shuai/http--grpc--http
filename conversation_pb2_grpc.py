# -*- coding: utf-8 -*-
# Minimal gRPC stub definitions for ConversationService.
# Generated manually to avoid requiring protoc-gen-grpc_python.
import grpc

import conversation_pb2 as conversation__pb2


class ConversationServiceStub(object):
    """Stub for ConversationService (client-side)."""

    def __init__(self, channel):
        self.StreamConversation = channel.stream_stream(
            "/traini.ConversationService/StreamConversation",
            request_serializer=conversation__pb2.AudioChunk.SerializeToString,
            response_deserializer=conversation__pb2.ConversationEvent.FromString,
        )
        self.EndConversation = channel.unary_unary(
            "/traini.ConversationService/EndConversation",
            request_serializer=conversation__pb2.EndConversationRequest.SerializeToString,
            response_deserializer=conversation__pb2.SessionSummary.FromString,
        )
