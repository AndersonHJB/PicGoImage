#!/usr/bin/env python3
"""Copy verified image batches from the private source repo to PicGoImage."""

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


def sparse_pattern(path: str) -> str:
    """Return an anchored literal gitignore pattern for one repository path."""
    escaped = path.replace("\\", "\\\\")
    for character in ("*", "?", "[", "]"):
        escaped = escaped.replace(character, f"\\{character}")
    return f"/{escaped}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--batches", required=True, help="comma-separated batch numbers")
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--target-base", default="main")
    parser.add_argument("--branch-prefix", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()

    batch_numbers = [int(value) for value in args.batches.split(",") if value.strip()]
    batches = [
        json.loads((args.batch_dir / f"{number:02d}.json").read_text(encoding="utf-8"))
        for number in batch_numbers
    ]
    if [batch["number"] for batch in batches] != batch_numbers:
        raise RuntimeError("batch manifest order mismatch")
    records = [record for batch in batches for record in batch["records"]]
    expected_bytes = sum(record["size_bytes"] for record in records)
    print(
        f"batches {','.join(str(number) for number in batch_numbers)}: "
        f"{len(records)} files, {expected_bytes} bytes",
        flush=True,
    )

    source = args.workdir / "source"
    if args.workdir.exists():
        shutil.rmtree(args.workdir)
    args.workdir.mkdir(parents=True)

    print("cloning source metadata", flush=True)
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

    print("fetching selected source blobs", flush=True)
    paths = sorted({record["source_path"] for record in records})
    patterns = "".join(f"{sparse_pattern(path)}\n" for path in paths)
    run(["git", "sparse-checkout", "init", "--no-cone"], cwd=source)
    run(
        ["git", "sparse-checkout", "set", "--no-cone", "--stdin"],
        cwd=source,
        input_text=patterns,
    )
    run(["git", "config", "checkout.workers", "0"], cwd=source)
    run(["git", "checkout", "--no-progress", "HEAD"], cwd=source)

    print("verifying source bytes", flush=True)
    verified: dict[str, str] = {}
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
        verified[record["sha256"]] = git_sha1
        verified_bytes += size

    print("fetching target base", flush=True)
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
    run(["git", "config", "remote.origin.promisor", "false"], cwd=source)
    run(["git", "config", "remote.target.promisor", "true"], cwd=source)
    run(["git", "config", "remote.target.partialclonefilter", "blob:none"], cwd=source)
    run(["git", "config", "extensions.partialClone", "target"], cwd=source)

    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        }
    )
    completed_bytes = 0
    for batch in batches:
        number = batch["number"]
        batch_records = batch["records"]
        branch = f"{args.branch_prefix}{number:02d}"
        index_path = args.workdir / f"target-{number:02d}.index"
        index_env = os.environ.copy()
        index_env["GIT_INDEX_FILE"] = str(index_path)
        run(["git", "read-tree", parent], cwd=source, env=index_env)
        index_info = "".join(
            f"100644 {verified[record['sha256']]}\t{record['remote_path']}\n"
            for record in batch_records
        )
        run(
            ["git", "update-index", "--add", "--index-info"],
            cwd=source,
            env=index_env,
            input_text=index_info,
        )
        tree = run(["git", "write-tree"], cwd=source, env=index_env, capture=True)
        message = f"Add bornforthis.cn article images batch {number:02d} of 29\n"
        commit = run(
            ["git", "commit-tree", tree, "-p", parent],
            cwd=source,
            env=commit_env,
            input_text=message,
            capture=True,
        )
        print(f"pushing batch {number:02d}", flush=True)
        run(
            [
                "git",
                "push",
                "--force",
                "target",
                f"{commit}:refs/heads/{branch}",
            ],
            cwd=source,
        )
        batch_bytes = sum(record["size_bytes"] for record in batch_records)
        completed_bytes += batch_bytes
        print(
            f"BATCH_DONE number={number:02d} files={len(batch_records)} "
            f"bytes={batch_bytes} commit={commit} branch={branch}",
            flush=True,
        )
    if completed_bytes != verified_bytes:
        raise RuntimeError(f"verified byte mismatch: {completed_bytes} != {verified_bytes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
