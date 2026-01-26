# Dockerfile for TDeck (Calendar web UI)
# - Uses a slim Python image
# - Installs requirements and runs webui.py

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# system deps for optional packages (add if needed)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# copy app (copy full project so requirements.txt is available in image)
COPY . /app
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# Expose default port (config.json controls actual port)
EXPOSE 5000

# Default command: start the web UI
CMD ["python", "webui.py"]
