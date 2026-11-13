"""Generate a Werkzeug-compatible password hash for employee provisioning.

Usage:
    python scripts/generate_password_hash.py 123456
"""
import argparse
from werkzeug.security import generate_password_hash

parser = argparse.ArgumentParser()
parser.add_argument("password", help="明文密码；请勿将生产密码写入 shell 历史")
args = parser.parse_args()
print(generate_password_hash(args.password, method="pbkdf2:sha256"))
