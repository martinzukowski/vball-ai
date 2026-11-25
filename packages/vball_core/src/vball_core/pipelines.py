"""Pipeline version identifiers shared by the API (which enqueues) and workers (which run).
Bump a version whenever its output changes; results record the version that produced them."""

INGEST = "ingest-1"
RALLY_HEURISTIC = "rally-heuristic-1"
RALLY_LEARNED = "rally-learned-1"
