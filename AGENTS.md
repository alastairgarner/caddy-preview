# caddy-preview

- Python stdlib only. CLI: `./preview`; tests: `python3 -m unittest discover -s tests -v`.
- Tests must stay isolated: fixtures, fake commands/admin API; offline real-Caddy adaptation is OK. Never contact live Caddy or DNS in tests.
- Runtime registry: `/etc/caddy/preview`, single-user writable; readable generated config, private state. No home permissions or extra groups.
- Implementation is not deployment. Do not install, change DNS/live Caddy/systemd, grant permissions, or restart services without approval.
- Never read/copy the Cloudflare secret environment file or token. Never print config-bearing API responses or Caddy diagnostics.
- Preserve existing services. Serialize mutations, adapt the combined config, atomically publish and gracefully reload via localhost admin API; roll back failures. Interrupted/uncertain reloads require explicit recovery.
- Keep README runbook and tests aligned with behavior. Do not commit or create remotes unless requested.
