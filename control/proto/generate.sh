#!/bin/sh
# Regenerate src/chatuses/adapters/stream_list_pb2.py (+ .pyi) from
# stream_list.proto, with the protoc bundled in grpcio-tools (a dev
# dependency). Run from control/: proto/generate.sh
set -eu
uv run python -m grpc_tools.protoc -Iproto --python_out=src/chatuses/adapters --pyi_out=src/chatuses/adapters proto/stream_list.proto
echo "generated src/chatuses/adapters/stream_list_pb2.py and .pyi"
