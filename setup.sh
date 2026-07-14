#!/bin/bash
# Setup script for Cardano Transaction Monitor

set -e

echo "🔷 Setting up Cardano Transaction Monitor..."

# Check uv is available (manages the .venv and Python 3.13 toolchain)
if ! command -v uv >/dev/null 2>&1; then
    echo "❌ uv is required (https://docs.astral.sh/uv/). Install it, e.g.:"
    echo "   brew install uv   # or: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Create .venv and install dependencies from uv.lock
echo "Installing dependencies (uv sync)..."
uv sync

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo ""
    echo "⚠️  Please edit .env and set your OGMIOS_WS_URL"
    echo ""
else
    echo ".env file already exists"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "To run the application:"
echo "  cd backend"
echo "  uv run python run.py"
echo ""
