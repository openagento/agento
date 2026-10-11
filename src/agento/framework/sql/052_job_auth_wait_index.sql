-- Jobs that wait for a credential (error_class = 'CredentialsUnavailableError').
-- The wake observer reads at most 200 of them in id order, from this index only.
CREATE INDEX idx_job_auth_wait ON job (status, error_class, id);
