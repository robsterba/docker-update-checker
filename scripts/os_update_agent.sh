#!/bin/bash
# OS Update Agent - Shell wrapper for cron
# This script runs the Python agent and is designed to be called from cron

# Configuration (can be overridden by environment variables)
OUTPUT_DIR="/var/lib/docker-update-checker"
OUTPUT_FILE="${OUTPUT_FILE:-$OUTPUT_DIR/os-updates.json}"
LOG_DIR="/var/log/docker-update-checker"
LOG_FILE="${LOG_FILE:-$LOG_DIR/os-updates.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure directories exist
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

# Run the Python agent
python3 "$SCRIPT_DIR/os_update_agent.py" >> "$LOG_FILE" 2>&1
