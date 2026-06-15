#!/bin/bash
set -e

cd /Users/ccc/Documents/AI/chatgpt-auto-register-new-ccc-cpa

pkill -f "[w]eb_gui.py" 2>/dev/null || true

# macOS may show the interpreter as Python.app, so fall back to the port.
pids=$(lsof -tiTCP:7778 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
  sleep 1
fi

if lsof -i :7778 >/dev/null 2>&1; then
  echo "WebUI may still be running on port 7778:"
  lsof -i :7778
  exit 1
fi

echo "WebUI stopped."
