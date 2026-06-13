#!/usr/bin/env python3
"""GameServer — interactive AWS game-server manager.

Provisions and operates Dockerised game servers (SnowClash / TankClash style)
on EC2: checks AWS auth, keeps a server registry in SSM, launches instances
with an Elastic IP, deploys/redeploys via SSM Run Command, and wires up a
Route53 domain with Nginx + Let's Encrypt (falling back to eip:port).

Usage:
    ./gameserver.py [--region REGION] [--dry-run]
"""

import argparse
import datetime

from lib import aws as awslib
from lib import deploy, ec2, registry, route53, ui

# Known games. Unknown games can be added interactively.
CATALOG = {
    "snowclash": {
        "image": "ghcr.io/nalbam/snowclash",
        "port": 2567,
        "github_repo": "nalbam/SnowClash",
    },
    "tankclash": {
        "image": "ghcr.io/nalbam/tankclash",
        "port": 2567,
        "github_repo": "nalbam/TankClash",
    },
}

COMMON_REGIONS = [
    "ap-northeast-2",
    "ap-northeast-1",
    "us-east-1",
    "us-west-2",
    "eu-west-1",
]


# -- setup --------------------------------------------------------------

def check_auth(aws):
    ui.header("GameServer Manager")
    try:
        ident = aws.caller_identity()
    except awslib.AwsError as e:
        ui.fatal(
            "AWS 인증 실패. 자격증명을 확인하세요 (aws configure / SSO).\n  "
            + str(e)
        )
    ui.success(f"계정: {ident['Account']}")
    ui.info(f"ARN:  {ident['Arn']}")


def resolve_region(aws, cli_region):
    region = cli_region or aws.configured_region()
    if region:
        aws.region = region
        ui.info(f"리전: {region}")
        return
    chosen = ui.select(
        "리전이 설정되지 않았습니다. 선택하세요:",
        COMMON_REGIONS,
        default_index=0,
        allow_cancel=True,
    )
    if chosen is None:
        ui.fatal("리전이 선택되지 않아 종료합니다.")
    aws.region = chosen
    ui.info(f"리전: {chosen}")


def game_meta(game, server=None):
    """Resolve image/port/github_repo from catalog, server record, or input."""
    meta = dict(CATALOG.get(game, {}))
    if server:
        if server.get("image"):
            meta["image"] = server["image"]
        if server.get("port"):
            meta["port"] = server["port"]
    if "image" not in meta:
        meta["image"] = ui.prompt_required(f"Docker 이미지 (예: ghcr.io/nalbam/{game})")
    if "port" not in meta:
        meta["port"] = int(ui.prompt("앱 포트", default="2567"))
    if "github_repo" not in meta:
        meta["github_repo"] = ui.prompt(
            "GitHub repo (owner/name, 버전 조회용)", default=""
        )
    return meta


# -- list ---------------------------------------------------------------

def show_servers(aws):
    servers = registry.list_servers(aws)
    if not servers:
        ui.info("등록된 게임 서버가 없습니다.")
        return servers
    rows = []
    for s in servers:
        status = ec2.describe_instance(aws, s.get("instance_id"))
        state = status["state"] if status else "없음"
        rows.append(
            [
                s["game"],
                state,
                s.get("public_ip", "-"),
                s.get("domain") or "-",
                s.get("version", "-"),
                s.get("instance_type", "-"),
            ]
        )
    ui.table(rows, ["게임", "상태", "EIP", "도메인", "버전", "타입"])
    return servers


# -- create -------------------------------------------------------------

def choose_game_for_create(aws):
    existing = {s["game"] for s in registry.list_servers(aws)}
    options = [g for g in CATALOG if g not in existing] + ["(직접 입력)"]
    chosen = ui.select("생성할 게임:", options, allow_cancel=True)
    if chosen is None:
        return None
    if chosen == "(직접 입력)":
        return ui.prompt_required("게임 이름 (소문자, 예: tankclash)").lower()
    return chosen


