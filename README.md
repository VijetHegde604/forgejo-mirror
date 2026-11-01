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

Choose the NixOS instructions below for a NixOS host. On other Linux distributions, use the installer for a system service or follow the manual user-service instructions. The repository includes `.env.example`, `requirements.txt`, and systemd service/timer units.

### Non-NixOS Linux installer

On a systemd-based Debian/Ubuntu, Fedora, or Arch Linux machine, the interactive installer can install the Python dependencies, configure the service account and systemd timer, and prompt for the required accounts and tokens:

```bash
git clone https://github.com/VijetHegde604/forgejo-mirror.git
cd forgejo-mirror
sudo ./scripts/install.sh
```

Token input is hidden while you type. The installer writes credentials to `/etc/github-forgejo-mirror.env` with root-only permissions, installs the application under `/opt/github-forgejo-mirror`, and enables a system timer. It asks before installing OS packages and before proceeding with an HTTP Forgejo URL. HTTP requires explicit confirmation and is intended only for a trusted private network. The timer starts the job five minutes after boot and repeats every 15 minutes. The installer supports apt, dnf, and pacman; it requires systemd and does not support NixOS.

After installation, check the timer and logs with:

```bash
systemctl status github-forgejo-mirror.timer
journalctl -u github-forgejo-mirror.service -n 100 --no-pager
```

To change credentials, rerun `sudo ./scripts/install.sh` from the checkout and enter the updated values. This replaces the environment file and refreshes the installed script and service units.

### Manual user-service installation

For a manual setup, install Python 3, venv/pip support, and systemd with your distribution's package manager. For example:

```bash
# Debian or Ubuntu
sudo apt install python3 python3-venv python3-pip

# Fedora
sudo dnf install python3 python3-pip

# Arch Linux
sudo pacman -S python python-pip
```

Then run these commands from a checkout of this repository:

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

The flake provides a package and a NixOS module. The module runs the mirror job as a restricted dynamic user, keeps its lock/state in `/var/lib/github-forgejo-mirror`, and installs a persistent systemd timer. It builds Python and the `requests` dependency for you.

#### Add the flake to a NixOS configuration

This example shows the relevant parts of a complete `/etc/nixos/flake.nix`. If you already have a flake, add the `forgejo-mirror` input and module to your existing host instead of replacing your file:

```nix
{
  description = "NixOS configuration";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    forgejo-mirror.url = "github:VijetHegde604/forgejo-mirror";
  };

  outputs = { self, nixpkgs, forgejo-mirror, ... }: {
    nixosConfigurations.nix-server = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        forgejo-mirror.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Replace `nix-server` with the host name you want to use after `#` in the rebuild command. Use `aarch64-linux` for a 64-bit ARM host. The first flake evaluation creates `flake.lock`, which records the exact nixpkgs and application revisions used by your system.

#### Configure the service

Add this to the NixOS module used above, commonly `/etc/nixos/configuration.nix`:

```nix
{ ... }:
{
  services.github-forgejo-mirror = {
    enable = true;
    environmentFile = "/etc/github-forgejo-mirror.env";

    # How often this program discovers repos and reconciles their settings.
    interval = "15m";
  };
}
```

The available module options are:

| Option | Default | Purpose |
|---|---|---|
| `services.github-forgejo-mirror.enable` | `false` | Installs and enables the systemd service and timer. |
| `services.github-forgejo-mirror.environmentFile` | `/run/secrets/github-forgejo-mirror.env` | Absolute path to a systemd `EnvironmentFile` with credentials and optional settings. |
| `services.github-forgejo-mirror.interval` | `15m` | How often the systemd timer runs the discovery job. |

`interval` controls when this program runs. The separate `MIRROR_INTERVAL` environment variable controls how frequently Forgejo pulls updates for each repository; that value must meet the Forgejo server's configured minimum mirror interval. The systemd timer runs five minutes after boot and then at the configured `interval`.

#### Supply the environment variables

For a machine without a secret manager, create the environment file directly on the host so the tokens stay out of the Nix store:

```bash
sudo install -o root -g root -m 600 /dev/null /etc/github-forgejo-mirror.env
sudoedit /etc/github-forgejo-mirror.env
```

Add the required values as plain `KEY=value` lines. For the Forgejo server at `http://nix-server:3001`, the minimum file is:

```ini
GITHUB_USERNAME=your-github-login
GITHUB_TOKEN=your-github-token
FORGEJO_URL=http://nix-server:3001
ALLOW_INSECURE_FORGEJO=true
FORGEJO_USERNAME=your-forgejo-login
FORGEJO_TOKEN=your-forgejo-token
```

For a public or otherwise TLS-enabled instance, use `FORGEJO_URL=https://forgejo.example.com` and omit `ALLOW_INSECURE_FORGEJO`. HTTP is rejected by default; set `ALLOW_INSECURE_FORGEJO=true` only when using HTTP on a trusted private network. `FORGEJO_URL` is the base address and must not include `/api/v1`.

Optional settings can go in the same file. For example:

```ini
GITHUB_API_URL=https://api.github.com
MIRROR_INTERVAL=15m
MIRROR_VISIBILITY=private
MIRROR_LFS=true
REQUEST_TIMEOUT_SECONDS=30
MIGRATION_TIMEOUT_SECONDS=3600
DRY_RUN=true
LOG_LEVEL=INFO
```

Remove `DRY_RUN=true` or change it to `false` after checking connectivity; while true, the job reports planned mirror creation without creating repositories. `MIRROR_VISIBILITY` accepts `private` or `preserve`. `EnvironmentFile` uses plain systemd `KEY=value` syntax, not shell syntax, so do not use `export`, shell expansion, or source the file. Keep it owned by root with mode `600`; systemd reads it before starting the restricted service. The module sets `STATE_DIR` itself, so do not add `STATE_DIR` to this file.

Putting tokens directly in NixOS configuration or `environment.etc.<name>.text` can expose them through the Nix store or system configuration outputs. The manually created `/etc` file above avoids storing tokens in the Nix store. A secrets manager such as agenix or sops-nix can provision the same environment file declaratively if desired.

#### Apply and operate the configuration

If flakes are not enabled on the host yet, add this setting to your NixOS configuration and apply it using your current rebuild method:

```nix
nix.settings.experimental-features = [ "nix-command" "flakes" ];
```

For a host that is not already using flakes, first apply that setting with the traditional command:

```bash
sudo nixos-rebuild switch
```

Before activating the service for the first time, set `DRY_RUN=true` in `/etc/github-forgejo-mirror.env` so its initial scheduled run only checks credentials and reports planned changes. Then build and activate the flake (replace `nix-server` with the host attribute under `nixosConfigurations`):

```bash
sudo nixos-rebuild switch --flake /etc/nixos#nix-server
```

Check the timer and run the service manually to inspect the result:

```bash
systemctl status github-forgejo-mirror.timer
sudo systemctl start github-forgejo-mirror.service
journalctl -u github-forgejo-mirror.service -n 100 --no-pager
```

With `DRY_RUN=true`, start the service and review its logs to validate credentials and connectivity without creating mirrors. Once that looks right, remove the setting (or set it to `false`) and start the service again to create any missing mirrors.

The flake also exposes a standalone package for systems that do not use the NixOS module:

```bash
nix profile install github:VijetHegde604/forgejo-mirror
nix run github:VijetHegde604/forgejo-mirror -- --help
```

The standalone package still needs the required environment variables supplied by its caller; use the module when you want NixOS to configure the scheduled service.

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
