-- Initialize databases for vreview and hatchet
CREATE DATABASE hatchet;
GRANT ALL PRIVILEGES ON DATABASE hatchet TO vreview;

\connect vreview;
CREATE EXTENSION IF NOT EXISTS vector;
