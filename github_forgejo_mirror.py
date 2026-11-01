#!/usr/bin/env python3
"""Discover personal GitHub repositories and create Forgejo pull mirrors.

The program is intentionally one-shot and idempotent. Schedule it with systemd
(or another scheduler); Forgejo itself performs the ongoing Git synchronization.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("github-forgejo-mirror")
GITHUB_API_VERSION = "2026-03-10"


class ConfigurationError(Exception):
    pass


class APIError(Exception):
    def __init__(self, service: str, method: str, url: str, status: int,
                 message: str) -> None:
        self.service = service
        self.method = method
        self.url = url
        self.status = status
        self.message = message
        super().__init__(f"{service} API {method} {url} returned HTTP {status}: {message}")


@dataclass(frozen=True)
class Config:
    github_token: str
    github_username: str
    github_api_url: str
    forgejo_url: str
    forgejo_token: str
    forgejo_username: str
    mirror_interval: str
    mirror_visibility: str
    mirror_lfs: bool
    request_timeout: int
    migration_timeout: int
    state_dir: Path
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ConfigurationError(f"Missing required environment variable: {name}")
            return value

        def boolean(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            value = raw.strip().lower()
            if value in {"1", "true", "yes", "on"}:
                return True
            if value in {"0", "false", "no", "off"}:
                return False
            raise ConfigurationError(f"{name} must be true/false, 1/0, yes/no, or on/off")

        def positive_int(name: str, default: int) -> int:
            raw = os.environ.get(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be an integer") from exc
            if value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
            return value

        github_api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/")
        forgejo_url = required("FORGEJO_URL").rstrip("/")
        if urlparse(github_api_url).scheme != "https" or not urlparse(github_api_url).netloc:
            raise ConfigurationError("GITHUB_API_URL must be an HTTPS URL")
        if urlparse(forgejo_url).scheme not in {"https", "http"} or not urlparse(forgejo_url).netloc:
            raise ConfigurationError("FORGEJO_URL must be an absolute HTTP(S) URL")
        if urlparse(forgejo_url).scheme != "https" and os.environ.get("ALLOW_INSECURE_FORGEJO", "false").lower() not in {"1", "true", "yes", "on"}:
            raise ConfigurationError("FORGEJO_URL must use HTTPS (set ALLOW_INSECURE_FORGEJO=true only for a trusted private network)")

        mirror_interval = os.environ.get("MIRROR_INTERVAL", "15m").strip()
        # Go duration syntax, intentionally restricted to positive h/m/s components.
        parts = re.findall(r"(\d+)(h|m|s)", mirror_interval)
        if not parts or "".join(n + unit for n, unit in parts) != mirror_interval:
            raise ConfigurationError("MIRROR_INTERVAL must use a duration such as 15m, 1h, or 1h30m")
        if sum(int(n) * {"h": 3600, "m": 60, "s": 1}[unit] for n, unit in parts) <= 0:
            raise ConfigurationError("MIRROR_INTERVAL must be greater than zero")

        visibility = os.environ.get("MIRROR_VISIBILITY", "private").strip().lower()
        if visibility not in {"private", "preserve"}:
            raise ConfigurationError("MIRROR_VISIBILITY must be 'private' or 'preserve'")

        state_dir = Path(os.environ.get(
            "STATE_DIR", str(Path.home() / ".local" / "state" / "github-forgejo-mirror")
        )).expanduser()

        return cls(
            github_token=required("GITHUB_TOKEN"),
            github_username=required("GITHUB_USERNAME"),
            github_api_url=github_api_url,
            forgejo_url=forgejo_url,
            forgejo_token=required("FORGEJO_TOKEN"),
            forgejo_username=required("FORGEJO_USERNAME"),
            mirror_interval=mirror_interval,
            mirror_visibility=visibility,
            mirror_lfs=boolean("MIRROR_LFS", True),
            request_timeout=positive_int("REQUEST_TIMEOUT_SECONDS", 30),
            migration_timeout=positive_int("MIGRATION_TIMEOUT_SECONDS", 3600),
            state_dir=state_dir,
            dry_run=boolean("DRY_RUN", False),
        )


class APIClient:
    """Small JSON API client; automatic retries are deliberately GET-only."""

    def __init__(self, service: str, base_url: str, token: str,
                 auth_scheme: str, headers: dict[str, str] | None = None,
                 timeout: int = 30) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"{auth_scheme} {token}",
            "User-Agent": "github-forgejo-mirror/1.0",
        })
        if headers:
            self.session.headers.update(headers)

        retry = Retry(
            total=5,
            connect=5,
            read=3,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def request(self, method: str, path_or_url: str, *, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None, timeout: int | tuple[int, int] | None = None,
                allowed_statuses: tuple[int, ...] = (200,)) -> requests.Response:
        if path_or_url.startswith("https://") or path_or_url.startswith("http://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=body,
                timeout=timeout or self.timeout,
                allow_redirects=(method.upper() == "GET"),
            )
        except requests.RequestException as exc:
            # Do not include request headers or request bodies in logs/errors.
            raise RuntimeError(f"{self.service} request failed ({method} {url}): {exc.__class__.__name__}: {exc}") from exc

        if response.status_code not in allowed_statuses:
            try:
                data = response.json()
                message = data.get("message") or data.get("error") or str(data)
            except (ValueError, AttributeError):
                message = response.text[:500] or response.reason
            raise APIError(self.service, method, response.url, response.status_code, str(message)[:500])
        return response

    def get_json(self, path: str) -> dict[str, Any]:
        response = self.request("GET", path)
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.service} returned invalid JSON for {path}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"{self.service} returned an unexpected JSON response for {path}")
        return result


class GitHubClient:
    def __init__(self, config: Config) -> None:
        self.api = APIClient(
            "GitHub", config.github_api_url, config.github_token, "Bearer",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            timeout=config.request_timeout,
        )

    def verify_account(self, expected_login: str) -> None:
        user = self.api.get_json("/user")
        actual = str(user.get("login", ""))
        if actual.casefold() != expected_login.casefold():
            raise ConfigurationError(
                f"GITHUB_USERNAME is '{expected_login}', but GITHUB_TOKEN belongs to '{actual or 'unknown'}'"
            )
        LOG.info("GitHub token verified for personal account %s", actual)

    def list_owned_repositories(self, expected_login: str) -> list[dict[str, Any]]:
        url = f"{self.api.base_url}/user/repos"
        params: dict[str, Any] | None = {
            "visibility": "all",
            "affiliation": "owner",
            "per_page": 100,
            "sort": "full_name",
            "direction": "asc",
        }
        expected_host = urlparse(self.api.base_url).netloc
        repositories: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        while url:
            if url in seen_urls:
                raise RuntimeError("GitHub pagination returned a repeated page URL")
            seen_urls.add(url)
            response = self.api.request("GET", url, params=params)
            params = None  # Subsequent URLs already contain their page query parameters.
            try:
                page = response.json()
            except ValueError as exc:
                raise RuntimeError("GitHub returned invalid JSON while listing repositories") from exc
            if not isinstance(page, list):
                raise RuntimeError("GitHub returned an unexpected repository-list response")

            for repo in page:
                if not isinstance(repo, dict):
                    continue
                owner = repo.get("owner") or {}
                if (
                    str(owner.get("login", "")).casefold() == expected_login.casefold()
                    and str(owner.get("type", "")).casefold() == "user"
                ):
                    repositories.append(repo)

            next_link = response.links.get("next", {}).get("url")
            if next_link:
                parsed = urlparse(next_link)
                if parsed.scheme != "https" or parsed.netloc != expected_host:
                    raise RuntimeError("Refusing to send GitHub credentials to an unexpected pagination host")
                url = next_link
            else:
                url = ""

        # Deduplicate defensively in case the upstream API returns overlap across pages.
        by_id: dict[int | str, dict[str, Any]] = {}
        for repo in repositories:
            repo_id = repo.get("id") or repo.get("full_name")
            by_id[repo_id] = repo
        result = sorted(by_id.values(), key=lambda item: str(item.get("name", "")).casefold())
        LOG.info("Discovered %d personal repositories owned by %s (archived repositories included)", len(result), expected_login)
        return result


class ForgejoClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = APIClient(
            "Forgejo", f"{config.forgejo_url}/api/v1", config.forgejo_token,
            "token", timeout=config.request_timeout,
        )

    def verify_account(self, expected_login: str) -> None:
        user = self.api.get_json("/user")
        actual = str(user.get("login", ""))
        if actual.casefold() != expected_login.casefold():
            raise ConfigurationError(
                f"FORGEJO_USERNAME is '{expected_login}', but FORGEJO_TOKEN belongs to '{actual or 'unknown'}'"
            )
        LOG.info("Forgejo token verified for account %s", actual)

    def get_repository(self, owner: str, name: str) -> dict[str, Any] | None:
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            response = self.api.request("GET", path, allowed_statuses=(200, 404))
        except APIError:
            raise
        if response.status_code == 404:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Forgejo returned invalid JSON while checking {owner}/{name}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Forgejo returned an unexpected response while checking {owner}/{name}")
        return data

    def update_mirror_interval(self, owner: str, name: str, interval: str) -> None:
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        self.api.request("PATCH", path, body={"mirror_interval": interval}, allowed_statuses=(200,))

    def create_pull_mirror(self, source: dict[str, Any]) -> dict[str, Any]:
        name = str(source.get("name", "")).strip()
        clone_url = str(source.get("clone_url", "")).strip()
        if not name or not clone_url:
            raise RuntimeError(f"GitHub repository is missing its name or clone_url: {source.get('full_name', '<unknown>')}")

        is_private = bool(source.get("private", False))
        target_private = True if self.config.mirror_visibility == "private" else is_private
        body: dict[str, Any] = {
            "clone_addr": clone_url,
            "repo_owner": self.config.forgejo_username,
            "repo_name": name,
            "service": "github",
            "mirror": True,
            "mirror_interval": self.config.mirror_interval,
            "private": target_private,
            "description": str(source.get("description") or ""),
            "lfs": self.config.mirror_lfs,
            # This is a Git repository mirror, not a full GitHub issue/PR migration.
            "issues": False,
            "labels": False,
            "milestones": False,
            "pull_requests": False,
            "releases": False,
            "wiki": False,
        }
        # Public repositories do not need credentials for the initial clone or later pulls.
        # Private repositories require the read-only GitHub token for every pull.
        if is_private:
            body["auth_token"] = self.config.github_token

        response = self.api.request(
            "POST", "/repos/migrate", body=body,
            timeout=(min(self.config.request_timeout, 30), self.config.migration_timeout),
            allowed_statuses=(201, 202),
        )
        try:
            result = response.json()
        except ValueError:
            result = {}
        return result if isinstance(result, dict) else {}


def normalize_git_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.casefold()


def duration_seconds(value: str) -> int | None:
    """Parse the Go-style duration format used by Forgejo mirror settings."""
    value = value.strip()
    parts = re.findall(r"(\d+)(h|m|s)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        return None
    return sum(int(number) * {"h": 3600, "m": 60, "s": 1}[unit] for number, unit in parts)


def acquire_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    lock_path = state_dir / "sync.lock"
    lock_file = lock_path.open("a+")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def run(config: Config) -> int:
    lock_file = acquire_lock(config.state_dir)
    if lock_file is None:
        LOG.info("Another synchronization run is active; exiting without changes")
        return 0

    try:
        github = GitHubClient(config)
        forgejo = ForgejoClient(config)
        github.verify_account(config.github_username)
        forgejo.verify_account(config.forgejo_username)
        repositories = github.list_owned_repositories(config.github_username)

        created = 0
        already_mirrored = 0
        updated = 0
        conflicts = 0
        failed = 0

        for repo in repositories:
            name = str(repo.get("name", ""))
            full_name = str(repo.get("full_name") or f"{config.github_username}/{name}")
            clone_url = str(repo.get("clone_url") or f"https://github.com/{config.github_username}/{name}.git")
            try:
                existing = forgejo.get_repository(config.forgejo_username, name)
                if existing is not None:
                    if existing.get("mirror") is True:
                        original_url = str(existing.get("original_url") or "")
                        if original_url and normalize_git_url(original_url) == normalize_git_url(clone_url):
                            current_interval = str(existing.get("mirror_interval") or "")
                            if duration_seconds(current_interval) != duration_seconds(config.mirror_interval):
                                if config.dry_run:
                                    LOG.info(
                                        "DRY RUN would update mirror interval for %s: %s -> %s",
                                        full_name, current_interval or "unset", config.mirror_interval,
                                    )
                                else:
                                    forgejo.update_mirror_interval(
                                        config.forgejo_username, name, config.mirror_interval
                                    )
                                    updated += 1
                                    LOG.info(
                                        "Updated mirror interval for %s: %s -> %s",
                                        full_name, current_interval or "unset", config.mirror_interval,
                                    )
                            else:
                                LOG.info("OK already mirrored with the configured interval: %s", full_name)
                        else:
                            LOG.warning(
                                "SKIP existing mirror with unverified/different source: %s (Forgejo original_url=%r)",
                                full_name, original_url or "not reported",
                            )
                            conflicts += 1
                    else:
                        LOG.warning("SKIP name conflict: %s already exists on Forgejo and is not a pull mirror", full_name)
                        conflicts += 1
                    already_mirrored += 1
                    continue

                target_visibility = "private" if config.mirror_visibility == "private" else ("private" if repo.get("private") else "public")
                if config.dry_run:
                    LOG.info("DRY RUN would create %s as %s pull mirror (%s)", full_name, target_visibility, config.mirror_interval)
                    continue

                LOG.info("Creating pull mirror: %s", full_name)
                forgejo.create_pull_mirror(repo)
                created += 1
                LOG.info("Created pull mirror: %s", full_name)
            except APIError as exc:
                # A concurrent/manual creation can race with our existence check.
                if exc.status in (409, 422):
                    try:
                        now_exists = forgejo.get_repository(config.forgejo_username, name)
                    except Exception:
                        now_exists = None
                    if now_exists is not None:
                        LOG.warning("SKIP repository created concurrently or already present: %s", full_name)
                        conflicts += 1
                        continue
                failed += 1
                LOG.error("Failed %s: %s", full_name, exc)
            except Exception as exc:  # Continue attempting other repositories.
                failed += 1
                LOG.exception("Failed %s: %s", full_name, exc)

        LOG.info(
            "Finished: discovered=%d created=%d existing=%d conflicts=%d interval_updated=%d failed=%d dry_run=%s",
            len(repositories), created, already_mirrored, conflicts, updated, failed, config.dry_run,
        )
        return 1 if failed else 0
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and maintain personal GitHub → Forgejo pull mirrors")
    parser.add_argument("--dry-run", action="store_true", help="Do not create repositories; report what would be created")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        config = Config.from_env()
        if args.dry_run:
            config = replace(config, dry_run=True)
        return run(config)
    except ConfigurationError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    except Exception as exc:
        LOG.exception("Fatal synchronization error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
