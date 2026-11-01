# GitHub → Forgejo personal repository mirrors

A one-shot, idempotent Python job that discovers repositories owned by one GitHub personal account and creates missing Forgejo **pull mirrors**. Forgejo performs the ongoing Git synchronization; this job only discovers repositories and provisions new mirrors.

## Behaviour and safety

- Uses the authenticated GitHub account's `/user/repos` endpoint with `visibility=all` and `affiliation=owner`.
- Filters results to the configured account's `owner.login` **and** `owner.type == "User"`. Organization-owned repositories are excluded. Archived repositories are included.
- Checks for each target repository before creating it. Existing repositories are never overwritten, deleted, renamed, or converted.
- Existing pull mirrors are recognized only when Forgejo reports a matching `original_url`. For a verified mirror, the script reconciles `mirror_interval` to the configured value using `PATCH /repos/{owner}/{repo}`. A normal repository or a mirror with a different/unknown source is reported as a conflict and left untouched.
- New mirrors are private by default (`MIRROR_VISIBILITY=private`). Use `preserve` to mirror GitHub visibility.
- Uses read-only GitHub access for private-source pulls. No GitHub write permission is needed.
- Uses GET-only automatic retries with backoff and `Retry-After` support. Migration POSTs are not blindly retried because a request may have succeeded even if the client timed out.
- Uses a local `flock` lock so concurrent runs do not race.
- Secrets are read from the process environment; they are not written to logs or command-line arguments.
- Non-zero exit status is returned when one or more repository operations fail. A later scheduled run can retry missing repositories.

This mirrors Git data (commits, branches, tags, and optionally LFS objects). It does **not** migrate GitHub issues, pull requests, discussions, releases, Actions run history, repository secrets, or settings. Workflow YAML files under `.github/workflows` are ordinary Git files and are mirrored with the repository.

## 1. Create tokens

### GitHub

Create a **fine-grained personal access token** with:

- Resource owner: your personal account.
- Repository access: **All repositories** (so repositories created in the future are included).
- Repository permissions: **Metadata: Read-only**, **Contents: Read-only**.

No organization access or write permissions are required. The token is also used by Forgejo to fetch private repositories on subsequent mirror runs, so keep it read-only and treat it as a long-lived secret.

### Forgejo

Create an access token for the Forgejo account that should own the mirrors:

- Repository access: **All** (public, private, and limited).
- Scopes: **read:user**, **write:repository**.

The script uses `GET /user` to verify the token owner, `GET /repos/{owner}/{repo}` to check for an existing target, and `POST /repos/migrate` to create a pull mirror. It does not need site-administrator privileges.

## 2. Configure Forgejo

In Forgejo's `app.ini`, confirm pull mirroring is enabled. The relevant defaults in current Forgejo documentation are:

```ini
[mirror]
ENABLED = true
DISABLE_NEW_PULL = false
MIN_INTERVAL = 10m

[cron.update_mirrors]
ENABLED = true
SCHEDULE = @every 10m
```

The server's `cron.update_mirrors` task must be enabled; otherwise the repositories can be created as mirrors without being periodically refreshed. The server's `[migrations]` allow/deny rules must also allow outbound access to `github.com` (or the applicable GitHub host). Ensure Forgejo LFS is enabled if `MIRROR_LFS=true`.

`MIRROR_INTERVAL` is the per-repository interval. It must not be shorter than the `MIN_INTERVAL` configured on the Forgejo server. A 15-minute interval works with Forgejo's documented default 10-minute minimum and 10-minute mirror queue schedule.

## 3. Install

Choose the NixOS instructions below for a NixOS host. For other Linux distributions, the included systemd user service works on distributions using systemd. You need Python 3, its venv/pip support, and systemd. Install those packages with your distribution's package manager; for example:

```bash
# Debian or Ubuntu
sudo apt install python3 python3-venv python3-pip

# Fedora
sudo dnf install python3 python3-pip

# Arch Linux
sudo pacman -S python python-pip
```

The repository includes `.env.example`, `requirements.txt`, and systemd service/timer units. Run these commands from a checkout of this repository:

```bash
mkdir -p ~/.local/opt ~/.config ~/.local/state/github-forgejo-mirror
cp -a . ~/.local/opt/github-forgejo-mirror/
cd ~/.local/opt/github-forgejo-mirror
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp .env.example ~/.config/github-forgejo-mirror.env
$EDITOR ~/.config/github-forgejo-mirror.env
chmod 600 ~/.config/github-forgejo-mirror.env
```

Edit the copied environment file and replace the placeholder credentials and account names. Keep the file private (`chmod 600`). With `STATE_DIR` omitted, the script uses `~/.local/state/github-forgejo-mirror` for this user service.

Install and enable the timer:

