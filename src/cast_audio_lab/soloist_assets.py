# SPDX-License-Identifier: MPL-2.0
"""Install the user's official Soloist copy into private runtime state.

The project never redistributes Spotify binaries. HTTPS downloads are obtained
from Spotify on the user's machine; hashes record the exact local artifact.
"""
import asyncio
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tarfile
import tempfile
import time
from urllib.request import urlopen
from urllib.parse import urlsplit

from .routes import atomic_json

HOST = 'soloist-builds.spotifycdn.com'
MAX_ARCHIVE = 96 * 1024 * 1024
REFRESH_AGE = 75 * 86400


def download_url(architecture=None):
    architecture = architecture or platform.machine().lower()
    asset = {'aarch64': 'arm64', 'arm64': 'arm64',
             'x86_64': 'x86_64', 'amd64': 'x86_64'}.get(architecture)
    if not asset:
        raise ValueError('SOLOIST_ARCHITECTURE_UNSUPPORTED')
    return f'https://{HOST}/soloist_release_{asset}.tar.gz'


def build_timestamp(executable):
    result = subprocess.run([str(executable), '--version'], capture_output=True,
                            timeout=8, check=True, env={'PATH': os.defpath, 'LANG': 'C'})
    match = re.search(rb'\bbuild (\d{10})\b', result.stdout[:2048])
    if not match:
        raise ValueError('SOLOIST_VERSION_UNRECOGNIZED')
    stamp = int(match[1])
    if stamp > time.time() + 86400:
        raise ValueError('SOLOIST_BUILD_DATE_INVALID')
    return stamp


def _install(directory, *, architecture=None, opener=urlopen):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('SOLOIST_PRIVATE_DIRECTORY_REQUIRED')
    lock_fd = os.open(directory / '.install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 100
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('SOLOIST_INSTALL_BUSY') from None
                time.sleep(.1)
        executable = directory / 'soloist'
        if executable.exists() and not executable.is_symlink():
            try:
                if time.time() - build_timestamp(executable) < REFRESH_AGE:
                    return executable
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        url = download_url(architecture)
        with opener(url, timeout=20) as response:
            if urlsplit(response.geturl()).scheme != 'https' or urlsplit(response.geturl()).hostname != HOST:
                raise ValueError('SOLOIST_DOWNLOAD_ORIGIN_INVALID')
            payload = bytearray()
            while chunk := response.read(65536):
                payload.extend(chunk)
                if len(payload) > MAX_ARCHIVE or time.monotonic() > deadline:
                    raise ValueError('SOLOIST_DOWNLOAD_LIMIT_EXCEEDED')
        with tempfile.TemporaryDirectory(prefix='.install-', dir=directory) as temporary:
            staging = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
                selected = {}
                for member in archive.getmembers():
                    if member.name not in {'soloist', 'CHANGELOG.md', 'THIRD_PARTY_LICENSES.txt'}:
                        raise ValueError('SOLOIST_ARCHIVE_CONTENTS_INVALID')
                    if not member.isfile() or member.name in selected or member.size > MAX_ARCHIVE:
                        raise ValueError('SOLOIST_ARCHIVE_CONTENTS_INVALID')
                    selected[member.name] = member
                if 'soloist' not in selected:
                    raise ValueError('SOLOIST_EXECUTABLE_MISSING')
                for name, member in selected.items():
                    data = archive.extractfile(member)
                    if data is None:
                        raise ValueError('SOLOIST_ARCHIVE_CONTENTS_INVALID')
                    (staging / name).write_bytes(data.read(MAX_ARCHIVE + 1))
            candidate = staging / 'soloist'
            candidate.chmod(0o700)
            timestamp = build_timestamp(candidate)
            if time.time() - timestamp >= 90 * 86400:
                raise ValueError('SOLOIST_DOWNLOADED_BUILD_EXPIRED')
            record = {'version': 1, 'url': url, 'downloaded_at': int(time.time()),
                      'build_timestamp': timestamp, 'sha256': hashlib.sha256(payload).hexdigest()}
            for name in selected:
                os.replace(staging / name, directory / name)
            atomic_json(directory / 'source.json', record)
        return executable
    finally:
        os.close(lock_fd)


async def ensure_soloist(directory):
    return await asyncio.to_thread(_install, directory)
