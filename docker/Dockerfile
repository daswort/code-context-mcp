FROM busybox:1.36.1 AS healthcheck

FROM chromadb/chroma:1.5.6

COPY --from=healthcheck /bin/busybox /usr/local/bin/busybox