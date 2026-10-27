# caddy-preview

Python 3 stdlib-only CLI publishing temporary localhost development servers through an existing Caddy + Tailscale setup.

```sh
preview register checkout-test --port 5173 --ttl 2h
# https://checkout-test.dev.hp.affablegecko.net
preview list
preview list --json
preview unregister checkout-test
preview prune
```

**Implementation is not deployment.** Installation, system configuration, DNS and live reloads require separate approval. No secret environment file or token is needed by this CLI. Do not read, copy, or put tokens in this repository.

## Requirements and policy

- Python 3.9+ on Linux; no pip packages. Caddy with the Cloudflare DNS module, an existing `cloudflare_tls` snippet, and a localhost admin listener.
- Default registry: **`/etc/caddy/preview/`**, owned/writable by the invoking user. Caddy only needs to read generated configuration. No home-directory access, extra group membership, sudo at runtime, or process restarts.
- Routes target **`127.0.0.1` only**, ports **1024–65535**, excluding 2019 and the configured admin port. This is not a general URL proxy. Bind development servers to `127.0.0.1` (not only `::1`).
- Names are single lowercase DNS labels, up to 63 characters. One port per name and one name per port. Re-registering the same name/port renews its TTL. To change a port, explicitly unregister first. New names colliding with exact host matchers in the combined config are refused.
- Default TTL: 2h; maximum: 7d. Units: `s`, `m`, `h`, `d`. Expiry is based on wall-clock time and **does not remove routes without `prune`**. A dead development server is not proof a port is safe: a stale route can expose a subsequent process that reuses it.
- A single-user registry, not a multi-user security boundary. UID ownership is recorded and checked. Other users must not have write access to the registry. Processes running as its owner, including agents, can publish localhost services; use only trusted agents. The port restrictions do not identify every sensitive local service.
- Anyone allowed to reach this machine over the tailnet and its ACLs can access the preview URLs. HTTPS is not application authentication.

## Machine context (read-only inspection)

- `/etc/caddy/Caddyfile`: `auto_https disable_redirects`, `cloudflare_tls` snippet binding `100.90.71.107`, Cloudflare DNS TLS using `{env.CF_DNS_API_TOKEN}`, resolvers `1.1.1.1` and `1.0.0.1`.
- Existing services: sync → `127.0.0.1:8384`, code → `127.0.0.1:3773`, metrics → `127.0.0.1:8090` (inspect the live file rather than assuming this list is current).
- Caddy service runs as `caddy:caddy`; `ProtectSystem=full`, no `ProtectHome`. `/etc/caddy` is root-owned, mode 0755; the main file is root-owned, mode 0644.
- Home is mode 0700. Generated imports intentionally live outside it.
- `/usr/local/bin/caddy` was available with version v2.11.7. Check the **effective** service command/drop-ins before deploying; the packaged unit and effective command can differ.
- The root-only Cloudflare environment file must remain untouched. The running Caddy process resolves its existing environment placeholder during provisioning. The CLI neither reads the file nor retrieves the live configuration from the admin API.

## How updates work

The main config gets one top-level import:

```caddyfile
import "/etc/caddy/preview/current/Caddyfile"
```

`current` is an atomically replaced symlink to a complete immutable generation:

```text
/etc/caddy/preview/
  .lock                     # private flock, all CLI operations serialized
  current -> gen-.../
  gen-.../
    Caddyfile               # wildcard site, imports cloudflare_tls + routes/*.caddy
    routes/checkout-test.caddy
    state.json              # private UID/port/expiry metadata
```

Generated config/directories are 0644/0755, matching ordinary readable system configuration. They contain **no secrets**, only names, bind addresses and ports. State, lock and recovery marker are 0600. There is no requirement to add either user to the other's group.

Each mutation builds a new generation, snapshots the main Caddyfile, and runs `caddy adapt` on the **combined** old and proposed configuration without changing the main file. Relative imports use the main file's directory. It refuses a missing/duplicate preview import and detects main-file edits during validation. Caddy diagnostics are suppressed because diagnostics from unrelated configuration may contain secrets.

After structural adaptation succeeds, it writes a durable recovery marker, atomically publishes the new generation, and posts the full JSON configuration to **`http://127.0.0.1:2019/load`**. Caddy itself performs runtime validation/provisioning using its existing environment and gracefully reloads; there is no process restart. Offline adaptation is not a substitute for that runtime validation (e.g. TLS provisioning can still fail). No new DNS record or certificate is required for each route.

On reload failure, the old symlink is restored and the old combined config reloaded. If rollback also fails, or execution is interrupted, mutations and listing stop until:

```sh
preview recover
```

Recovery validates and reloads the generation that `current` presently points to, then clears the marker. It can therefore complete a crashed publish or restore the previously published configuration. Check the result before relying on a URL. Pending generations are retained until recovery; successful operations remove unused generations.

**Coordination limit:** the registry lock serializes this CLI, not arbitrary Caddy API clients, systemctl reloads or edits to other imported files. Treat the on-disk main config as authoritative. Do not manage unrelated services solely through ephemeral admin API changes: loading the full file-based configuration would overwrite those. Coordinate manual edits/reloads with this same lock (`flock /etc/caddy/preview/.lock ...`); do not restart/reload the service during a preview mutation. A crash after the disk publish but before the admin reload creates a temporary disk/live mismatch, deliberately surfaced by the recovery marker.

