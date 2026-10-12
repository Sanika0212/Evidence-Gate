#!/usr/bin/env bash
# Regenerate the Python gRPC stubs (and type stubs) from proto/. CI fails if they drift.
set -euo pipefail
cd "$(dirname "$0")/.."
out=src/evidencegate/v1
rm -f "$out"/*_pb2*.py "$out"/*_pb2*.pyi
python -m grpc_tools.protoc -I proto \
  --python_out=src --grpc_python_out=src --mypy_out=src --mypy_grpc_out=src \
  proto/evidencegate/v1/*.proto
touch "$out/__init__.py"
