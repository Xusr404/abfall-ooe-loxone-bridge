"""Validate an existing release tag without evaluating user input as shell code."""

import os
import re
import subprocess


def resolve(tag):
    number = r"(?:0|[1-9][0-9]*)"
    identifier = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    if not re.fullmatch(rf"v{number}\.{number}\.{number}(?:-{identifier}(?:\.{identifier})*)?", tag):
        raise ValueError("Expected vMAJOR.MINOR.PATCH with optional SemVer prerelease")
    return subprocess.check_output(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], text=True,
    ).strip()


if __name__ == "__main__":
    tag = os.environ["RELEASE_TAG"]
    commit = resolve(tag)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"tag={tag[1:]}\ncommit={commit}\n")
        output.write(f"image=ghcr.io/{os.environ['OWNER'].lower()}/abfall-ooe\n")
