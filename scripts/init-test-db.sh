#!/bin/bash
# Creates the hh_monitor_test database on first volume initialization.
# This script is mounted into /docker-entrypoint-initdb.d/ and runs only
# once when the PostgreSQL data volume is first created.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE hh_monitor_test;
    GRANT ALL PRIVILEGES ON DATABASE hh_monitor_test TO hh_monitor;
EOSQL
