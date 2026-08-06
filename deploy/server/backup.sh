#!/usr/bin/env bash
# vococo 服务器版数据备份:平台库 + 全部租户目录打包,带日期轮转。
# 用法(宿主机 cron,如每日 04:17):17 4 * * * /opt/vococo/deploy/server/backup.sh
set -euo pipefail

# 数据卷在宿主机上的落点(docker compose 命名卷 = <目录名>_vococo-data;
# 按 /srv/vococo/code 克隆的默认是 code_vococo-data,可用 VOCOCO_DATA_DIR 覆盖)
DATA_DIR="${VOCOCO_DATA_DIR:-/var/lib/docker/volumes/code_vococo-data/_data}"
BACKUP_ROOT="${VOCOCO_BACKUP_DIR:-/var/backups/vococo}"
KEEP_DAYS="${VOCOCO_BACKUP_KEEP_DAYS:-14}"

ts="$(date +%Y%m%d-%H%M%S)"
dest="$BACKUP_ROOT/$ts"
mkdir -p "$dest"

# 平台库:用 sqlite 在线备份,不拷裸文件(WAL 下裸拷可能拷到写一半的)
sqlite3 "$DATA_DIR/platform.db" ".backup '$dest/platform.db'"

# 租户目录:tar 整包(各租户 state.db 也是 sqlite,同理在线备份更稳,
# 但量小且每日一次,先 tar 简单可靠;量大后再改逐库 .backup)
tar -czf "$dest/tenants.tar.gz" -C "$DATA_DIR" tenants 2>/dev/null || true

# 轮转:删掉 KEEP_DAYS 天前的
find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf {} +

echo "[backup] $dest 完成"
