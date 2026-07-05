#!/usr/bin/env python3
"""CLI management tool for user administration.

Usage:
    python src/manage.py create-admin --username admin --email admin@example.com
    python src/manage.py generate-invite [--count 5] [--max-uses 1]
"""
import argparse
import getpass
import os
import secrets
import string
import sys
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'src'))

import db


def generate_invite_code():
    """Generate an 8-character invite code (uppercase letters + digits)."""
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


def cmd_create_admin(args):
    """Create the first admin user via CLI."""
    import bcrypt as _bcrypt

    password = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        print("Error: Password must be at least 8 characters")
        sys.exit(1)
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: Passwords do not match")
        sys.exit(1)

    conn = db.get_conn()

    # Check if username or email already exists
    existing = db.get_user_by_username(conn, args.username)
    if existing:
        print(f"Error: Username '{args.username}' already exists")
        conn.close()
        sys.exit(1)
    if args.email:
        existing = db.get_user_by_email(conn, args.email)
        if existing:
            print(f"Error: Email '{args.email}' already exists")
            conn.close()
            sys.exit(1)

    user_id = str(uuid.uuid4())
    password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=12)).decode()

    db.create_user(conn, user_id, args.username, args.email, password_hash, role='admin')

    # Migrate item_status to composite PK, assigning existing data to this admin
    db.migrate_item_status_add_user_id(conn, user_id)

    conn.close()

    print(f"Admin user created:")
    print(f"  ID:       {user_id}")
    print(f"  Username: {args.username}")
    print(f"  Email:    {args.email}")
    print(f"  Role:     admin")


def cmd_generate_invite(args):
    """Generate invite codes."""
    conn = db.get_conn()

    # Find an admin user as creator
    admins = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    if not admins:
        print("Error: No admin user found. Create one first with: manage.py create-admin")
        conn.close()
        sys.exit(1)

    admin_id = admins[0]
    codes = []
    for _ in range(args.count):
        code = generate_invite_code()
        db.create_invite_code(conn, code, admin_id, max_uses=args.max_uses)
        codes.append(code)

    conn.close()

    print(f"Generated {args.count} invite code(s) (max {args.max_uses} use(s) each):")
    for code in codes:
        print(f"  {code}")


def main():
    parser = argparse.ArgumentParser(description='Info2Action Management CLI')
    sub = parser.add_subparsers(dest='command')

    # create-admin
    p_admin = sub.add_parser('create-admin', help='Create an admin user')
    p_admin.add_argument('--username', required=True)
    p_admin.add_argument('--email', required=True)

    # generate-invite
    p_invite = sub.add_parser('generate-invite', help='Generate invite codes')
    p_invite.add_argument('--count', type=int, default=1)
    p_invite.add_argument('--max-uses', type=int, default=1)

    args = parser.parse_args()
    if args.command == 'create-admin':
        cmd_create_admin(args)
    elif args.command == 'generate-invite':
        cmd_generate_invite(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
