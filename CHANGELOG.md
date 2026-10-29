# Changelog

All notable changes to tralha are listed here. Versions follow [Semantic Versioning](https://semver.org/); while the version is below 1.0, a minor release can break compatibility.

## [0.2.0] - 2026-10-29

### Added

- Easypanel deployment: `deploy/easypanel/schema.json` for Create from Schema, and the same definition as a template in the easypanel-io/templates layout.

### Fixed

- CLI help and error messages that were still in Portuguese.

### Documentation

- The README links to the new wiki and explains the Easypanel deployment.

## [0.1.0] - 2026-10-29

First public release.

### Added

- Buckets with folder-like keys, per-bucket visibility, quota and image compression.
- Revocable public links that never expire unless an expiry is set, plus public-read buckets.
- Single-use upload URLs, so agents send binary files with `curl` instead of base64.
- Full-text search over text and markdown documents, on SQLite (FTS5) or Postgres.
- API keys scoped per bucket, with optional expiry and last-use tracking.
- MCP server at `/mcp` with tools for buckets, documents, uploads, image previews, links and search.
- Web admin panel to browse, preview and upload files, manage links, buckets, cleanup rules and system settings.
- Cleanup rules by age or last access, optionally per prefix and keeping files with active links.
- Opt-in WebP compression for PNG, JPEG and GIF, lossless or lossy, optionally keeping the original.
- Separate private and public ports, with an optional Caddy proxy for HTTPS on a VPS.
- CLI for keys, buckets, rules, settings and cleanup.

### Security

- Image decoding runs in a sandboxed worker with resource limits, no process creation and a Landlock ruleset.
- Requests without a valid key or session are rejected before their body is read; repeated failures lock the client IP out.
- Public responses carry a sandbox CSP; the content type is detected from file bytes.
- Hardened container: non-root user, read-only root filesystem, no capabilities.

### Known limits

- Files are stored on local disk only.
- No database migrations yet; a schema change can require a fresh database.
- ChatGPT connectors need OAuth, which is not supported yet.

[0.2.0]: https://github.com/Lusqinha/tralha/releases/tag/v0.2.0
[0.1.0]: https://github.com/Lusqinha/tralha/releases/tag/v0.1.0