## Tests (safe before deployment)

```sh
python3 -m unittest discover -s tests -v
./preview --help
```

Tests use temporary fixture directories, fake Caddy executables and a localhost fake admin server. An optional test uses the installed Caddy only for **offline adaptation** of a fixture with an internal TLS issuer. Tests never contact live Caddy, Cloudflare, DNS, or a real development server and never read the token. They cover lifecycle, renewal, collisions, ownership, expiry, concurrent processes, adaptation failure, rollback, interrupted recovery, private state and readable config modes.

## One-time deployment — approval required

The following is a runbook, **not something the implementation executes automatically**.

### 1. Install executable and initialize registry

Check the chosen Python/Caddy binaries and existing directory ownership first. Do not reuse an unrelated or shared-writable directory.

```sh
install -d -m 0755 "$HOME/.local/bin"
install -m 0755 ./preview "$HOME/.local/bin/preview"
# Only this initial directory creation needs root. No broad sudo grant.
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0755 /etc/caddy/preview
"$HOME/.local/bin/preview" init
```

`init` creates files only; it does not edit the main config, contact Caddy or create DNS records. All later operations must use the same user and registry settings. Global options precede the command:

```sh
preview --caddy /usr/local/bin/caddy --directory /etc/caddy/preview list
```

If deploying to a different machine, set `--domain` and `--bind` consistently for **every** command, including `init` and manual `prune`. The existing TLS snippet must also bind the intended Tailscale address; this CLI does not rewrite it.

### 2. Wildcard DNS and main import

After explicit approval:

1. Create/confirm a **DNS-only** (not Cloudflare-proxied) wildcard A record `*.dev.hp.affablegecko.net` → `100.90.71.107`. Clients need a working Tailscale connection and ACL access to port 443. Do not add an AAAA record unless the server actually listens on an appropriate tailnet IPv6 address.
2. Back up the main file (root-owned backup outside the registry). Add the one top-level import shown above, leaving **all existing services and the `cloudflare_tls` snippet unchanged**.
3. Check that Caddy can read the generated files: e.g. `sudo -u caddy test -r /etc/caddy/preview/current/Caddyfile`. Also check one generated fragment after a route exists. Do not loosen the home directory or token file permissions.
4. Run `preview --caddy /usr/local/bin/caddy recover` to adapt the combined config and perform the initial graceful admin reload. Initial wildcard certificate provisioning may require working Cloudflare credentials/DNS and a longer `--timeout`; do not extract the token to diagnose it. If initial loading fails, restore the main-file backup and investigate before proceeding.
5. Verify existing sync/code/metrics services remain healthy. Start a harmless localhost-only fixture server, register it, check its HTTPS URL from a tailnet client, then unregister it. Confirm an unknown preview hostname returns 404.

The wildcard site uses the existing DNS TLS snippet, permitting a wildcard certificate. The admin API must stay on loopback. Treat local admin API access as privileged: it can replace all Caddy configuration. No authentication boundary is added by this CLI. If local admin access is restricted, do not weaken it automatically; arrange a narrowly scoped deployment separately.

### 3. Manual expiry cleanup

Cleanup is manual; no background service or timer is provided.

```sh
preview prune
```

Run this as the registry owner to remove expired routes. Unregister routes when finished rather than waiting for expiry. TTL alone does not stop exposure: routes remain active until successfully pruned or unregistered, and can expose later processes reusing their ports.

### 4. Non-interactive T3 / agent setup

An interactive shell's `.bashrc` is not sufficient for T3's service-spawned commands. Set an explicit PATH on the **actual T3 systemd service**, retaining its existing entries. For example, a user-service drop-in (substitute the real service name and any required existing paths):

```ini
[Service]
Environment="PATH=/home/alastair/.local/bin:/usr/local/bin:/usr/bin:/bin"
```

Apply the service environment/restart only after approval; this is separate from Caddy (which does not restart). For a system-managed T3 service, use its system service drop-in instead. Do not use `~` or `$HOME` inside systemd's `Environment=` value: provide absolute paths. Verify from a **fresh non-interactive T3 command** with `command -v preview; preview list`. Until then, use `/home/alastair/.local/bin/preview` explicitly.

Add to the relevant harness/repository agent instructions after installation:

```text
For a shareable dev URL, bind the dev server to 127.0.0.1 and choose a unique
checkout-specific name. Run /home/alastair/.local/bin/preview register NAME
--port PORT --ttl 2h, then return its HTTPS URL. Unregister NAME when done.
Do not expose admin/debug/credential services. Never change DNS, TLS tokens,
live Caddy configuration, or permissions. If a collision occurs, pick a new
name/port; do not remove another task's route. Expiry depends on cleanup.
```

### Removal

Unregister the routes, remove the dedicated main import under approval, and gracefully reload the remaining main configuration using the established administrative procedure. Only then remove the registry/executable and (if desired and approved) the wildcard DNS record. Never delete existing service blocks or the shared TLS snippet.

## Repository status

Public repository: https://github.com/alastairgarner/caddy-preview. Implementation and tests do not constitute approval to install, change DNS, alter Caddy's live configuration or grant permissions.
