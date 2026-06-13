"""Auto-discover game metadata from sibling git repositories.

This tool lives in .../<account>/GameServer; game servers live as sibling
repos (.../<account>/SnowClash, .../<account>/TankClash). For each sibling
that looks like a deployable game (a git repo with a Dockerfile) we derive
the GitHub repo, ghcr image, app port, and GitHub Pages origin from the repo
itself — no hardcoded account/project values.
"""

import os
import re
import subprocess


def git_slug(path):
    """'owner/repo' from a repo's origin remote, or None."""
    try:
        url = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    # git@github.com:owner/repo.git  |  https://github.com/owner/repo(.git)
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _expose_port(dockerfile):
    try:
        with open(dockerfile) as f:
            for line in f:
                m = re.match(r"\s*EXPOSE\s+(\d+)", line)
                if m:
                    return int(m.group(1))
    except OSError:
        pass
    return None


def derive(path):
    """Metadata for one game directory, or None if it isn't a game repo."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    dockerfile = os.path.join(path, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return None
    slug = git_slug(path)
    if not slug:
        return None
    owner, repo = slug.split("/", 1)
    meta = {
        "github_repo": slug,
        "image": f"ghcr.io/{owner.lower()}/{repo.lower()}",
        "default_origin": f"https://{owner.lower()}.github.io",
    }
    port = _expose_port(dockerfile)
    if port:
        meta["port"] = port
    return meta


def discover_games(search_root):
    """{game_name: meta} for sibling game repos under search_root."""
    games = {}
    try:
        entries = sorted(os.listdir(search_root))
    except OSError:
        return games
    for name in entries:
        meta = derive(os.path.join(search_root, name))
        if meta:
            games[name.lower()] = meta
    return games
