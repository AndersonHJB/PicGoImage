#!/usr/bin/env python3
"""Copy one verified image batch from the private source repo to PicGoImage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = False,
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def hashes(path: Path, size: int) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1()
    git_sha1.update(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            sha256.update(chunk)
            git_sha1.update(chunk)
    return sha256.hexdigest(), git_sha1.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-file", required=True, type=Path)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--target-base", default="main")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()

    batch = json.loads(args.batch_file.read_text(encoding="utf-8"))
    records = batch["records"]
    expected_bytes = sum(record["size_bytes"] for record in records)
    print(
        f"batch {batch['number']:02d}: {len(records)} files, "
        f"{expected_bytes} bytes",
        flush=True,
    )

    source = args.workdir / "source"
    if args.workdir.exists():
        shutil.rmtree(args.workdir)
    args.workdir.mkdir(parents=True)

    run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth=1",
            "--single-branch",
            "--branch",
            "main",
            args.source_repo,
            str(source),
        ]
    )

    paths = [record["source_path"] for record in records]
    run(["git", "checkout", "--no-progress", "HEAD", "--", *paths], cwd=source)

    entries: list[tuple[str, str]] = []
    verified_bytes = 0
    for record in records:
        path = source / record["source_path"]
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size != record["size_bytes"]:
            raise RuntimeError(f"size mismatch: {record['source_path']}: {size}")
        sha256, git_sha1 = hashes(path, size)
        if sha256 != record["sha256"]:
            raise RuntimeError(
                f"sha256 mismatch: {record['source_path']}: "
                f"{sha256} != {record['sha256']}"
            )
        run(["git", "cat-file", "-e", f"{git_sha1}^{{blob}}"], cwd=source)
        entries.append((git_sha1, record["remote_path"]))
        verified_bytes += size

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is missing")
    target_url = f"https://x-access-token:{token}@github.com/{args.target_repo}.git"
    run(["git", "remote", "add", "target", target_url], cwd=source)
    run(
        [
            "git",
            "fetch",
            "--filter=blob:none",
            "--depth=1",
            "target",
            args.target_base,
        ],
        cwd=source,
    )
    parent = run(["git", "rev-parse", "FETCH_HEAD"], cwd=source, capture=True)

    index_path = args.workdir / "target.index"
    index_env = os.environ.copy()
    index_env["GIT_INDEX_FILE"] = str(index_path)
    run(["git", "read-tree", parent], cwd=source, env=index_env)
    index_info = "".join(
        f"100644 {git_sha1}\t{remote_path}\n"
        for git_sha1, remote_path in entries
    )
    run(
        ["git", "update-index", "--add", "--index-info"],
        cwd=source,
        env=index_env,
        input_text=index_info,
    )
    tree = run(["git", "write-tree"], cwd=source, env=index_env, capture=True)

    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        }
    )
    message = f"Add bornforthis.cn article images batch {batch['number']:02d} of 29\n"
    commit = run(
        ["git", "commit-tree", tree, "-p", parent],
        cwd=source,
        env=commit_env,
        input_text=message,
        capture=True,
    )
    run(
        [
            "git",
            "push",
            "--force",
            "target",
            f"{commit}:refs/heads/{args.branch}",
        ],
        cwd=source,
    )
    print(
        f"BATCH_DONE number={batch['number']:02d} files={len(records)} "
        f"bytes={verified_bytes} commit={commit} branch={args.branch}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
