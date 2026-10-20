"""Destructive restore test, refuses to run unless the local account DB is empty."""
import json
import uuid

import docker
from sqlalchemy import delete, func, select

from service.backup import LABEL, OWNER, DEPLOY, backup, helper, restore
from service.db import Space, User, factory


def main():
    cli = docker.from_env(timeout=120)
    with factory()() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise SystemExit('Storage smoke refuses a non-empty account DB. Use a disposable deployment.')
        user = User(username='storage' + uuid.uuid4().hex[:12], email=uuid.uuid4().hex + '@test.invalid',
                    status='pending', email_verified=False)
        db.add(user)
        db.flush()
        owner = user.id
        db.add(Space(user_id=owner))
        db.commit()
    name = 'hc-user-' + owner + '-data'
    volume = cli.volumes.create(name, labels={LABEL: DEPLOY, OWNER: owner})
    container = helper(cli, name, readonly=False)
    result = container.exec_run(['/opt/hermes/.venv/bin/python', '-c',
        "import sqlite3,pathlib; p=pathlib.Path('/data'); (p/'marker').write_text('durable'); c=sqlite3.connect('/data/state.db'); c.execute('CREATE TABLE check_data(value TEXT)'); c.execute(\"INSERT INTO check_data VALUES ('durable')\"); c.commit(); c.close()"])
    if result.exit_code:
        raise RuntimeError('Fixture initialization failed')
    container.remove(force=True)
    from pathlib import Path
    root = Path('/backups') / ('smoke-' + uuid.uuid4().hex[:12])
    try:
        backup(cli, root)
        volume.remove()
        with factory()() as db:
            db.execute(delete(Space).where(Space.user_id == owner))
            db.execute(delete(User).where(User.id == owner))
            db.commit()
        restore(cli, root)
        container = helper(cli, name)
        try:
            result = container.exec_run(['/opt/hermes/.venv/bin/python', '-c',
                "import sqlite3,pathlib; assert pathlib.Path('/data/marker').read_text()=='durable'; assert sqlite3.connect('/data/state.db').execute('SELECT value FROM check_data').fetchone()[0]=='durable'"])
            assert result.exit_code == 0, 'Restored data mismatch'
        finally:
            container.remove(force=True)
        with factory()() as db:
            assert db.get(User, owner).username.startswith('storage')
        print('PASS: stopped-volume archive, PostgreSQL dump, empty-target restore, exact file and SQLite content.')
    finally:
        try:
            cli.volumes.get(name).remove()
        except docker.errors.NotFound:
            pass
        with factory()() as db:
            db.execute(delete(Space).where(Space.user_id == owner))
            db.execute(delete(User).where(User.id == owner))
            db.commit()
    print('Storage fixture removed; test backup retained under artifacts/backups (gitignored).')


if __name__ == '__main__':
    main()
