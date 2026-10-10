-- Roles are rows (panel Roles tab). `admin` and `user` are seeded; an admin adds more.
-- `admin` stays the one code with the built-in admin operations (accounts.may), see DECISIONS.md.
-- The role CHECKs go first: MySQL refuses an FK with a referential action on a column that a
-- CHECK names (ER 3823). `user.role` has no action, so a role with users cannot be deleted;
-- a role's grants go with it.
CREATE TABLE IF NOT EXISTS role (
    code       VARCHAR(16) NOT NULL PRIMARY KEY,
    label      VARCHAR(64) NOT NULL,
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_role_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO role (code, label) VALUES ('admin', 'Administrator'), ('user', 'User');

ALTER TABLE `user` DROP CHECK chk_user_role;
ALTER TABLE `user` ADD CONSTRAINT fk_user_role FOREIGN KEY (role) REFERENCES role (code);
ALTER TABLE role_grant DROP CHECK chk_role_grant_role;
ALTER TABLE role_grant ADD CONSTRAINT fk_role_grant_role
    FOREIGN KEY (role) REFERENCES role (code) ON DELETE CASCADE;