def ensure_env(aws, game, meta, public_ip_hint=None):
    if registry.env_exists(aws, game):
        ui.info(f"SSM env 존재: {registry.env_param_name(game)}")
        return
    if not ui.confirm(
        f"SSM env({registry.env_param_name(game)})가 없습니다. 기본값으로 생성할까요?",
        default=True,
    ):
        return
    origins = ui.prompt(
        "ALLOWED_ORIGINS", default="https://nalbam.github.io"
    )
    env = (
        "NODE_ENV=production\n"
        f"PORT={meta['port']}\n"
        f"ALLOWED_ORIGINS={origins}\n"
    )
    registry.put_env(aws, game, env)
    ui.success("기본 env 생성됨 (도메인 연결 후 SERVER_URL/ALLOWED_ORIGINS 갱신 권장)")


def cmd_create(aws):
    game = choose_game_for_create(aws)
    if not game:
        return
    existing = registry.get_server(aws, game)
    if existing and ec2.describe_instance(aws, existing.get("instance_id")):
        ui.warn(f"'{game}' 서버가 이미 존재합니다. 관리 메뉴를 사용하세요.")
        return

    meta = game_meta(game)
    ensure_env(aws, game, meta)

    instance_type = ui.select(
        "인스턴스 타입:",
        ec2.INSTANCE_TYPES,
        labeler=lambda t: f"{t[0]:<12} {t[1]}",
        default_index=0,
        allow_cancel=True,
    )
    if instance_type is None:
        return
    instance_type = instance_type[0]

    key_pairs = ec2.list_key_pairs(aws)
    if key_pairs:
        key_name = ui.select(
            "키 페어 (SSH 접속용):",
            key_pairs + ["(없음 — SSM만 사용)"],
            allow_cancel=True,
        )
        if key_name is None:
            return
        if key_name == "(없음 — SSM만 사용)":
            key_name = None
    else:
        ui.warn("키 페어가 없습니다. SSM Run Command로만 관리합니다.")
        key_name = None

    vpc_id = ec2.default_vpc_id(aws)
    if not vpc_id:
        ui.fatal("기본 VPC를 찾을 수 없습니다. VPC를 먼저 준비하세요.")

    ssh_cidr = ec2.my_public_cidr() if key_name else None
    if key_name and not ssh_cidr:
        ui.warn("내 공인 IP 조회 실패 — SSH를 0.0.0.0/0으로 엽니다.")

    if meta["github_repo"]:
        tag = deploy.select_version(meta["github_repo"])
        if tag is None:
            return
    else:
        tag = "latest"

    ui.header("생성 요약")
    ui.info(f"게임:        {game}")
    ui.info(f"이미지:      {meta['image']}:{tag}")
    ui.info(f"인스턴스:    {instance_type}")
    ui.info(f"키 페어:     {key_name or '(없음)'}")
    ui.info(f"포트:        {meta['port']} (eip:port 직접 접속)")
    ui.info(f"리전/VPC:    {aws.region} / {vpc_id}")
    if not ui.confirm("이 설정으로 생성할까요?", default=True):
        return

    sg_id = ec2.ensure_security_group(
        aws, game, vpc_id, meta["port"], open_app_port=True, ssh_cidr=ssh_cidr
    )
    profile = ec2.ensure_instance_profile(aws)
    arch = ec2.architecture_for(aws, instance_type)
    image_arches = ec2.image_architectures(meta["image"], tag)
    if image_arches and ec2.normalize_arch(arch) not in image_arches:
        ui.warn(
            f"이미지 {meta['image']}:{tag} 는 {sorted(image_arches)} 만 지원하는데 "
            f"선택한 인스턴스({instance_type})는 {ec2.normalize_arch(arch)} 입니다."
        )
        ui.warn("이대로면 컨테이너가 실행되지 않습니다 (exec format error).")
        if not ui.confirm("호환되는 인스턴스 타입으로 다시 선택할까요?", default=True):
            if not ui.confirm("그래도 강제로 계속할까요?", default=False):
                return
        else:
            return cmd_create(aws)

    ami_id = ec2.latest_al2023_ami(aws, arch)
    ui.info(f"AMI: {ami_id} ({arch})")

    user_data = deploy.render_user_data(
        game, meta["image"], tag, meta["port"], registry.env_param_name(game)
    )
    instance_id = ec2.launch_instance(
        aws, game, instance_type, ami_id, key_name, sg_id, profile, user_data
    )
    ui.success(f"인스턴스 시작: {instance_id}")
    ec2.wait_running(aws, instance_id)
    alloc_id, public_ip = ec2.allocate_and_associate_eip(aws, instance_id, game)
    ui.success(f"EIP 연결: {public_ip}")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    registry.put_server(
        aws,
        game,
        {
            "instance_id": instance_id,
            "eip_alloc_id": alloc_id,
            "public_ip": public_ip,
            "region": aws.region,
            "instance_type": instance_type,
            "domain": "",
            "image": meta["image"],
            "version": tag,
            "port": meta["port"],
            "created_at": now,
        },
    )
    ui.success("레지스트리 기록 완료")

    deploy.wait_ssm_online(aws, instance_id)
    ui.header("생성 완료")
    print(f"  접속:  http://{public_ip}:{meta['port']}")
    print("  부트스트랩(docker pull/run)은 인스턴스에서 1~3분 더 진행됩니다.")
    print("  도메인(HTTPS) 연결은 관리 메뉴 > '도메인 연결'을 사용하세요.")