```bash
mkdir -p ~/.config/systemd/user
cp github-forgejo-mirror.service github-forgejo-mirror.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

The unit's `WorkingDirectory` and `ExecStart` assume the project is at `~/.local/opt/github-forgejo-mirror`, as in the install commands above. If your distribution does not use systemd, install Python and its venv support the same way, then schedule `~/.local/opt/github-forgejo-mirror/.venv/bin/python ~/.local/opt/github-forgejo-mirror/github_forgejo_mirror.py` with its native scheduler (for example, cron). Ensure that scheduler receives the required variables from the environment file; a systemd `EnvironmentFile` is not a shell script.

## 4. Test safely, then enable

Run a dry run first:

```bash
cd ~/.local/opt/github-forgejo-mirror
set -a
. ~/.config/github-forgejo-mirror.env
set +a
.venv/bin/python github_forgejo_mirror.py --dry-run
```

Then run once for real:

```bash
.venv/bin/python github_forgejo_mirror.py
```

Enable automatic discovery every 15 minutes:

```bash
systemctl --user enable --now github-forgejo-mirror.timer
systemctl --user list-timers github-forgejo-mirror.timer
```

For a headless machine where the user may not be logged in, enable lingering for that user:

```bash
loginctl enable-linger "$USER"
```

Inspect logs:

```bash
journalctl --user -u github-forgejo-mirror.service -n 100 --no-pager
journalctl --user -u github-forgejo-mirror.service -f
```

Run immediately on demand:

```bash
systemctl --user start github-forgejo-mirror.service
```

### NixOS

The module at `nix/module.nix` builds the script with its Python dependency, runs it as a restricted dynamic user, stores its lock under `/var/lib/github-forgejo-mirror`, and schedules it with a persistent systemd timer. Keep this repository at a stable path available when evaluating your NixOS configuration, or copy `nix/module.nix` into your configuration repository. Import it and enable the service (adjust the path to this checkout):

```nix
{
  imports = [ /path/to/github-forgejo-mirror/nix/module.nix ];

  services.github-forgejo-mirror = {
    enable = true;
    environmentFile = "/run/secrets/github-forgejo-mirror.env";
    interval = "15m";
  };
}
```

Create the environment file through a secrets manager such as agenix or sops-nix. It must be available at the configured path at service start and readable by the systemd service manager. Put the required variables from `.env.example` in it as plain `KEY=value` lines; `EnvironmentFile` does not support shell expansion. Keep the secret out of the Nix store. The module sets `STATE_DIR` to its managed state directory, so leave `STATE_DIR` out of the secret file.

Apply the configuration with `sudo nixos-rebuild switch`. Verify the timer and run the job once manually if desired:

```bash
systemctl status github-forgejo-mirror.timer
sudo systemctl start github-forgejo-mirror.service
journalctl -u github-forgejo-mirror.service -n 100 --no-pager
```

To check credentials and connectivity before provisioning mirrors, temporarily set `DRY_RUN=true` in the secret environment file, start the service, then remove that setting and start it again for the real run.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_USERNAME` | required | Personal GitHub login to mirror |
| `GITHUB_TOKEN` | required | Fine-grained, read-only GitHub token |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub REST API base; set to your GHES API URL if applicable |
| `FORGEJO_URL` | required | Forgejo base URL, without `/api/v1` |
| `FORGEJO_USERNAME` | required | Forgejo account that owns the mirrors |
| `FORGEJO_TOKEN` | required | Forgejo token with `read:user`, `write:repository` |
| `MIRROR_INTERVAL` | `15m` | Ongoing per-repo pull interval configured in Forgejo |
| `MIRROR_VISIBILITY` | `private` | `private` or `preserve` GitHub visibility |
| `MIRROR_LFS` | `true` | Import Git LFS objects; requires Forgejo LFS support |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Normal API request timeout |
| `MIGRATION_TIMEOUT_SECONDS` | `3600` | Read timeout for initial Forgejo migration/clone |
| `STATE_DIR` | `~/.local/state/github-forgejo-mirror` | Directory for lock file |
| `DRY_RUN` | `false` | Report planned creations without creating them |
| `LOG_LEVEL` | `INFO` | Python log level |
| `ALLOW_INSECURE_FORGEJO` | `false` | Explicit opt-in to HTTP Forgejo URL on trusted private networks |

## Existing repositories and token rotation

Forgejo pull mirrors must be created as mirrors at migration time. An existing normal Forgejo repository cannot be converted by this script; it is left untouched and logged as a conflict. Manually remove/rename the existing target only after confirming that it contains nothing you need, then let the next run create the mirror.

Private mirrors need the GitHub token to remain valid for future pulls. If you rotate the GitHub token, update the stored mirror credentials in Forgejo as well (or re-create the mirrors). The script deliberately does not attempt to patch mirror credentials on existing repositories. It only patches `mirror_interval` after verifying the mirror's `original_url` matches the corresponding GitHub clone URL.

## API endpoints used

| API | Method | Endpoint | Reason |
|---|---|---|---|
| GitHub REST | GET | `/user` | Verify the authenticated token owner |
| GitHub REST | GET | `/user/repos?visibility=all&affiliation=owner&per_page=100` | Enumerate owned repositories; follow `Link: rel="next"` pagination |
| Forgejo REST | GET | `/user` | Verify the Forgejo token owner |
| Forgejo REST | GET | `/repos/{owner}/{repo}` | Detect existing targets without overwriting them |
| Forgejo REST | PATCH | `/repos/{owner}/{repo}` | Reconcile `mirror_interval` on a verified existing pull mirror |
| Forgejo REST | POST | `/repos/migrate` | Create a new repository with `mirror=true` and `mirror_interval` |

Forgejo publishes the API reference and OpenAPI document on each instance at `/api/swagger` and `/swagger.v1.json`. Check your own instance's spec if running an older major version or a customized build.


## References

- [GitHub REST: list repositories for the authenticated user](https://docs.github.com/en/rest/repos/repos#list-repositories-for-the-authenticated-user)
- [GitHub REST: get the authenticated user](https://docs.github.com/en/rest/users/users#get-the-authenticated-user)
- [Forgejo API usage and per-instance OpenAPI document](https://forgejo.org/docs/latest/user/api/usage/)
- [Forgejo repository mirrors](https://forgejo.org/docs/v17.0/user/repo-mirror/)
- [Forgejo mirror and migration configuration](https://forgejo.org/docs/v17.0/admin/config-cheat-sheet/)
- [Forgejo access-token scopes](https://forgejo.org/docs/v17.0/user/authentication/token-scope/)
