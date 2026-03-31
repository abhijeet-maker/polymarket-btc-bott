#!/bin/bash
set -e

echo "================================"
echo "Polymarket BTC Bot Setup"
echo "================================"

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python version: $python_version"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Create .env file if not exists
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo "✓ Created .env - edit with your PRIVATE_KEY"
fi

echo ""
echo "================================"
echo "Setup Complete!"
echo "================================"
echo ""
echo "Next steps:"
echo "  1. Activate venv: source venv/bin/activate"
echo "  2. Edit .env and add your PRIVATE_KEY"
echo "  3. Test with DRY_RUN=True: python bot.py"
echo "  4. For live trading, set DRY_RUN=False"
echo ""
