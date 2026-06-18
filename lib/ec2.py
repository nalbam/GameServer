"""EC2 provisioning: AMI lookup, key pairs, security groups, IAM instance
profile, instance launch, and Elastic IP association.

All write paths funnel through Aws.run(mutating=True) so they honour dry-run.
"""

import json
import urllib.request

from . import ui

# x86_64 first: the game images are published linux/amd64-only, so an ARM
# (Graviton) instance would fail to run them. ARM options stay available but
# are guarded against by image-architecture checks in the launch flow.
INSTANCE_TYPES = [
    ("t3.small", "x86 2vCPU/2GB  ~$18/mo  (권장)"),
    ("t3.micro", "x86 2vCPU/1GB  ~$9/mo   (테스트)"),
    ("t3.medium", "x86 2vCPU/4GB  ~$36/mo  (프로덕션)"),
    ("t4g.small", "ARM 2vCPU/2GB  ~$12/mo  (이미지가 arm64 지원 시)"),
    ("t4g.micro", "ARM 2vCPU/1GB  ~$6/mo   (이미지가 arm64 지원 시)"),
    ("t4g.medium", "ARM 2vCPU/4GB  ~$24/mo  (이미지가 arm64 지원 시)"),
]


# docker/OCI architecture names <-> EC2 architecture names
_ARCH_ALIAS = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def normalize_arch(arch):
    return _ARCH_ALIAS.get(arch, arch)


# -- discovery ----------------------------------------------------------

def describe_instance(aws, instance_id):
    """Return {state, public_ip} for an instance, or None if it is gone."""
    if not instance_id:
        return None
    result = aws.run(
        ["ec2", "describe-instances", "--instance-ids", instance_id],
        check=False,
    )
    for res in (result or {}).get("Reservations", []):
        for inst in res.get("Instances", []):
            return {
                "state": inst.get("State", {}).get("Name", "unknown"),
                "public_ip": inst.get("PublicIpAddress", ""),
            }
    return None


def default_vpc_id(aws):
    result = aws.run(
        [
            "ec2",
            "describe-vpcs",
            "--filters",
            "Name=isDefault,Values=true",
        ]
    )
    vpcs = result.get("Vpcs", [])
    if not vpcs:
        return None
    return vpcs[0]["VpcId"]


def list_key_pairs(aws):
    result = aws.run(["ec2", "describe-key-pairs"], check=False)
    return [kp["KeyName"] for kp in (result or {}).get("KeyPairs", [])]


def architecture_for(aws, instance_type):
    """Resolve the CPU architecture (x86_64 / arm64) for an instance type."""
    result = aws.run(
        [
            "ec2",
            "describe-instance-types",
            "--instance-types",
            instance_type,
            "--query",
            "InstanceTypes[0].ProcessorInfo.SupportedArchitectures",
        ],
        check=False,
    )
    archs = result or []
    if "arm64" in archs:
        return "arm64"
    return "x86_64"


def latest_al2023_ami(aws, arch):
    """Latest Amazon Linux 2023 AMI id via the public SSM parameter."""
    name = f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"
    result = aws.run(["ssm", "get-parameter", "--name", name])
    return result["Parameter"]["Value"]


def image_architectures(image, tag):
    """Best-effort set of OCI architectures (e.g. {'amd64'}) supported by a
    ghcr.io image. Returns None when it can't be determined (non-ghcr registry
    or network/parse failure) so the caller can skip the guard."""
    prefix = "ghcr.io/"
    if not image.startswith(prefix):
        return None
    repo = image[len(prefix):]
    try:
        token_url = f"https://ghcr.io/token?scope=repository:{repo}:pull"
        with urllib.request.urlopen(token_url, timeout=8) as r:
            token = json.loads(r.read().decode()).get("token", "")
        man_url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
        req = urllib.request.Request(
            man_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json,"
                "application/vnd.docker.distribution.manifest.list.v2+json,"
                "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            manifest = json.loads(r.read().decode())
    except Exception:
        return None

    arches = set()
    for m in manifest.get("manifests", []):
        arch = m.get("platform", {}).get("architecture")
        if arch and arch != "unknown":
            arches.add(arch)
    return arches or None


def my_public_cidr():
    """This machine's public IP as a /32 CIDR, for the SSH rule."""
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
            ip = r.read().decode().strip()
        return f"{ip}/32"
    except Exception:
        return None


# -- security group -----------------------------------------------------

def _sg_id_by_name(aws, vpc_id, name):
    result = aws.run(
        [
            "ec2",
            "describe-security-groups",
            "--filters",
            f"Name=group-name,Values={name}",
            f"Name=vpc-id,Values={vpc_id}",
        ],
        check=False,
    )
    groups = (result or {}).get("SecurityGroups", [])
    return groups[0]["GroupId"] if groups else None


def ensure_security_group(aws, game, vpc_id, app_port, open_app_port, ssh_cidr):
    """Create (or reuse) a security group allowing HTTP/HTTPS (and SSH only
    when ssh_cidr is set) plus, when open_app_port is set, the raw app port
    for eip:port access."""
    name = f"gameserver-{game}"
    existing = _sg_id_by_name(aws, vpc_id, name)
    if existing:
        ui.info(f"기존 보안그룹 재사용: {name} ({existing})")
        return existing

    created = aws.run(
        [
            "ec2",
            "create-security-group",
            "--group-name",
            name,
            "--description",
            f"GameServer {game}",
            "--vpc-id",
            vpc_id,
        ],
        mutating=True,
    )
    sg_id = created["GroupId"] if created else f"sg-dryrun-{game}"

    def authorize(port, cidr, desc):
        aws.run(
            [
                "ec2",
                "authorize-security-group-ingress",
                "--group-id",
                sg_id,
                "--ip-permissions",
                json.dumps(
                    [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": port,
                            "ToPort": port,
                            "IpRanges": [{"CidrIp": cidr, "Description": desc}],
                        }
                    ]
                ),
            ],
            mutating=True,
            check=False,
        )

    if ssh_cidr:
        authorize(22, ssh_cidr, "SSH")
    authorize(80, "0.0.0.0/0", "HTTP")
    authorize(443, "0.0.0.0/0", "HTTPS")
    if open_app_port:
        authorize(app_port, "0.0.0.0/0", "GameServer app port")

    ui.success(f"보안그룹 생성: {name} ({sg_id})")
    return sg_id


