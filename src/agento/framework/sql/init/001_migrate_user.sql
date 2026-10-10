-- The migration user (WS8): runs as root at initdb, and `agento upgrade` pipes it into an
-- existing MySQL. Idempotent. Both db forms: a table GRANT checks the image's exact
-- `cron\_agent` row (else MySQL 1142), a db GRANT the `_` pattern row (else 1044).
GRANT CREATE USER ON *.* TO 'cron_agent'@'%';
GRANT ALL PRIVILEGES ON `cron_agent`.* TO 'cron_agent'@'%' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON `cron\_agent`.* TO 'cron_agent'@'%' WITH GRANT OPTION;
