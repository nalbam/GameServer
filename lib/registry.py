"""Game-server registry backed by SSM Parameter Store.

Each server is one String parameter at /gameserver/<game> holding JSON:
    {instance_id, eip_alloc_id, public_ip, region, instance_type,
     sg_id, domain, image, version, port, created_at}

The per-game runtime environment lives separately at /env/prod/<game>
(SecureString) so it stays compatible with the in-instance deploy scripts.
"""

import json

REGISTRY_PREFIX = "/gameserver"


def param_name(game):
    return f"{REGISTRY_PREFIX}/{game}"


def env_param_name(game):
    return f"/env/prod/{game}"


def list_servers(aws):
    """Return all registered servers as a list of dicts (each includes 'game')."""
    result = aws.run(
        [
            "ssm",
            "get-parameters-by-path",
            "--path",
            REGISTRY_PREFIX,
            "--recursive",
        ],
        check=False,
    )
    servers = []
    for param in (result or {}).get("Parameters", []):
        name = param.get("Name", "")
        game = name[len(REGISTRY_PREFIX) + 1 :]
        if not game:
            continue
        try:
            data = json.loads(param.get("Value", "{}"))
        except json.JSONDecodeError:
            continue
        data["game"] = game
        servers.append(data)
    servers.sort(key=lambda s: s["game"])
    return servers


def get_server(aws, game):
    result = aws.run(
        ["ssm", "get-parameter", "--name", param_name(game)],
        check=False,
    )
    if not result or "Parameter" not in result:
        return None
    try:
        data = json.loads(result["Parameter"]["Value"])
    except json.JSONDecodeError:
        return None
    data["game"] = game
    return data


def put_server(aws, game, data):
    payload = {k: v for k, v in data.items() if k != "game"}
    aws.run(
        [
            "ssm",
            "put-parameter",
            "--name",
            param_name(game),
            "--type",
            "String",
            "--overwrite",
            "--value",
            json.dumps(payload),
        ],
        mutating=True,
    )


def delete_server(aws, game):
    aws.run(
        ["ssm", "delete-parameter", "--name", param_name(game)],
        mutating=True,
        check=False,
    )


def env_exists(aws, game):
    result = aws.run(
        ["ssm", "get-parameter", "--name", env_param_name(game)],
        check=False,
    )
    return bool(result and "Parameter" in result)


def put_env(aws, game, env_content):
    aws.run(
        [
            "ssm",
            "put-parameter",
            "--name",
            env_param_name(game),
            "--type",
            "SecureString",
            "--overwrite",
            "--value",
            env_content,
        ],
        mutating=True,
    )
