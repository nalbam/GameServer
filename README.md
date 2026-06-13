# GameServer

AWS에 Docker 게임 서버(SnowClash / TankClash 형식)를 배포·운영하는 대화형 CLI 도구입니다.
로컬에서 실행하며 AWS를 오케스트레이션합니다 — 인증 확인, 서버 레지스트리(SSM) 관리,
EC2 + EIP 생성, SSM Run Command 배포/재배포, Route53 도메인 + HTTPS 연결까지 한 번에 처리합니다.

```
./gameserver.py
```

## 동작 흐름

1. **인증/리전 확인** — `aws sts get-caller-identity`로 계정·ARN 확인, 리전 자동 감지(미설정 시 선택)
2. **서버 목록** — SSM `/gameserver/*` 레지스트리 + 각 EC2 실제 상태를 표로 표시
3. **새 서버 생성**
   - 게임 선택(카탈로그) 또는 직접 입력
   - 인스턴스 타입 선택, 키 페어 선택(없으면 SSM만 사용)
   - 보안그룹 생성(SSH=내 IP, HTTP 80, HTTPS 443, 앱 포트 2567)
   - IAM 인스턴스 프로파일 보장(SSM Core + env 읽기 권한)
   - Amazon Linux 2023로 EC2 생성 → EIP 생성·연결 → 레지스트리 기록
   - 생성 직후 `http://<eip>:<port>`로 접속 가능
4. **재배포 / 새 버전** — GitHub releases에서 버전 선택 → SSM Run Command로 `docker pull` + 재시작
5. **도메인 연결** — 호스팅영역 선택 → `<game>.game.<zone>` A 레코드(EIP) → Nginx + Let's Encrypt HTTPS
   - 호스팅영역이 없으면 `http://<eip>:<port>`로 접속

## 사전 조건

### AWS 자격증명
`aws configure` 또는 SSO 등으로 자격증명이 설정되어 있어야 합니다. 추가 의존성(boto3 등)은 없으며
이미 설치된 AWS CLI를 사용합니다.

### 운영자(로컬) IAM 권한
도구를 실행하는 주체에 다음 권한이 필요합니다:

| 서비스 | 액션 |
|--------|------|
| STS | `sts:GetCallerIdentity` |
| SSM | `ssm:GetParameter*`, `ssm:PutParameter`, `ssm:DeleteParameter`, `ssm:GetParametersByPath`, `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:DescribeInstanceInformation` |
| EC2 | `ec2:Describe*`, `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`, `ec2:RunInstances`, `ec2:AllocateAddress`, `ec2:AssociateAddress`, `ec2:TerminateInstances`, `ec2:ReleaseAddress` |
| IAM | `iam:GetInstanceProfile`, `iam:CreateRole`, `iam:AttachRolePolicy`, `iam:PutRolePolicy`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`, `iam:PassRole` |
| Route53 | `route53:ListHostedZones`, `route53:ChangeResourceRecordSets` |

### EC2 인스턴스 IAM 역할
도구가 `gameserver-ec2-role` / `gameserver-ec2-profile`을 자동 생성합니다:
- `AmazonSSMManagedInstanceCore` (SSM Run Command 배포에 필요)
- `/env/prod/*`, `/gameserver/*`에 대한 `ssm:GetParameter` 인라인 정책

## 데이터 모델 (SSM)

| 파라미터 | 타입 | 내용 |
|----------|------|------|
| `/gameserver/<game>` | String | 서버 레지스트리 JSON (instance_id, eip_alloc_id, public_ip, region, instance_type, domain, image, version, port, created_at) |
| `/env/prod/<game>` | SecureString | 컨테이너 런타임 env (기존 배포 스크립트와 호환) |

게임 env 예시:

```bash
aws ssm put-parameter --name /env/prod/snowclash --type SecureString --value "NODE_ENV=production
PORT=2567
SERVER_URL=snowclash.game.example.com
ALLOWED_ORIGINS=https://snowclash.game.example.com,https://nalbam.github.io"
```

## 게임 카탈로그

기본 등록된 게임 (직접 입력으로 추가 가능):

| 게임 | 이미지 | 포트 |
|------|--------|------|
| snowclash | `ghcr.io/nalbam/snowclash` | 2567 |
| tankclash | `ghcr.io/nalbam/tankclash` | 2567 |

## 옵션

| 옵션 | 설명 |
|------|------|
| `--region REGION` | AWS 리전 지정 (미지정 시 설정값/선택) |
| `--dry-run` | 변경 작업을 실행하지 않고 실행할 명령만 출력 |

## 트러블슈팅

- **SSM 명령이 실패/대기**: 인스턴스의 SSM 에이전트가 온라인인지 확인
  (`aws ssm describe-instance-information`). 인스턴스 프로파일에 `AmazonSSMManagedInstanceCore`가 필요합니다.
- **certbot 인증서 발급 실패**: A 레코드 전파 전이라면 잠시 후 '도메인 연결'을 다시 실행하세요.
  `dig +short <fqdn>`로 EIP를 가리키는지 확인합니다.
- **부트스트랩 미완료**: 생성 직후 인스턴스 내부에서 docker pull/run이 1~3분 진행됩니다.
  `http://<eip>:<port>` 응답이 없으면 잠시 기다린 뒤 재시도하세요.

## 구조

```
GameServer/
├── gameserver.py            # 대화형 CLI 진입점
├── lib/
│   ├── ui.py                # 메뉴 / 프롬프트 / 컬러 로깅
│   ├── aws.py               # AWS CLI subprocess 래퍼 (dry-run)
│   ├── registry.py          # SSM 서버 레지스트리
│   ├── ec2.py               # AMI / 키페어 / 보안그룹 / IAM / 인스턴스 / EIP
│   ├── deploy.py            # 버전 선택 + SSM Run Command 배포
│   └── route53.py           # 호스팅영역 / A 레코드 / Nginx+Certbot
└── templates/
    └── ec2-user-data.sh     # EC2 부트스트랩 (토큰 치환)
```
