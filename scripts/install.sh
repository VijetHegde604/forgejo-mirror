#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly APP_NAME=github-forgejo-mirror
readonly INSTALL_DIR=/opt/github-forgejo-mirror
readonly ENV_FILE=/etc/github-forgejo-mirror.env
readonly SERVICE_FILE=/etc/systemd/system/github-forgejo-mirror.service
readonly TIMER_FILE=/etc/systemd/system/github-forgejo-mirror.timer
readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

if [[ -e /etc/NIXOS ]]; then
  fail "This installer is for non-NixOS Linux systems. See the NixOS flake instructions in README.md."
fi

if (( EUID != 0 )); then
  command -v sudo >/dev/null || fail "Run this installer as root or install sudo."
  exec sudo -- "$SOURCE_DIR/scripts/install.sh" "$@"
fi

[[ -d "$SOURCE_DIR" && -f "$SOURCE_DIR/github_forgejo_mirror.py" && -f "$SOURCE_DIR/requirements.txt" ]] \
  || fail "Run this script from a complete repository checkout."
command -v systemctl >/dev/null || fail "systemd is required, but systemctl was not found."
command -v useradd >/dev/null || fail "The useradd command is required to create the service account."

install_system_dependencies() {
  if command -v apt-get >/dev/null; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null; then
    dnf install -y python3 python3-pip
  elif command -v pacman >/dev/null; then
    pacman -S --needed --noconfirm python python-pip
  else
    fail "Unsupported distribution. Install Python 3, venv support, pip, and systemd, then adapt this installer."
  fi
}

prompt_value() {
  local prompt="$1" value
  while true; do
    read -r -p "$prompt: " value
    [[ -n "$value" ]] || { printf 'A value is required.\n' >&2; continue; }
    [[ "$value" != *[[:space:]]* ]] || { printf 'Whitespace is not allowed in this value.\n' >&2; continue; }
    REPLY="$value"
    return
  done
}

prompt_secret() {
  local prompt="$1" value
  while true; do
    read -r -s -p "$prompt: " value
    printf '\n'
    [[ -n "$value" ]] || { printf 'A value is required.\n' >&2; continue; }
    [[ "$value" != *[[:space:]]* ]] || { printf 'Whitespace is not allowed in tokens.\n' >&2; continue; }
    REPLY="$value"
    return
  done
}

printf 'GitHub to Forgejo mirror installer\n'
printf 'Credentials are entered interactively; token input will not be displayed.\n\n'

prompt_value 'GitHub username'
github_username="$REPLY"
prompt_secret 'GitHub token'
github_token="$REPLY"
prompt_value 'Forgejo base URL (for example https://forgejo.example.com)'
forgejo_url="${REPLY%/}"
[[ "$forgejo_url" =~ ^https?://[^/]+(/.*)?$ ]] || fail "Forgejo URL must be an absolute HTTP(S) URL."
[[ "$forgejo_url" != */api/v1 ]] || fail "Enter the Forgejo base URL without /api/v1."

allow_insecure=false
if [[ "$forgejo_url" == http://* ]]; then
  printf 'HTTP is allowed only for a trusted private network.\n'
  read -r -p 'Continue with this HTTP URL? [y/N] ' confirm_http
  [[ "$confirm_http" =~ ^[Yy]$ ]] || fail "Use an HTTPS Forgejo URL or rerun and explicitly accept HTTP."
  allow_insecure=true
fi

prompt_value 'Forgejo username'
forgejo_username="$REPLY"
prompt_secret 'Forgejo token (read:user and write:repository scopes)'
forgejo_token="$REPLY"

printf '\nThis will install Python dependencies, place the app in %s, configure a system service, and enable its 15-minute timer.\n' "$INSTALL_DIR"
read -r -p 'Continue? [y/N] ' confirmation
[[ "$confirmation" =~ ^[Yy]$ ]] || fail "Installation cancelled."

install_system_dependencies
command -v python3 >/dev/null || fail "python3 was not installed successfully."

if ! getent passwd "$APP_NAME" >/dev/null; then
  useradd --system --user-group --home-dir "/var/lib/$APP_NAME" --create-home --shell /bin/false "$APP_NAME"
elif ! getent group "$APP_NAME" >/dev/null; then
  groupadd --system "$APP_NAME"
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -o root -g root -m 0644 "$SOURCE_DIR/github_forgejo_mirror.py" "$INSTALL_DIR/github_forgejo_mirror.py"
install -o root -g root -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

env_tmp="$(mktemp /etc/github-forgejo-mirror.env.XXXXXX)"
trap 'rm -f "$env_tmp"' EXIT
{
  printf 'GITHUB_USERNAME=%s\n' "$github_username"
  printf 'GITHUB_TOKEN=%s\n' "$github_token"
  printf 'FORGEJO_URL=%s\n' "$forgejo_url"
  printf 'ALLOW_INSECURE_FORGEJO=%s\n' "$allow_insecure"
  printf 'FORGEJO_USERNAME=%s\n' "$forgejo_username"
  printf 'FORGEJO_TOKEN=%s\n' "$forgejo_token"
} > "$env_tmp"
chown root:root "$env_tmp"
chmod 0600 "$env_tmp"
mv -f "$env_tmp" "$ENV_FILE"
trap - EXIT

install -o root -g root -m 0644 "$SOURCE_DIR/systemd/github-forgejo-mirror.service" "$SERVICE_FILE"
install -o root -g root -m 0644 "$SOURCE_DIR/systemd/github-forgejo-mirror.timer" "$TIMER_FILE"
systemctl daemon-reload
systemctl enable --now github-forgejo-mirror.timer

unset github_token forgejo_token
printf '\nInstallation complete. The timer runs five minutes after boot and every 15 minutes thereafter.\n'
printf 'Check status with: systemctl status github-forgejo-mirror.timer\n'
printf 'View logs with: journalctl -u github-forgejo-mirror.service -n 100 --no-pager\n'
