"""Route53 hosted-zone lookup and the Nginx + Let's Encrypt setup that
fronts a game server at <subdomain>.<zone> over HTTPS. The subdomain is
user-supplied (default <game>.game), so the zone apex is also reachable.

When no hosted zone is available the caller falls back to http://<eip>:<port>.
"""

import json

from . import ui


def list_hosted_zones(aws):
    """Return [{id, name}] with trailing dots stripped from names."""
    result = aws.run(["route53", "list-hosted-zones"], check=False)
    zones = []
    for z in (result or {}).get("HostedZones", []):
        zones.append(
            {
                "id": z["Id"].split("/")[-1],
                "name": z["Name"].rstrip("."),
            }
        )
    return zones


def default_subdomain(game):
    return f"{game}.game"


def fqdn_for(subdomain, zone_name):
    """Assemble an FQDN from a subdomain prefix and a zone. An empty
    subdomain resolves to the zone apex."""
    subdomain = subdomain.strip(". ")
    return f"{subdomain}.{zone_name}" if subdomain else zone_name


def upsert_a_record(aws, zone_id, fqdn, ip, ttl=300):
    change = {
        "Changes": [
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": fqdn,
                    "Type": "A",
                    "TTL": ttl,
                    "ResourceRecords": [{"Value": ip}],
                },
            }
        ]
    }
    aws.run(
        [
            "route53",
            "change-resource-record-sets",
            "--hosted-zone-id",
            zone_id,
            "--change-batch",
            json.dumps(change),
        ],
        mutating=True,
    )
    ui.success(f"A 레코드 설정: {fqdn} -> {ip}")


def render_nginx_certbot_script(game, fqdn, email, app_port):
    """In-instance script: install Nginx + certbot, reverse-proxy to the
    container, and obtain a certificate (HTTP->HTTPS redirect)."""
    return f"""#!/bin/bash
set -euo pipefail
dnf install -y nginx certbot python3-certbot-nginx
systemctl enable nginx
systemctl start nginx
rm -f /etc/nginx/conf.d/default.conf

cat > /etc/nginx/conf.d/{game}.conf <<'NGINX'
limit_req_zone  $binary_remote_addr zone={game}_req:10m rate=20r/s;
limit_conn_zone $binary_remote_addr zone={game}_conn:10m;
map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
}}
upstream {game}_backend {{
    server 127.0.0.1:{app_port};
    keepalive 64;
}}
server {{
    listen 80;
    server_name {fqdn};

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;

    location = /healthz {{
        return 200 '{game} OK';
        add_header Content-Type text/plain;
    }}
    location / {{
        limit_req  zone={game}_req burst=40 nodelay;
        limit_conn {game}_conn 30;
        proxy_pass http://{game}_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}
}}
NGINX

nginx -t && systemctl reload nginx

certbot_with_retry() {{
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if certbot "$@"; then
            return 0
        fi
        if [ "$attempt" = "6" ]; then
            return 1
        fi
        echo "certbot failed; retrying in 20s ($attempt/6)"
        sleep 20
    done
}}

certbot_with_retry --nginx -d {fqdn} --non-interactive --agree-tos \
    --email {email} --redirect

systemctl enable certbot-renew.timer
systemctl start certbot-renew.timer
nginx -t && systemctl reload nginx
echo "HTTPS ready: https://{fqdn}"
"""
