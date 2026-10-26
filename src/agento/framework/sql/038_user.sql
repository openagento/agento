-- Platform users (E1.5, PRD E2 §5). Login and RBAC logic are E2's.
-- `user` is a non-reserved keyword in MySQL 8: always backtick it.
CREATE TABLE IF NOT EXISTS `user` (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(191) NOT NULL,
    -- NULL until a password is set: such a user cannot log in with one.
    password_hash VARCHAR(255) NULL,
    -- No default: every user is given a role on purpose.
    role          VARCHAR(16)  NOT NULL,
    is_active     TINYINT(1)   NOT NULL DEFAULT 1,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_username (username),
    CONSTRAINT chk_user_role CHECK (role IN ('admin', 'user'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