# -- manage -------------------------------------------------------------

def cmd_redeploy(aws, server, meta):
    if meta["github_repo"]:
        tag = deploy.select_version(meta["github_repo"])
        if tag is None:
            return
    else:
        ui.warn("github_repo가 없어 버전 목록을 가져올 수 없습니다.")
        tag = ui.prompt("버전 태그", default="latest")
    bind_local = bool(server.get("domain"))
    script = deploy.render_restart_script(
        server["game"],
        meta["image"],
        tag,
        meta["port"],
        registry.env_param_name(server["game"]),
        bind_local,
    )
    if not ui.confirm(f"{meta['image']}:{tag} 로 재배포할까요?", default=True):
        return
    if deploy.send_command(
        aws, server["instance_id"], script, f"deploy {server['game']}:{tag}"
    ):
        server["version"] = tag
        registry.put_server(aws, server["game"], server)
        ui.success(f"재배포 완료: {tag}")


def cmd_connect_domain(aws, server, meta):
    if not server.get("public_ip"):
        ui.warn("EIP 정보가 없습니다.")
        return
    zones = route53.list_hosted_zones(aws)
    if not zones:
        ui.warn("호스팅영역이 없습니다. eip:port로 접속하세요.")
        print(f"  접속: http://{server['public_ip']}:{meta['port']}")
        return
    zone = ui.select(
        "호스팅영역:", zones, labeler=lambda z: z["name"], allow_cancel=True
    )
    if zone is None:
        return
    fqdn = route53.fqdn_for(server["game"], zone["name"])
    email = ui.prompt("Let's Encrypt 이메일", default=f"admin@{zone['name']}")
    ui.info(f"도메인: {fqdn} -> {server['public_ip']}")
    if not ui.confirm("A 레코드 생성 + HTTPS 설정을 진행할까요?", default=True):
        return

    route53.upsert_a_record(aws, zone["id"], fqdn, server["public_ip"])
    ui.info("DNS 전파를 기다린 뒤 인증서를 발급합니다 (실패 시 잠시 후 재시도 가능).")
    script = route53.render_nginx_certbot_script(
        server["game"], fqdn, email, meta["port"]
    )
    if deploy.send_command(
        aws, server["instance_id"], script, f"https {fqdn}", timeout=600
    ):
        server["domain"] = fqdn
        registry.put_server(aws, server["game"], server)
        ui.header("도메인 연결 완료")
        print(f"  접속: https://{fqdn}")
        print("  SERVER_URL/ALLOWED_ORIGINS를 SSM env에 갱신하고 재배포하세요.")


