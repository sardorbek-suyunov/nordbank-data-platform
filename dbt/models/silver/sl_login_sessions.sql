-- Login sessions, the latest state of each, a soft-deleted one absent (specification 008
-- sections 2 and 4).
{{ latest_state(ref('br_corebank__login_sessions'), ['login_session_id']) }}
