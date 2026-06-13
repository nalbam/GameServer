"""Deployment via SSM Run Command (no SSH keys required).

Renders the EC2 user-data bootstrap and the in-instance docker pull/restart
script, sends shell commands to instances through SSM, and polls for result.
"""

import json
import os
import time
import urllib.request

from . import ui

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


# -- versions -----------------------------------------------------------

def fetch_versions(github_repo, limit=10):
    """Release tags from the GitHub API (newest first). [] on failure."""
    url = f"https://api.github.com/repos/{github_repo}/releases"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gameserver-cli"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        return [rel["tag_name"] for rel in data][:limit]
    except Exception:
        return []


def select_version(github_repo):
    """Interactive version pick; returns a docker tag (no leading 'v'),
    or None if the user cancels."""
    tags = fetch_versions(github_repo)
    options = ["latest"] + tags
    chosen = ui.select("배포할 버전:", options, default_index=0, allow_cancel=True)
    if chosen is None:
        return None
    if chosen == "latest":
        return "latest"
    return chosen.lstrip("v")


# -- rendering ----------------------------------------------------------

def render_user_data(game, image, tag, app_port, ssm_param):
    path = os.path.join(TEMPLATE_DIR, "ec2-user-data.sh")
    with open(path) as f:
        tpl = f.read()
    replacements = {
        "__GAME__": game,
        "__DOCKER_IMAGE__": image,
        "__DOCKER_TAG__": tag,
        "__APP_PORT__": str(app_port),
        "__SSM_PARAM_NAME__": ssm_param,
    }
    for token, value in replacements.items():
        tpl = tpl.replace(token, value)
    return tpl


def render_restart_script(game, image, tag, app_port, ssm_param, bind_local):
    """Bash that refreshes .env from SSM, pulls a tag, and restarts the
    container. bind_local=True binds 127.0.0.1 (fronted by Nginx)."""
    bind = "127.0.0.1" if bind_local else "0.0.0.0"
    install_dir = f"/home/ec2-user/{game}"
    return f"""#!/bin/bash
set -euo pipefail
INSTALL_DIR="{install_dir}"
IMAGE="{image}:{tag}"
mkdir -p "$INSTALL_DIR"
ENV_CONTENT=$(aws ssm get-parameter --name "{ssm_param}" --with-decryption \
    --output text --query Parameter.Value 2>/dev/null || echo "")
if [[ -n "$ENV_CONTENT" ]]; then
    echo "$ENV_CONTENT" > "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
fi
docker pull "$IMAGE"
docker stop {game} 2>/dev/null || true
docker rm {game} 2>/dev/null || true
docker run -d --name {game} --restart unless-stopped \
    -p "{bind}:{app_port}:{app_port}" \
    --env-file "$INSTALL_DIR/.env" \
    "$IMAGE"
docker image prune -f >/dev/null 2>&1 || true
echo "Deployed $IMAGE bound to {bind}:{app_port}"
"""


# -- SSM Run Command ----------------------------------------------------

def send_command(aws, instance_id, script, comment, timeout=600):
    """Run a shell script on an instance via SSM; wait for completion.
    Returns True on success."""
    if aws.dry_run:
        print(f"  [dry-run] ssm send-command -> {instance_id}: {comment}")
        print("  ---- script ----")
        for line in script.splitlines():
            print(f"  | {line}")
        print("  ----------------")
        return True

    params = {"commands": script.splitlines()}
    result = aws.run(
        [
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            comment,
            "--parameters",
            json.dumps(params),
            "--timeout-seconds",
            str(timeout),
        ],
        mutating=True,
    )
    command_id = result["Command"]["CommandId"]
    ui.info(f"SSM 명령 전송됨: {command_id} — 완료 대기 중...")
    return _wait_invocation(aws, command_id, instance_id, timeout)


def _wait_invocation(aws, command_id, instance_id, timeout):
    deadline = time.time() + timeout
    terminal = {"Success", "Failed", "Cancelled", "TimedOut"}
    while time.time() < deadline:
        time.sleep(4)
        inv = aws.run(
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            ],
            check=False,
        )
        if not inv:
            continue
        status = inv.get("Status", "Pending")
        if status in terminal:
            if status == "Success":
                ui.success("SSM 명령 성공")
                out = inv.get("StandardOutputContent", "").strip()
                if out:
                    print(out.splitlines()[-1])
                return True
            ui.error(f"SSM 명령 실패: {status}")
            err = inv.get("StandardErrorContent", "").strip()
            if err:
                print(err)
            return False
    ui.error("SSM 명령 타임아웃")
    return False


def ssm_online(aws, instance_id):
    """Whether the SSM agent has registered the instance as online."""
    result = aws.run(
        [
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        ],
        check=False,
    )
    for info in (result or {}).get("InstanceInformationList", []):
        if info.get("PingStatus") == "Online":
            return True
    return False


def wait_ssm_online(aws, instance_id, timeout=300):
    if aws.dry_run:
        return True
    ui.info("SSM 에이전트 온라인 대기...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ssm_online(aws, instance_id):
            ui.success("SSM 에이전트 온라인")
            return True
        time.sleep(8)
    ui.warn("SSM 에이전트가 아직 온라인이 아닙니다 (나중에 재배포로 재시도 가능)")
    return False
