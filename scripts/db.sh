#!/bin/bash
# Database management script for Docker containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# The interactive shells below resolve their credentials INSIDE the container,
# from the environment Compose already populated from .env. Nothing here parses
# .env: a host-side parser has to reimplement dotenv (quoting, inline comments,
# `export ` prefixes, duplicate-key precedence) and any disagreement with
# Compose's parser produces a confusing auth failure against a database that is
# configured correctly. The container's own environment is the same value the
# server was started with, by construction.
#
# It also keeps secrets out of the host process list: the password is never an
# argument, on either side of `docker exec`.

case "$1" in
    start)
        echo "Starting database containers..."
        docker-compose up -d
        echo ""
        echo "Waiting for databases to be ready..."
        sleep 5
        echo ""
        echo "Database status:"
        docker-compose ps
        echo ""
        echo "PostgreSQL: localhost:5432"
        echo "ClickHouse HTTP: localhost:8123"
        echo "ClickHouse Native: localhost:9000"
        ;;
    stop)
        echo "Stopping database containers..."
        docker-compose stop
        ;;
    restart)
        echo "Restarting database containers..."
        docker-compose restart
        ;;
    status)
        docker-compose ps
        ;;
    logs)
        docker-compose logs -f "${2:-}"
        ;;
    down)
        echo "Stopping and removing containers..."
        docker-compose down
        ;;
    reset)
        echo "⚠️  WARNING: This will delete all database data!"
        read -p "Are you sure? (yes/no): " confirm
        if [ "$confirm" = "yes" ]; then
            docker-compose down -v
            echo "All data removed. Run './scripts/db.sh start' to recreate."
        else
            echo "Cancelled."
        fi
        ;;
    psql)
        # POSTGRES_USER / POSTGRES_DB are expanded by the container's shell, not
        # this one, hence the single quotes. The defaults match docker-compose.
        docker exec -it tms-postgres sh -c \
            'exec psql -U "${POSTGRES_USER:-tms_user}" -d "${POSTGRES_DB:-tms_db}"'
        ;;
    clickhouse)
        # clickhouse-client reads CLICKHOUSE_PASSWORD from its own environment,
        # which the official image already carries, so no credential has to
        # cross from the host. Do NOT reintroduce `docker exec -e
        # CLICKHOUSE_PASSWORD`: for a variable unset in the invoking shell that
        # flag STRIPS the container's own value rather than passing it through,
        # turning a working shell into `Code: 516 Authentication failed` on
        # exactly the password-protected deployments it looks like it is for.
        docker exec -it tms-clickhouse sh -c \
            'exec clickhouse-client --user "${CLICKHOUSE_USER:-default}"'
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|down|reset|psql|clickhouse}"
        echo ""
        echo "Commands:"
        echo "  start       - Start all database containers"
        echo "  stop        - Stop all database containers"
        echo "  restart     - Restart all database containers"
        echo "  status      - Show container status"
        echo "  logs [svc]  - Show logs (optionally for specific service)"
        echo "  down        - Stop and remove containers"
        echo "  reset       - Stop, remove containers and volumes (⚠️ deletes data)"
        echo "  psql        - Connect to PostgreSQL via psql"
        echo "  clickhouse  - Connect to ClickHouse via clickhouse-client"
        exit 1
        ;;
esac
