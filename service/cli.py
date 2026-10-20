import argparse
import getpass
import json

import httpx
import pyotp
from sqlalchemy import select, text

from service.db import Base, Identity, LocalCredential, ModelCall, Space, User, audit, factory
from service.security import hasher, validate_password, validate_username
from service.settings import config, secret


def migrate():
    engine = factory().kw['bind']
    with engine.begin() as connection:
        connection.execute(text('SELECT pg_advisory_xact_lock(58231001)'))
        Base.metadata.create_all(connection)
        connection.execute(text('CREATE TABLE IF NOT EXISTS platform_schema (version integer PRIMARY KEY)'))
        connection.execute(text('INSERT INTO platform_schema(version) VALUES (1) ON CONFLICT DO NOTHING'))
    print('Schema version 1 ready.')


def bootstrap(args):
    username = validate_username(args.username)
    password = getpass.getpass('Admin password (12+ characters): ')
    validate_password(password)
    otp = pyotp.random_base32()
    with factory()() as db:
        if db.scalar(select(User).where(User.admin.is_(True))):
            raise SystemExit('Administrator already exists; use recover-admin for password/OTP recovery.')
        user = User(username=username, email=args.email.lower(), admin=True, email_verified=True,
                    status='active', totp_secret=otp)
        db.add(user)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hasher.hash(password)))
        db.add(Identity(id='local:' + username, user_id=user.id, subject=username))
        db.add(Space(user_id=user.id))
        audit(db, 'operator', 'bootstrap_admin', user.id)
        db.commit()
    print('Add this account to an authenticator; keep the enrollment URI private:')
    print(pyotp.TOTP(otp).provisioning_uri(username, issuer_name='Hermes Central'))


def recover(args):
    from service.accounts import revoke_all
    password = getpass.getpass('New admin password: ')
    validate_password(password)
    otp = pyotp.random_base32()
    with factory()() as db:
        user = db.scalar(select(User).where(User.username == args.username, User.admin.is_(True)))
        if not user:
            raise SystemExit('Administrator not found')
        db.get(LocalCredential, user.id).password_hash = hasher.hash(password)
        user.totp_secret = otp
        revoke_all(db, user.id)
        audit(db, 'operator', 'recover_admin', user.id)
        db.commit()
    print(pyotp.TOTP(otp).provisioning_uri(args.username, issuer_name='Hermes Central'))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate')
    sub.add_parser('drain')
    sub.add_parser('reconcile-calls')
    for name in ('bootstrap-admin', 'recover-admin'):
        p = sub.add_parser(name)
        p.add_argument('--username', required=True)
        p.add_argument('--email', default='admin@localhost')
    args = parser.parse_args()
    if args.command == 'migrate':
        migrate()
    elif args.command == 'bootstrap-admin':
        bootstrap(args)
    elif args.command == 'recover-admin':
        recover(args)
    elif args.command == 'drain':
        response = httpx.post(config()['manager_url'] + '/drain', headers={'x-manager-key': secret('manager_key')},
                              timeout=120, trust_env=False)
        response.raise_for_status()
        print(json.dumps(response.json()))
    elif args.command == 'reconcile-calls':
        # Explicit operator recovery after a full drain. Reservations remain charged.
        from service.models import settle
        with factory()() as db:
            ids = list(db.scalars(select(ModelCall.id).where(ModelCall.state == 'running')))
        for call_id in ids:
            settle(call_id, failed=True)
        print(f'Reconciled {len(ids)} interrupted calls; reservations retained.')


if __name__ == '__main__':
    main()
