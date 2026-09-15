FROM redis:8.2.9-alpine3.22@sha256:30abb90e62f14b737010746def3ba99cc79fe19dcdb3d37b41f21fc62e7da19d

RUN apk add --no-cache --upgrade \
        libcrypto3=3.5.8-r0 \
        libssl3=3.5.8-r0 \
        setpriv=2.41.6-r1 \
    && apk info -e 'libcrypto3=3.5.8-r0' \
    && apk info -e 'libssl3=3.5.8-r0' \
    && apk info -e 'setpriv=2.41.6-r1' \
    && redis-server --version

COPY --chmod=0555 docker/redis/start-redis.sh /usr/local/bin/start-investment-research-redis
