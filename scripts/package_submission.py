"""Build a submission-safe archive of the project.

    python scripts/package_submission.py [--out dist/agentguard_submission.zip]

Includes the application, the Drunix integration (bridge + agentauth
chaincode + scripts), migrations, templates, static assets, tests, docs and
requirements. Excludes secrets, local state and machine-specific files:
.env (but keeps .env.example), .venv, .git, dev_keys, __pycache__,
.pytest_cache, .DS_Store, drunix/.runtime (network state, generated crypto,
logs), drunix/drunix.env, built binaries, archives and local backups.

After building, the archive is scanned for private keys and credentials; the
script exits non-zero if anything suspicious is found.
"""
import argparse
import fnmatch
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".git", ".venv", "venv", "dev_keys", "__pycache__", ".pytest_cache", "node_modules", "dist",
                "build", ".idea", ".vscode", "__MACOSX"}
EXCLUDE_PATHS = ["drunix/.runtime", "drunix/chaincode-agentauth/vendor", "ui_backup_pre_redesign_*"]
EXCLUDE_FILES = [".env", ".env.bak*", ".env.local", ".DS_Store", "*.pyc", "*.pem", "*.key", "priv_sk", "*_sk",
                 "*.tar.gz", "*.tgz", "*.zip", "drunix.env", "drunix-bridge", "m_act.png"]
KEEP = {".env.example", "drunix/drunix.env.example"}

SECRET_PATTERNS = [
    (re.compile(rb"-----BEGIN (?:EC |RSA |OPENSSH |)PRIVATE KEY-----"), "private key"),
    # a URL with a real-looking password (documentation placeholders are allowed)
    (re.compile(rb"postgres(?:ql)?(?:\+psycopg2)?://[^:\s/]+:(?!(?:CHANGE_ME|<password>|pass|PASS|password|\*\*\*)@)[^@\s]{3,}@"),
     "database URL with a password"),
    (re.compile(rb"^AGENT_KEY_SEED=\S+", re.M), "AGENT_KEY_SEED value"),
    (re.compile(rb"^DRUNIX_BRIDGE_TOKEN=\S+", re.M), "bridge token value"),
]


def excluded(rel: str) -> bool:
    if rel in KEEP:
        return False
    parts = rel.split("/")
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    for pat in EXCLUDE_PATHS:
        if "/" in pat:
            if rel.startswith(pat + "/"):
                return True
        elif fnmatch.fnmatch(parts[0], pat) and len(parts) > 1:
            return True
    return any(fnmatch.fnmatch(parts[-1], pat) for pat in EXCLUDE_FILES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist/agentguard_submission.zip")
    args = ap.parse_args()
    out = (ROOT / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and not p.is_symlink()
                   and not excluded(p.relative_to(ROOT).as_posix()) and p.resolve() != out)
    findings = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            data = p.read_bytes()
            for pat, what in SECRET_PATTERNS:
                if pat.search(data):
                    findings.append(f"{rel}: {what}")
            z.writestr(f"agentguard/{rel}", data)
    size = out.stat().st_size
    tops = sorted({f.relative_to(ROOT).parts[0] for f in files})
    print(f"wrote {out} — {len(files)} files, {size / 1024:.0f} KiB")
    print("top-level entries:", ", ".join(tops))
    for must in ("app", "alembic", "drunix", "static", "templates", "tests", "docs", "requirements.txt", "README.md"):
        if must not in tops:
            findings.append(f"missing required entry: {must}")
    if findings:
        print("SECRET / COMPLETENESS SCAN FAILED:")
        for f in findings:
            print("  -", f)
        return 1
    print("secret scan: clean (no private keys, credentials, key seeds or tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
