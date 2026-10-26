-- Per-app settings and members (D-MINIAPP-3). Rights rows hang off the versioned_artifact
-- row: the delete of that row drops them (ON DELETE CASCADE), so no observer must succeed.
-- No CHECK for "exactly one of user_id or role": MySQL refuses a CHECK on a column with a
-- referential action (error 3823). settings.put_member, the only writer, enforces it.
CREATE TABLE IF NOT EXISTS miniapp (
    artifact_code VARCHAR(64) NOT NULL PRIMARY KEY,
    title VARCHAR(255) NULL,
    icon VARCHAR(32) NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_miniapp_artifact FOREIGN KEY (artifact_code)
        REFERENCES versioned_artifact (artifact_code) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS miniapp_member (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    artifact_code VARCHAR(64) NOT NULL,
    user_id INT UNSIGNED NULL,
    role VARCHAR(16) NULL,
    can_edit TINYINT(1) NOT NULL DEFAULT 0,
    can_remove TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_member_user (artifact_code, user_id),
    UNIQUE KEY uq_member_role (artifact_code, role),
    CONSTRAINT fk_member_artifact FOREIGN KEY (artifact_code)
        REFERENCES versioned_artifact (artifact_code) ON DELETE CASCADE,
    CONSTRAINT fk_member_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE,
    CONSTRAINT fk_member_role FOREIGN KEY (role) REFERENCES role (code) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The full action set of the approved version's manifest. NULL (an approval made before this
-- file): no execute for a user who is not admin until an editor approves again.
ALTER TABLE miniapp_activation ADD COLUMN manifest_actions JSON NULL AFTER allowed_actions;
