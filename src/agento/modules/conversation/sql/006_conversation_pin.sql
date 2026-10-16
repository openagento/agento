-- A user pins a thread to the top of their own rail. The column is the pin AND its clock:
-- NULL is unpinned, and the timestamp orders the pinned group (newest pin first).
ALTER TABLE conversation ADD COLUMN pinned_at TIMESTAMP NULL;

-- The owner list reads `user_id = ? AND status = 'active'` ordered by the pin first, and the
-- cap counts the user's pinned rows; both are this index.
ALTER TABLE conversation ADD KEY idx_user_pinned (user_id, status, pinned_at);
