# Shared schedule-resolution helper. Sourced by run_soak.sh and run_demo.sh.
# Not executable on its own.
#
# resolve_schedule <input>
#   Accepts any of: "soak_45c", "soak_45c.yaml", "schedules/soak_45c.yaml".
#   Strips the optional schedules/ prefix and .yaml suffix, validates the
#   resulting id is composed only of [A-Za-z0-9_-], confirms the file exists,
#   and exports three globals for the caller:
#       SCHEDULE       — the canonical bare id (e.g. "soak_45c")
#       SCHEDULE_FILE  — the resolved YAML path (e.g. "schedules/soak_45c.yaml")
#       SCHEDULE_ID    — alias for SCHEDULE; safe to interpolate into SQL
#                        equality predicates (the validator forbids quotes,
#                        semicolons, %, spaces). Do NOT interpolate into a
#                        LIKE pattern — schedule names contain `_` which is
#                        SQL's single-char wildcard.
#
# Fatal on bad input (prints to stderr and exits the caller).

resolve_schedule() {
    local input="$1" prog="${2:-schedule}"
    if [[ -z "$input" ]]; then
        echo "[$prog] schedule input is empty" >&2
        exit 1
    fi
    # Normalize: strip schedules/ prefix and trailing .yaml.
    local id="${input#schedules/}"
    id="${id%.yaml}"
    # Validate. Rejects empty, paths with /, quotes, %, ;, spaces — anything
    # that would be unsafe to drop into an SQL string literal or a path.
    if [[ ! "$id" =~ ^[A-Za-z0-9_-]+$ ]]; then
        echo "[$prog] invalid schedule id: $(printf '%q' "$id") (allowed: [A-Za-z0-9_-]+)" >&2
        exit 1
    fi
    local file="schedules/${id}.yaml"
    if [[ ! -f "$file" ]]; then
        echo "[$prog] schedule not found: $file" >&2
        exit 1
    fi
    SCHEDULE="$id"
    SCHEDULE_FILE="$file"
    SCHEDULE_ID="$id"
}
