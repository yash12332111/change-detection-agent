#!/bin/bash

echo "🚀 Starting Change Detection Agent locally..."

# Start backend in the background
echo "→ Starting FastAPI backend on port 8001..."
cd "/Users/eslavathyaswanthvenkatasai/Change Detection Agent/backend"
source .venv/bin/activate
uvicorn main:app --port 8001 --reload &
BACKEND_PID=$!

# Start frontend in the foreground
echo "→ Starting Next.js frontend on port 3005..."
cd "/Users/eslavathyaswanthvenkatasai/Change Detection Agent/frontend"
npm run dev -- -p 3005

# If the user presses Ctrl+C to stop the frontend, kill the backend too
trap "echo 'Stopping backend...'; kill $BACKEND_PID" EXIT
