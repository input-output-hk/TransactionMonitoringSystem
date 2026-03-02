#!/bin/bash
# Startup script for Transaction Analyzer

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "🚀 Starting Transaction Analyzer..."
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found!"
    echo "Creating from .env.example..."
    cp .env.example .env
    echo "📝 Please edit .env and set your OGMIOS_WS_URL"
    echo ""
fi

# Check if virtual environment exists
if [ ! -d venv ]; then
    echo "⚠️  Virtual environment not found!"
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Installing dependencies..."
    source venv/bin/activate
    pip install -r requirements.txt
    echo ""
fi

# Activate virtual environment
source venv/bin/activate

# Start databases
echo "📦 Starting database containers..."
./scripts/db.sh start

# Wait a bit for databases to be ready
echo ""
echo "⏳ Waiting for databases to initialize..."
sleep 3

# Check if databases need initialization
echo ""
echo "🔍 Checking if databases need initialization..."
if ! python3 -c "
import sys
sys.path.insert(0, 'backend')
from app.db import clickhouse
try:
    clickhouse.init_client()
    clickhouse._client.execute('SELECT 1 FROM transactions LIMIT 1')
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null; then
    echo "📊 Initializing database schemas..."
    cd backend
    python scripts/init_db.py
    cd ..
    echo "✅ Databases initialized"
else
    echo "✅ Databases already initialized"
fi

echo ""
echo "🎯 Starting FastAPI server..."
echo ""
echo "Server will be available at:"
echo "  - Web UI: http://localhost:8000"
echo "  - API Docs: http://localhost:8000/docs"
echo "  - ReDoc: http://localhost:8000/redoc"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

cd backend
python run.py
