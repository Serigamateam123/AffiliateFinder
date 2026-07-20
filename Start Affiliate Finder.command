#!/bin/bash
# Double-click this to launch the Affiliate Creator Finder.
cd "$(dirname "$0")"

if [ ! -x "venv/bin/python" ]; then
  echo "Setting up for the first time…"
  python3 -m venv venv || { echo "Could not create venv. Is python3 installed?"; read -r; exit 1; }
  ./venv/bin/pip install -q -r requirements.txt
fi

echo "Starting Affiliate Creator Finder…"
echo "It opens at http://localhost:7374 — close this window to stop it."
exec ./venv/bin/python ui_server.py
