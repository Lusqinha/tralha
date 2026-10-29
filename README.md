<p align="center">
  <img src="src/tralha/static/icon.svg" width="96" height="96" alt="">
</p>

<h1 align="center">tralha</h1>

<p align="center">
  Self-hosted storage for the files your AI agents produce, with public links when you want them and an MCP server so agents can use it on their own.
</p>

When an AI agent tests your UI or adds visual proof to a pull request, it needs somewhere to put the screenshots. The usual workaround is a throwaway branch in the repository, committed only to get a GitHub URL for each image. tralha replaces that branch. The agent uploads the file to a bucket, asks for a link and pastes `![login](https://files.example.com/p/…)` into the PR.

The same place keeps the notes and findings the agent writes along the way. Markdown documents are indexed for full-text search, so a later session can find what an earlier one discovered.

Everything is private by default. A file only becomes reachable from the internet when someone creates a link for it, and that link can be revoked.

*Tralha* is Portuguese for odds and ends, the stuff that piles up in a junk drawer. It also stands for **T**ool for **R**emote **A**rtifact **L**inking & **H**osting for **A**Is.

![The admin panel showing a screenshot with its public link and details](docs/panel.png)

## How it works

Files live in buckets. A file's key is a path like `pr-128/login-before.png`, and the slashes act as folders. Each bucket has its own visibility, quota, cleanup rules and image compression setting.

tralha listens on two ports:

| Port | Serves | Who should reach it |
|---|---|---|
| 8000 (private) | REST API, MCP server, uploads, admin panel | You and your agents |
| 8001 (public) | `/p/<token>` links and public-read buckets at `/b/…` | Anyone, including GitHub's image proxy |

The split is by port because a Host header can be forged from outside. Port 8001 serves nothing that needs a key. Port 8000 turns away any request without a valid key or panel session before reading its body, so it can face the internet behind HTTPS, the way an S3 endpoint does, or stay on a VPN if you prefer.

Links are random tokens stored in the database. They never expire unless you ask for an expiry, because an image in a merged PR should keep working. A revoked link stops resolving at once, although browsers and GitHub's image proxy may keep a cached copy for up to an hour.

API keys are scoped per bucket with `bucket:permission` pairs, such as `scratch:write`, `pr-*:share` or `*:read`. An agent can get a key that writes and shares in two buckets and can delete nothing.

Cleanup rules delete files older than a given age, or not accessed for a given time, optionally only under a prefix. A rule can keep any file that still has an active link.

Image compression is off until you turn it on. In lossless mode PNG and GIF become pixel-identical WebP; in lossy mode JPEG is converted too.

## Quick start

With Docker:

```sh
git clone https://github.com/Lusqinha/tralha.git && cd tralha
cp .env.example .env            # the defaults work on your own machine
docker compose up -d --build

docker compose exec tralha tralha bucket create pr-assets
docker compose exec tralha tralha bucket create scratch --ttl 7d
docker compose exec tralha tralha key create admin --scope '*:admin'
```

