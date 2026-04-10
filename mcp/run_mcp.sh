#!/bin/bash
# Launcher script for MCP server via uv
# Used by Claude Desktop to start the MCP server with correct dependencies
cd /Users/ashwathstephen/Documents/GitHub/sairo/mcp
exec uv run --with "mcp[cli]" --with httpx python server.py --transport stdio
