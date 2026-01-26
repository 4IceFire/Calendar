Docker usage for TDeck (Calendar)

Prereqs
- Docker installed
- (Optional) docker-compose

Build image (from repo root):

```bash
docker build -t tdeck-calendar:latest .
```

Run with Docker (mapping port 5000):

```bash
docker run --rm -p 5000:5000 \
  -v "$(pwd)/config.json:/app/config.json" \
  -v "$(pwd)/events.json:/app/events.json" \
  -v "$(pwd)/timer_presets.json:/app/timer_presets.json" \
  -v "$(pwd)/videohub_presets.json:/app/videohub_presets.json" \
  -v "$(pwd)/auth.db:/app/auth.db" \
  tdeck-calendar:latest
```

Run with docker-compose (recommended for development):

```bash
docker compose up --build
```

Notes
- The app reads its port and host from `config.json` (`webserver_port`, `webserver_host`). The Dockerfile exposes port 5000 by default; make sure your `config.json` contains `webserver_port: 5000` or change the `ports` mapping accordingly.
- Persisted files: the compose file mounts `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, and `auth.db` to keep settings and user accounts across restarts.
- If you change Python dependencies, update `requirements.txt` and rebuild the image.
- To run in production, consider using a process manager or reverse proxy (Nginx) and secure the host.

Troubleshooting
- If the container starts but the UI is not reachable, check container logs:

```bash
docker compose logs -f
# or
docker logs <container-id>
```

- If you change `webserver_port` in `config.json`, recreate the container or update `docker-compose.yml` port mapping.
