# kino

Lightweight local video player and an experiment in AI-driven development.

- Flask + Jinja templates
- CouchDB backend
- Docker compose (`web` + `couchdb`)
- Background media scanner inside the web container

## Quick start

1. Put video files into `./media` (or mount your own path there).
2. Start services:

   ```bash
   docker compose up --build
   ```

3. Open `http://localhost:5050`.
4. Click **Refresh** to trigger a background scan.
