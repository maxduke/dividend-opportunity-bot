#!/bin/bash
docker run --rm -v "$(pwd):/app" -w /app python:3.12-slim-bookworm sh -c "pip install pip-tools==7.5.2 && pip-compile --upgrade requirements.in -o requirements.txt"
