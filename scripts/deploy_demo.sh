#!/usr/bin/env bash
# 演示环境一键部署：幂等可重跑。前置：Linux + Docker 26 + Compose >= 2.24 + root。
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
HTPASSWD="deploy/nginx/htpasswd"
COMPOSE=(docker compose --env-file "$ENV_FILE" -p ragkb-demo \
  -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml)
log() { printf '\n==> %s\n' "$*"; }
rand() { openssl rand -hex "${1:-16}"; }

log "前置检查"
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker compose version >/dev/null || { echo "缺少 docker compose v2"; exit 1; }
docker compose version | grep -Eq 'v2\.(2[4-9]|[3-9][0-9])' || \
  echo "警告：Compose 低于 v2.24，ports: !reset 可能不支持"
if ss -ltn 2>/dev/null | grep -q ':18000 ' && \
   ! docker ps --format '{{.Names}}' | grep -q '^ragkb-demo-'; then
  echo "18000 被其他进程占用，拒绝继续"; exit 1
fi
avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
[ "${avail_kb:-0}" -ge 5242880 ] || { echo "根分区可用空间不足 5G"; exit 1; }

if [ ! -f "$ENV_FILE" ]; then
  log "生成 $ENV_FILE"
  umask 077
  cat > "$ENV_FILE" <<EOF
DB_PASSWORD=$(rand 16)
DB_APP_USER=kb_app
DB_APP_PASSWORD=$(printf '%s' "$DB_PASSWORD")
STORAGE_BACKEND=s3
S3_ENDPOINT=http://minio:9000
S3_BUCKET=rag-kb
S3_ACCESS_KEY=rag-app
S3_SECRET_KEY=$(rand 16)
MINIO_ROOT_USER=ragkb
MINIO_ROOT_PASSWORD=$(rand 16)
MINIO_VIEWER_USER=rag-viewer
MINIO_VIEWER_PASSWORD=$(rand 16)
EMBED_MODE=real
EMBED_MODEL=${EMBED_MODEL:-BAAI/bge-m3}
DEMO_USER=demo
DEMO_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')
EOF
fi
chmod 600 "$ENV_FILE"

# 把当前 shell 的 LLM_*/EMBED_* 补进 .env（不覆盖已有值）
for key in LLM_API_KEY LLM_BASE_URL LLM_MODEL EMBED_API_KEY EMBED_BASE_URL EMBED_MODEL; do
  value="${!key:-}"
  if [ -n "$value" ] && ! grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
done
set -a; . "./$ENV_FILE"; set +a
[ -n "${EMBED_API_KEY:-}" ] || echo "警告：EMBED_API_KEY 为空，入库会退回 mock 向量"
[ -n "${LLM_API_KEY:-}" ] || echo "警告：LLM_API_KEY 为空，问答不可用"

if [ ! -f "$HTPASSWD" ]; then
  log "生成 Basic Auth 凭据"
  # nginx alpine worker 用 uid 101 跑，需要可读 htpasswd；bcrypt 加盐后 644 足够安全
  umask 022
  docker run --rm httpd:2.4-alpine \
    htpasswd -nbB "${DEMO_USER:-demo}" "${DEMO_PASSWORD}" > "$HTPASSWD"
  chmod 644 "$HTPASSWD"
fi

log "构建并启动"
"${COMPOSE[@]}" up -d --build
log "等待数据库"
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T db pg_isready -U kb -d kb >/dev/null 2>&1 && break
  sleep 2
done
log "迁移"
"${COMPOSE[@]}" exec -T api alembic upgrade head
log "等待应用就绪"
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T api python -c \
    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" \
    >/dev/null 2>&1 && break
  sleep 2
done

if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  log "放行 18000/tcp"
  firewall-cmd --permanent --add-port=18000/tcp >/dev/null
  firewall-cmd --reload >/dev/null
fi

log "健康检查"
curl -fsS http://127.0.0.1:18000/health >/dev/null && echo "/health OK"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/ui/)
[ "$code" = "401" ] && echo "/ui 未鉴权 401 OK" || { echo "/ui 期望 401，实际 $code"; exit 1; }
curl -fsS -u "${DEMO_USER}:${DEMO_PASSWORD}" -H 'X-Tenant-Id: demo' \
  http://127.0.0.1:18000/v1/storage >/dev/null && echo "鉴权访问 OK"

log "完成"
echo "入口：http://<server-ip>:18000/ui/"
echo "账号：${DEMO_USER} / ${DEMO_PASSWORD}（见 $(pwd)/.env，勿外传）"
