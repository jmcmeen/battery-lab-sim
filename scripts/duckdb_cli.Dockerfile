# Minimal DuckDB CLI image — pulls the official duckdb binary release.
# Used only by the `make duckdb` profile-gated compose service.
FROM debian:bookworm-slim
ARG DUCKDB_VERSION=v1.1.3
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip gettext-base && \
    arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        amd64) zip="duckdb_cli-linux-amd64.zip" ;; \
        arm64) zip="duckdb_cli-linux-aarch64.zip" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/duckdb.zip \
        "https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/${zip}" && \
    unzip /tmp/duckdb.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/duckdb && \
    rm /tmp/duckdb.zip && \
    apt-get purge -y curl unzip && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*
COPY duckdb_entrypoint.sh /usr/local/bin/duckdb-init
RUN chmod +x /usr/local/bin/duckdb-init
ENTRYPOINT ["/usr/local/bin/duckdb-init"]
