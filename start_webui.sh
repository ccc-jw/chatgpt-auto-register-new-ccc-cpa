#!/bin/bash
set -e

cd /Users/ccc/Documents/AI/chatgpt-auto-register-new-ccc-cpa

nohup python3 web_gui.py > webui.log 2>&1 &

echo "WebUI started."
echo "URL: http://127.0.0.1:7778"
echo "Log: webui.log"
