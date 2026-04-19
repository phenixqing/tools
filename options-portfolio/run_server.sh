#!/bin/bash
# Source user profile to pick up exported API keys
source ~/.bashrc 2>/dev/null || true
source ~/.zshrc 2>/dev/null || true

cd "$(dirname "$0")"
exec python3 backend/main.py
