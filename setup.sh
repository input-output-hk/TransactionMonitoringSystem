#!/bin/bash
# Setup script for Cardano Transaction Monitor

set -e

echo "🔷 Setting up Cardano Transaction Monitor..."

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $python_version"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

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
echo "  source venv/bin/activate"
echo "  cd backend"
echo "  python run.py"
echo ""
