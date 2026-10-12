-- Admin panel operations are global role_grant rows (no workspace, no agent_view); see
-- the leaves in acl.json. The Miniapps screen is one of them now, so every role that is not
-- admin (admin holds them all) gets it, and nobody loses the screen.
INSERT INTO role_grant (role, grant_kind, name)
SELECT code, 'operation', 'miniapps' FROM role WHERE code <> 'admin';
