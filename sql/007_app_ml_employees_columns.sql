-- 007: app_ml reads only the employee columns the model needs (decision D22).
-- The trainer joins claims to employees for `grade`; it has no use for names or emails.

REVOKE SELECT ON employees FROM app_ml;
GRANT SELECT (employee_id, grade) ON employees TO app_ml;