Without Docker, using [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run tralha bucket create pr-assets
uv run tralha key create admin --scope '*:admin'
uv run tralha serve             # private port 8000, public port 8001
```

`key create` prints the key once. Only its hash is stored, so copy it right away.

Open http://localhost:8000/admin and sign in with the admin key to browse files, upload by drag and drop, create links and manage buckets.

## Connect an AI agent

Create a key for the agent with only the access it needs. `--expires` is optional:

```sh
tralha key create claude --expires 90d \
  --scope '*:read' \
  --scope 'pr-assets:write' --scope 'pr-assets:share' \
  --scope 'scratch:write' --scope 'scratch:share'
```

Add it to Claude Code. On a VPS, use your API domain instead of localhost:

```sh
claude mcp add --transport http --scope user tralha http://localhost:8000/mcp \
  --header "Authorization: Bearer trh_…"
```

Then ask for things in plain language:

> Take a screenshot of the login page, upload it to pr-assets under pr-128/ and give me the markdown for the PR.

> Save what you found about the token bug as a note in scratch/notes/ so we can find it later.

The MCP tools cover buckets, text documents, binary uploads, image previews for the model, links and search. Binary files never travel through MCP as base64. The agent calls `create_upload`, gets a single-use URL and sends the file with `curl`, which saves a lot of tokens.

ChatGPT connectors require OAuth, which tralha does not support yet.

## Deploy on a VPS

GitHub loads PR images through its own proxy, so links must be reachable from the internet over HTTPS. The compose file ships an optional Caddy proxy that serves both ports on their own domains and gets certificates by itself.

1. Point two DNS records at the server, for example `api.example.com` and `files.example.com`.
2. In `.env`, switch to the VPS block: set `TRALHA_API_URL` and `TRALHA_PUBLIC_URL` to the two `https://` addresses and `TRALHA_API_DOMAIN` and `TRALHA_FILES_DOMAIN` to the bare domains.
3. Start everything with the proxy profile:

   ```sh
   docker compose --profile proxy up -d --build
   ```

Only ports 80 and 443 open on the server. Both tralha ports stay bound to localhost, and Caddy reaches them over the compose network. `TRALHA_FORWARDED_ALLOW_IPS` already trusts that network, so tralha sees the real client IP and HTTPS scheme, and the panel cookie gets the `Secure` flag.

Then connect your local agent to `https://api.example.com/mcp`. With port 8000 on the internet, a few extra steps help:

- `TRALHA_ADMIN_ALLOWED_IPS` restricts the panel to your IPs or networks. Anyone else gets a 404 at `/admin`, while the API and MCP keep working from anywhere.
- `--expires` on `tralha key create` limits how long a leaked key stays useful. `tralha key list` shows when and from which IP each key was last used.
- Prefer a VPN such as Tailscale if you would rather keep the API off the internet altogether. Bind port 8000 to the VPN address and point `TRALHA_API_URL` at it; the agent uses that URL for uploads.

## Configuration

All settings are environment variables, and `.env.example` lists them with comments.

| Variable | Default | Purpose |
|---|---|---|
| `TRALHA_PORT` | `8000` | Private port |
| `TRALHA_PUBLIC_PORT` | `8001` | Public port |
| `TRALHA_API_URL` | `http://localhost:8000` | Base URL for upload links given to agents |
| `TRALHA_PUBLIC_URL` | `http://localhost:8001` | Base URL for public links |
| `TRALHA_DATA_DIR` | `./data` | Where files and the SQLite database are stored |
| `TRALHA_DATABASE_URL` | SQLite in the data dir | Or `postgresql+psycopg://user:pass@host/db` |
| `TRALHA_MAX_UPLOAD_MB` | `25` | Maximum size of one file |
| `TRALHA_UPLOAD_TTL` | `600` | Seconds an upload URL stays valid |
| `TRALHA_GC_INTERVAL` | `3600` | Seconds between cleanup runs |
| `TRALHA_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` headers; `.env.example` sets the compose network |
| `TRALHA_ADMIN_ALLOWED_IPS` | empty (anyone) | IPs or networks allowed to open `/admin` |
| `TRALHA_API_DOCS` | off | Serve OpenAPI docs at `/api/docs` |
| `TRALHA_API_DOMAIN`, `TRALHA_FILES_DOMAIN` | none | Domains for the bundled Caddy |

Compression and keeping originals are changed at runtime from the panel or with `tralha settings compression=lossless`.

## Security

Uploaded files come from agents, and agents may copy content from untrusted places. tralha treats every upload as hostile:

- Image decoding never happens in the server process. Each image is handled by a short-lived worker that cannot create processes, has capped memory and CPU, and, on Linux 5.13 or newer, runs under a Landlock ruleset that blocks running programs, opening TCP connections and touching any file other than its input and output.
- Pillow only opens PNG, JPEG, GIF and WebP, so a file disguised as an image cannot reach decoders such as Ghostscript. Oversized images and decompression bombs are rejected before decoding. tralha does not use ImageMagick, Ghostscript, librsvg or ffmpeg.
- The content type comes from the file's bytes, not from what the client claims. Everything served publicly carries a sandbox CSP, so an uploaded SVG or HTML file cannot run scripts, and HTML is served as a download.
- On port 8000, a request without a valid key or panel session is rejected before its body is read. JSON bodies are capped at 1 MB, and file bodies at the upload limit.
- An IP that sends 20 bad keys, panel logins or upload tokens within 5 minutes is locked out for 15 minutes.
- Keys can expire, and each use records the time and client IP.
- The admin panel uses HttpOnly, SameSite=Strict session cookies, refuses cross-origin form posts and allows no inline scripts.
- The container runs as a non-root user with a read-only root filesystem, no Linux capabilities and a process limit.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
```

The tests include an escape attempt from inside the image sandbox. It is skipped on kernels without Landlock.

Work happens on `develop`; `master` receives releases.

## Current limits

- Files are stored on local disk. An S3-compatible backend is planned.
- Video is stored as uploaded. Video compression needs ffmpeg and a background queue.
- There are no database migrations yet, so a schema change can require starting from a fresh database.

## License

[AGPL-3.0](LICENSE). If you run a modified tralha as a service for others, you must publish your changes.
