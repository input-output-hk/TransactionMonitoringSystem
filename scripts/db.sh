#!/bin/bash
# Database management script for Docker containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

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
        docker exec -it tms-postgres psql -U tms_user -d tms_db
        ;;
    clickhouse)
        docker exec -it tms-clickhouse clickhouse-client
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
