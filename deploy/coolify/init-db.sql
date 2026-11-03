-- Runs once, when Postgres starts on an empty data directory (docker-entrypoint-initdb.d).
-- One server, three databases: the app (vreview), the Hatchet workflow engine, and the web app's better-auth tables.
CREATE DATABASE vreview;
CREATE DATABASE hatchet;
CREATE DATABASE vreview_auth;

\connect vreview
CREATE EXTENSION IF NOT EXISTS vector;
