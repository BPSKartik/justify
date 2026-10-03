#!/bin/sh
# Rebuild the 3D code city (frontend/city.js -> justify/hosted/static/city.js) after changing it.
# The built file is committed, so the container image needs no Node.
set -e
cd "$(dirname "$0")/../frontend"
npm ci --no-audit --no-fund
npm run build
