"""Offline, labelled-volume backup/restore. Runs only in the operator Compose profile."""
import argparse
import hashlib
import json
import re
import shutil
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath

import docker
import httpx

from service.settings import config, secret

DEPLOY = 'hermes-custom'
LABEL = 'hermes.custom.deployment'
OWNER = 'hermes.custom.owner'
NAME = re.compile(r'hc-user-[0-9a-f-]{36}-data')


def checksum(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def service(cli, name):
    matches = cli.containers.list(all=True, filters={'label': ['com.docker.compose.project=' + DEPLOY,
                                                              'com.docker.compose.service=' + name]})
    if len(matches) != 1:
        raise RuntimeError(f'Expected one {name} container')
    return matches[0]


def get_file(container, source, dest):
    staging = dest.with_suffix('.transport.tar')
    stream, _ = container.get_archive(source)
    with staging.open('wb') as out:
        for chunk in stream:
            out.write(chunk)
    try:
        with tarfile.open(staging) as archive, dest.open('wb') as out:
            with archive.extractfile(PurePosixPath(source).name) as incoming:
                shutil.copyfileobj(incoming, out)
    finally:
        staging.unlink()


def helper(cli, volume, readonly=True):
    return cli.containers.run(config()['runtime_image'], ['infinity'], entrypoint='/bin/sleep',
        user='0:0', detach=True, network_disabled=True, read_only=True, cap_drop=['ALL'],
        volumes={volume: {'bind': '/data', 'mode': 'ro' if readonly else 'rw'}},
        labels={'hermes.custom.backup-helper': 'true'})


def quiesce(cli):
    wait_ready(config()['manager_url'] + '/healthz')
    response = httpx.post(config()['manager_url'] + '/drain', headers={'x-manager-key': secret('manager_key')},
                          timeout=120, trust_env=False)
    response.raise_for_status()
    stopped = []
    try:
        for name in ('edge', 'platform', 'manager'):
            container = service(cli, name)
            if container.status == 'running':
                container.stop(timeout=10)
                stopped.append(container)
    except Exception:
        restart(stopped)
        raise
    return stopped


def restart(stopped):
    for container in reversed(stopped):
        container.start()
    for container in reversed(stopped):
        name = container.labels.get('com.docker.compose.service')
        if name == 'manager':
            wait_ready(config()['manager_url'] + '/healthz')
        elif name == 'platform':
            wait_ready('http://platform:8000/healthz')


def wait_ready(url):
    """Container start is not service readiness; required before an immediate restore/drain."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        threading.Event().wait(0.25)
    raise RuntimeError('Service did not become ready after restart')


def backup(cli, root):
    root.mkdir(parents=True, exist_ok=False)
    stopped = quiesce(cli)
    try:
        db = service(cli, 'db')
        result = db.exec_run(['pg_dump', '-U', 'hermes', '-d', 'hermes', '-Fc', '-f', '/tmp/hc-backup.dump'])
        if result.exit_code:
            raise RuntimeError('pg_dump failed')
        get_file(db, '/tmp/hc-backup.dump', root / 'platform.dump')
        volumes = cli.volumes.list(filters={'label': LABEL + '=' + DEPLOY})
        entries = []
        for volume in volumes:
            if not NAME.fullmatch(volume.name):
                raise RuntimeError('Unexpected service-owned volume name')
            labels = volume.attrs.get('Labels') or {}
            owner = labels.get(OWNER)
            if volume.name != 'hc-user-' + str(owner) + '-data':
                raise RuntimeError('Volume owner mismatch')
            container = helper(cli, volume.name)
            try:
                stream, _ = container.get_archive('/data')
                filename = volume.name + '.tar'
                with (root / filename).open('wb') as out:
                    for chunk in stream:
                        out.write(chunk)
                entries.append({'name': volume.name, 'owner': owner, 'file': filename})
            finally:
                container.remove(force=True)
        files = ['platform.dump'] + [e['file'] for e in entries]
        manifest = {'version': 1, 'volumes': entries, 'sha256': {name: checksum(root / name) for name in files},
                    'secrets': 'Restore with the original .secrets files, kept in a separate secure backup.'}
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        print(f'Backup complete: {root.name}; {len(entries)} user volumes. Secrets are not included.')
    finally:
        restart(stopped)


def verify(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest['version'] != 1:
        raise RuntimeError('Unsupported backup version')
    for name, expected in manifest['sha256'].items():
        if PurePosixPath(name).name != name or not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
            raise RuntimeError('Invalid backup path')
        if checksum(root / name) != expected:
            raise RuntimeError('Backup checksum mismatch: ' + name)
    return manifest


def restore(cli, root):
    manifest = verify(root)
    if any((v.attrs.get('Labels') or {}).get(LABEL) == DEPLOY for v in cli.volumes.list()):
        raise RuntimeError('Restore requires an empty target: tenant volumes already exist')
    stopped = quiesce(cli)
    created = []
    try:
        db = service(cli, 'db')
        result = db.exec_run(['psql', '-U', 'hermes', '-d', 'hermes', '-Atc', 'SELECT count(*) FROM users'])
        if result.exit_code or result.output.strip() != b'0':
            raise RuntimeError('Restore refused: target contains users or is not initialized')
        for entry in manifest['volumes']:
            name, owner = entry['name'], entry['owner']
            if not NAME.fullmatch(name) or name != 'hc-user-' + owner + '-data' or entry['file'] != name + '.tar':
                raise RuntimeError('Invalid volume manifest')
            if entry['file'] not in manifest['sha256']:
                raise RuntimeError('Unverified volume file')
            archive_path = root / entry['file']
            with tarfile.open(archive_path) as archive:
                for member in archive:
                    path = PurePosixPath(member.name)
                    if not path.parts or path.parts[0] != 'data' or '..' in path.parts or member.isdev():
                        raise RuntimeError('Unsafe archive path')
                    if (member.issym() or member.islnk()) and (member.linkname.startswith('/') or '..' in PurePosixPath(member.linkname).parts):
                        raise RuntimeError('External archive link is not supported')
            # A pre-existing foreign volume of the same name is never adopted or overwritten.
            try:
                cli.volumes.get(name)
            except docker.errors.NotFound:
                pass
            else:
                raise RuntimeError('Volume name collision')
            volume = cli.volumes.create(name, labels={LABEL: DEPLOY, OWNER: owner})
            created.append(volume)
            container = helper(cli, name, readonly=False)
            try:
                # Docker refuses extraction against / on a read-only rootfs, even for a nested mount.
                # Rebase the validated data/ archive and write directly into the writable volume.
                import copy
                import tempfile
                with tempfile.TemporaryFile() as stream:
                    with tarfile.open(archive_path) as source, tarfile.open(fileobj=stream, mode='w') as target:
                        for member in source:
                            relative = PurePosixPath(member.name).parts[1:]
                            if not relative:
                                continue
                            entry = copy.copy(member)
                            entry.name = str(PurePosixPath(*relative))
                            if entry.islnk() and entry.linkname.startswith('data/'):
                                entry.linkname = entry.linkname[5:]
                            target.addfile(entry, source.extractfile(member) if member.isfile() else None)
                    stream.seek(0)
                    container.put_archive('/data', stream)
            finally:
                container.remove(force=True)
        # put_archive places only this operator-provided DB dump into a private /tmp path.
        import io
        with io.BytesIO() as buffer:
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                archive.add(root / 'platform.dump', arcname='hc-restore.dump')
            db.put_archive('/tmp', buffer.getvalue())
        result = db.exec_run(['pg_restore', '-U', 'hermes', '-d', 'hermes', '--single-transaction', '--clean', '--if-exists', '/tmp/hc-restore.dump'])
        if result.exit_code:
            raise RuntimeError('pg_restore failed; transaction rolled back')
        print(f'Restored metadata and {len(created)} volumes. Runtimes remain stopped until explicitly started.')
    except Exception:
        for volume in created:
            volume.remove()
        raise
    finally:
        restart(stopped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['backup', 'verify', 'restore'])
    parser.add_argument('name', help='Directory name under /backups, no path separators')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', args.name):
        raise SystemExit('Use a simple backup directory name')
    root = Path('/backups') / args.name
    if args.action == 'verify':
        verify(root)
        print('Checksums verified.')
    else:
        cli = docker.from_env(timeout=120)
        (backup if args.action == 'backup' else restore)(cli, root)


if __name__ == '__main__':
    main()
