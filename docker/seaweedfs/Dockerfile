FROM chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882

LABEL org.opencontainers.image.description="Local-only SeaweedFS runtime for Investment Research Archive"

USER 0:0

RUN mkdir -p /data/master /data/volume /data/filer /run/seaweedfs \
    && chown -R 65532:65532 /data /run/seaweedfs

COPY --chmod=0555 docker/seaweedfs/start-s3.sh /usr/local/bin/start-seaweedfs-s3

USER 65532:65532
WORKDIR /data
