FROM node:24-bookworm-slim AS media-runtime
WORKDIR /media-runtime
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=media-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=media-runtime /media-runtime/node_modules /app/node_modules
RUN node --version

COPY . .

# Fix Windows CRLF line endings and make entrypoint executable
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

EXPOSE 5000
VOLUME ["/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
