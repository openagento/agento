-- 060: the tool catalog (framework/tool_catalog.py). One row per (source, tool name), synced from
-- the enabled module manifests by `tool:sync-catalog`. The sync never deletes a row: a tool gone
-- from every manifest is marked `present = 0`, so the admin's label overrides survive its return.
-- Effective labels = default_labels, plus the `add` rows, minus the `remove` rows.
-- A tool is reviewed while reviewed_hash equals contract_hash. A contract change bumps `revision`.
CREATE TABLE IF NOT EXISTS tool_catalog (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(128) NOT NULL,
    tool_name VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    alias VARCHAR(191) NOT NULL,
    toolset VARCHAR(128) NULL,
    description TEXT NULL,
    default_labels JSON NOT NULL,
    contract_hash CHAR(40) NOT NULL,
    reviewed_hash CHAR(40) NULL,
    revision INT NOT NULL DEFAULT 0,
    present TINYINT(1) NOT NULL DEFAULT 1,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_tool_source_name (source, tool_name),
    KEY idx_tool_alias (alias)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tool_label (
    id INT AUTO_INCREMENT PRIMARY KEY,
    label_key VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    name_norm VARCHAR(100) NOT NULL UNIQUE,
    is_protected TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tool_label_assignment (
    tool_id INT NOT NULL,
    label_id INT NOT NULL,
    mode ENUM('add','remove') NOT NULL,
    PRIMARY KEY (tool_id, label_id),
    CONSTRAINT fk_tla_tool FOREIGN KEY (tool_id) REFERENCES tool_catalog(id) ON DELETE CASCADE,
    CONSTRAINT fk_tla_label FOREIGN KEY (label_id) REFERENCES tool_label(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO tool_label (label_key, name, name_norm, is_protected) VALUES
    ('private-data', 'Private data access', 'private data access', 1),
    ('untrusted-content', 'Untrusted content', 'untrusted content', 1),
    ('external-communication', 'External communication', 'external communication', 1),
    ('stub-in-test', 'Stub in test', 'stub in test', 1);
