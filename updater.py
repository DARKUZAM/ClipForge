from __future__ import annotations

import json
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote


class GitHubUpdater:
    """Fetch and apply published GitHub branch updates without touching user data."""

    # An update may replace only application code and shipped UI assets.
    ROOT_FILES = {'app.py', 'launch.py', 'live_streams.py', 'updater.py', 'README.md', 'requirements.txt', 'VERSION', 'Run ClipForge.bat'}
    MAX_ARCHIVE_BYTES = 80 * 1024 * 1024

    def __init__(self, app_root: Path, data_dir: Path, read_config):
        self.app_root = app_root.resolve()
        self.data_dir = data_dir
        self.read_config = read_config
        self.state_file = data_dir / 'updates' / 'installed_commit.json'

    def _repo(self, repo: str | None = None) -> str:
        repo = (repo or self.read_config().get('update_repo') or '').strip().strip('/')
        parts=repo.split('/')
        if len(parts) != 2 or any(not part or part in ('.', '..') or not part.replace('-', '').replace('_', '').replace('.', '').isalnum() for part in parts):
            raise ValueError('Enter your public GitHub repository as owner/name, then save settings.')
        return repo

    @staticmethod
    def _get_json(url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'ClipForge-Desktop-Updater',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError('GitHub repository or branch was not found. Check owner/name and make sure it is public.') from exc
            raise RuntimeError(f'GitHub returned HTTP {exc.code}. Try again later.') from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'Could not check GitHub for updates: {exc}') from exc

    def check(self, repo: str | None = None) -> dict[str, Any]:
        repo = self._repo(repo)
        base = f'https://api.github.com/repos/{repo}'
        metadata = self._get_json(base)
        branch = str(metadata.get('default_branch') or 'main')
        head = self._get_json(f'{base}/commits/{quote(branch, safe="")}')
        sha = str(head.get('sha') or '')
        if len(sha) != 40:
            raise RuntimeError('GitHub did not return a valid commit for the default branch.')
        installed = ''
        try:
            installed = str(json.loads(self.state_file.read_text('utf-8')).get('sha') or '')
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return {
            'repo': repo,
            'branch': branch,
            'current_commit': installed or 'not linked to a GitHub build yet',
            'latest_commit': sha,
            'message': str(head.get('commit', {}).get('message', '')).splitlines()[0][:180],
            'url': str(head.get('html_url') or f'https://github.com/{repo}/commit/{sha}'),
            'update_available': bool(installed and installed != sha),
            'first_sync': not bool(installed),
        }

    def apply(self, repo: str | None = None) -> dict[str, Any]:
        info = self.check(repo)
        if not info['update_available'] and not info['first_sync']:
            return {'updated': False, 'message': 'ClipForge is already up to date.', **info}
        sha = info['latest_commit']
        archive_url = f'https://api.github.com/repos/{info["repo"]}/zipball/{sha}'
        req = urllib.request.Request(archive_url, headers={'User-Agent': 'ClipForge-Desktop-Updater'})
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                archive = response.read(self.MAX_ARCHIVE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f'Could not download the GitHub update: {exc}') from exc
        if len(archive) > self.MAX_ARCHIVE_BYTES:
            raise RuntimeError('The GitHub source archive is unexpectedly large; update was cancelled.')

        update_root = self.data_dir / 'updates'
        update_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        backup = update_root / 'backups' / timestamp
        staged: dict[Path, bytes] = {}
        try:
            with tempfile.TemporaryDirectory(prefix='clipforge-update-') as temp:
                zip_path = Path(temp) / 'source.zip'
                zip_path.write_bytes(archive)
                with zipfile.ZipFile(zip_path) as zipped:
                    members = [m for m in zipped.infolist() if not m.is_dir()]
                    if sum(m.file_size for m in members) > self.MAX_ARCHIVE_BYTES:
                        raise RuntimeError('The GitHub update expands beyond the allowed source size.')
                    for member in members:
                        parts = PurePosixPath(member.filename).parts
                        if len(parts) < 2 or '..' in parts or parts[0].startswith('.'):
                            continue
                        relative = Path(*parts[1:])
                        if not (relative.parts[0] in self.ROOT_FILES or relative.parts[0] == 'static'):
                            continue
                        if relative.parts[0] == 'static' and len(relative.parts) < 2:
                            continue
                        target = (self.app_root / relative).resolve()
                        if self.app_root not in target.parents:
                            continue
                        staged[target] = zipped.read(member)
            if 'VERSION' not in {p.name for p in staged} or 'app.py' not in {p.name for p in staged}:
                raise RuntimeError('The GitHub branch does not contain a ClipForge source build.')
            backup.mkdir(parents=True, exist_ok=True)
            for target, content in staged.items():
                if target.is_file():
                    relative = target.relative_to(self.app_root)
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
            for target, content in staged.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temp_target = target.with_name(target.name + '.clipforge-update')
                temp_target.write_bytes(content)
                temp_target.replace(target)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state_tmp = self.state_file.with_suffix('.tmp')
            state_tmp.write_text(json.dumps({'sha': sha, 'repo': info['repo'], 'branch': info['branch']}, indent=2), 'utf-8')
            state_tmp.replace(self.state_file)
        except Exception:
            # Restore application files on a failed install. User config and
            # generated projects are outside the update whitelist entirely.
            if backup.exists():
                for saved in backup.rglob('*'):
                    if saved.is_file():
                        target = self.app_root / saved.relative_to(backup)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(saved, target)
            raise
        return {'updated': True, 'message': 'Update installed. Close and reopen ClipForge to load the new version.', **info}