def cmd_delete(aws, server):
    game = server["game"]
    ui.warn(f"'{game}' 서버를 삭제하면 EC2 종료 + EIP 해제 + 레지스트리 삭제됩니다.")
    if not ui.confirm(f"정말 '{game}'를 삭제할까요?", default=False):
        return
    confirm_name = ui.prompt(f"확인을 위해 게임 이름을 입력하세요 ('{game}')")
    if confirm_name != game:
        ui.info("취소되었습니다.")
        return
    iid = server.get("instance_id")
    if iid:
        aws.run(
            ["ec2", "terminate-instances", "--instance-ids", iid],
            mutating=True,
            check=False,
        )
        ui.info(f"인스턴스 종료 요청: {iid}")
    alloc = server.get("eip_alloc_id")
    if alloc:
        aws.run(
            ["ec2", "release-address", "--allocation-id", alloc],
            mutating=True,
            check=False,
        )
        ui.info(f"EIP 해제: {alloc}")
    registry.delete_server(aws, game)
    ui.success(f"'{game}' 레지스트리 삭제 완료 (env/{game}는 보존)")


def cmd_manage(aws, server):
    game = server["game"]
    meta = game_meta(game, server)
    while True:
        status = ec2.describe_instance(aws, server.get("instance_id"))
        ui.header(f"{game} 관리")
        ui.info(f"상태: {status['state'] if status else '없음'}  "
                f"IP: {server.get('public_ip', '-')}  "
                f"도메인: {server.get('domain') or '-'}  "
                f"버전: {server.get('version', '-')}")
        action = ui.select(
            "",
            ["재배포 (새 버전)", "도메인 연결", "접속 정보", "서버 삭제"],
            allow_cancel=True,
        )
        if action is None:
            return
        if action == "재배포 (새 버전)":
            cmd_redeploy(aws, server, meta)
        elif action == "도메인 연결":
            cmd_connect_domain(aws, server, meta)
        elif action == "접속 정보":
            if server.get("domain"):
                print(f"  https://{server['domain']}")
            else:
                print(f"  http://{server.get('public_ip', '?')}:{meta['port']}")
        elif action == "서버 삭제":
            cmd_delete(aws, server)
            return


# -- main loop ----------------------------------------------------------

def main_menu(aws):
    while True:
        servers = show_servers(aws)
        options = ["새 게임 서버 생성"]
        options += [f"관리: {s['game']}" for s in servers]
        options.append("새로고침")
        choice = ui.select("작업 선택:", options, allow_cancel=True)
        if choice is None:
            ui.info("종료합니다.")
            return
        if choice == "새 게임 서버 생성":
            cmd_create(aws)
        elif choice == "새로고침":
            continue
        else:
            game = choice[len("관리: "):]
            server = next((s for s in servers if s["game"] == game), None)
            if server:
                cmd_manage(aws, server)


def main():
    parser = argparse.ArgumentParser(description="GameServer AWS manager")
    parser.add_argument("--region", help="AWS 리전 (미지정 시 설정/선택)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="변경 작업을 실행하지 않고 명령만 출력",
    )
    args = parser.parse_args()

    aws = awslib.Aws(region=args.region, dry_run=args.dry_run)
    try:
        aws.ensure_cli()
    except awslib.AwsError as e:
        ui.fatal(str(e))

    if args.dry_run:
        ui.warn("DRY-RUN 모드: 변경 작업은 실행되지 않습니다.")

    check_auth(aws)
    resolve_region(aws, args.region)
    main_menu(aws)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        ui.info("중단되었습니다.")
