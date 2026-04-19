#!/usr/bin/env bash
# Start the Options Portfolio Analyzer backend
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/backend"

echo "📦 Installing dependencies..."
pip install -r requirements.txt -q

echo "🚀 Starting server at http://localhost:8000"
python main.py
