#!/bin/sh
set -eu

readonly secret_file=/run/secrets/redis_password
readonly acl_file=/run/redis/users.acl

if [ ! -r "${secret_file}" ]; then
  echo "Redis password secret is unavailable." >&2
  exit 1
fi

password="$(cat "${secret_file}")"
if [ "${#password}" -lt 16 ]; then
  echo "Redis password must contain at least 16 characters." >&2
  exit 1
fi

password_hash="$(printf '%s' "${password}" | sha256sum)"
password_hash="${password_hash%% *}"
unset password

umask 077
printf 'user default on #%s ~* &* +@all\n' "${password_hash}" >"${acl_file}"
unset password_hash

exec redis-server \
  --aclfile "${acl_file}" \
  --appendonly no \
  --bind 0.0.0.0 \
  --maxmemory 128mb \
  --maxmemory-policy noeviction \
  --port 6379 \
  --protected-mode yes \
  --save ""