def instance_security_groups(aws, instance_id):
    """The security-group ids attached to an instance (empty list if gone)."""
    if not instance_id:
        return []
    result = aws.run(
        ["ec2", "describe-instances", "--instance-ids", instance_id],
        check=False,
    )
    for res in (result or {}).get("Reservations", []):
        for inst in res.get("Instances", []):
            return [g["GroupId"] for g in inst.get("SecurityGroups", [])]
    return []


def revoke_app_port(aws, sg_id, app_port, cidr="0.0.0.0/0"):
    """Remove the raw app-port ingress so only Nginx (443) stays reachable.
    Idempotent: a missing rule is not treated as an error."""
    aws.run(
        [
            "ec2",
            "revoke-security-group-ingress",
            "--group-id",
            sg_id,
            "--ip-permissions",
            json.dumps(
                [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": app_port,
                        "ToPort": app_port,
                        "IpRanges": [{"CidrIp": cidr}],
                    }
                ]
            ),
        ],
        mutating=True,
        check=False,
    )


# -- IAM instance profile ----------------------------------------------

def ensure_instance_profile(aws, game):
    """Idempotently create a per-game EC2 role + instance profile granting SSM
    agent access and read on this game's /env/prod/<game> parameter only."""
    role = f"gameserver-{game}-role"
    profile = f"gameserver-{game}-profile"
    existing = aws.run(
        ["iam", "get-instance-profile", "--instance-profile-name", profile],
        check=False,
    )
    if existing and "InstanceProfile" in existing:
        ui.info(f"기존 IAM 인스턴스 프로파일 재사용: {profile}")
        return profile

    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    aws.run(
        [
            "iam",
            "create-role",
            "--role-name",
            role,
            "--assume-role-policy-document",
            json.dumps(trust),
        ],
        mutating=True,
        check=False,
    )
    aws.run(
        [
            "iam",
            "attach-role-policy",
            "--role-name",
            role,
            "--policy-arn",
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        ],
        mutating=True,
        check=False,
    )
    ssm_read = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                "Resource": [
                    f"arn:aws:ssm:{aws.region or '*'}:*:parameter/env/prod/{game}",
                ],
            }
        ],
    }
    aws.run(
        [
            "iam",
            "put-role-policy",
            "--role-name",
            role,
            "--policy-name",
            "gameserver-ssm-read",
            "--policy-document",
            json.dumps(ssm_read),
        ],
        mutating=True,
        check=False,
    )
    aws.run(
        [
            "iam",
            "create-instance-profile",
            "--instance-profile-name",
            profile,
        ],
        mutating=True,
        check=False,
    )
    aws.run(
        [
            "iam",
            "add-role-to-instance-profile",
            "--instance-profile-name",
            profile,
            "--role-name",
            role,
        ],
        mutating=True,
        check=False,
    )
    ui.success(f"IAM 인스턴스 프로파일 생성: {profile}")
    if not aws.dry_run:
        ui.info("IAM 전파 대기 (10초)...")
        import time

        time.sleep(10)
    return profile


# -- launch + EIP -------------------------------------------------------

def launch_instance(aws, game, instance_type, ami_id, key_name, sg_id,
                    profile_name, user_data):
    tag_spec = (
        f"ResourceType=instance,Tags=[{{Key=Name,Value=gameserver-{game}}},"
        f"{{Key=GameServer,Value={game}}}]"
    )
    args = [
        "ec2",
        "run-instances",
        "--image-id",
        ami_id,
        "--instance-type",
        instance_type,
        "--security-group-ids",
        sg_id,
        "--iam-instance-profile",
        f"Name={profile_name}",
        "--user-data",
        user_data,
        "--tag-specifications",
        tag_spec,
        "--count",
        "1",
    ]
    if key_name:
        args += ["--key-name", key_name]

    result = aws.run(args, mutating=True)
    if not result:
        return "i-dryrun"
    return result["Instances"][0]["InstanceId"]


def wait_running(aws, instance_id):
    if aws.dry_run:
        return
    ui.info("인스턴스 running 대기...")
    aws.run(
        ["ec2", "wait", "instance-running", "--instance-ids", instance_id],
        json_output=False,
    )
    ui.success("인스턴스 running")


def allocate_and_associate_eip(aws, instance_id, game):
    alloc = aws.run(
        [
            "ec2",
            "allocate-address",
            "--domain",
            "vpc",
            "--tag-specifications",
            f"ResourceType=elastic-ip,Tags=[{{Key=Name,Value=gameserver-{game}}}]",
        ],
        mutating=True,
    )
    if not alloc:
        return ("eipalloc-dryrun", "0.0.0.0")
    alloc_id = alloc["AllocationId"]
    public_ip = alloc["PublicIp"]
    aws.run(
        [
            "ec2",
            "associate-address",
            "--instance-id",
            instance_id,
            "--allocation-id",
            alloc_id,
        ],
        mutating=True,
    )
    return (alloc_id, public_ip)
