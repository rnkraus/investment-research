#!/bin/sh

set -eu

CONFIG_PATH=/run/seaweedfs/s3.json
ADMIN_ACCESS_KEY_PATH=/run/secrets/seaweedfs_admin_access_key
ADMIN_SECRET_ACCESS_KEY_PATH=/run/secrets/seaweedfs_admin_secret_access_key
APPLICATION_ACCESS_KEY_PATH=/run/secrets/seaweedfs_application_access_key
APPLICATION_SECRET_ACCESS_KEY_PATH=/run/secrets/seaweedfs_application_secret_access_key

read_secret() {
    secret_path=$1
    secret_name=$2

    if [ ! -r "$secret_path" ]; then
        echo "required $secret_name secret is unavailable" >&2
        exit 1
    fi

    secret_value=$(cat "$secret_path")
    secret_length=${#secret_value}
    case "$secret_value" in
        "" | *[!A-Za-z0-9_-]*)
            echo "$secret_name must contain only letters, digits, underscores, and hyphens" >&2
            exit 1
            ;;
    esac
    if [ "$secret_length" -lt 16 ] || [ "$secret_length" -gt 128 ]; then
        echo "$secret_name must contain between 16 and 128 characters" >&2
        exit 1
    fi

    printf '%s' "$secret_value"
}

bucket=${INVESTMENT_RESEARCH_S3_BUCKET:?INVESTMENT_RESEARCH_S3_BUCKET is required}
if ! printf '%s' "$bucket" | grep -Eq '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'; then
    echo "INVESTMENT_RESEARCH_S3_BUCKET is not a portable S3 bucket name" >&2
    exit 1
fi
case "$bucket" in
    *..* | *.-* | *-.*)
        echo "INVESTMENT_RESEARCH_S3_BUCKET contains invalid adjacent punctuation" >&2
        exit 1
        ;;
esac
if printf '%s' "$bucket" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    echo "INVESTMENT_RESEARCH_S3_BUCKET must not be formatted as an IP address" >&2
    exit 1
fi

admin_access_key=$(read_secret "$ADMIN_ACCESS_KEY_PATH" "administrator access key")
admin_secret_access_key=$(read_secret "$ADMIN_SECRET_ACCESS_KEY_PATH" "administrator secret key")
application_access_key=$(read_secret "$APPLICATION_ACCESS_KEY_PATH" "application access key")
application_secret_access_key=$(
    read_secret "$APPLICATION_SECRET_ACCESS_KEY_PATH" "application secret key"
)

if [ "$admin_access_key" = "$application_access_key" ] || \
    [ "$admin_secret_access_key" = "$application_secret_access_key" ]; then
    echo "administrator and application credentials must be distinct" >&2
    exit 1
fi

umask 077
printf '{"identities":[{"name":"bootstrap-administrator","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","List","Tagging","Write"]},{"name":"investment-research-application","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Read:%s","Write:%s","List:%s"]}]}\n' \
    "$admin_access_key" \
    "$admin_secret_access_key" \
    "$application_access_key" \
    "$application_secret_access_key" \
    "$bucket" \
    "$bucket" \
    "$bucket" >"$CONFIG_PATH"

unset admin_access_key admin_secret_access_key application_access_key application_secret_access_key

exec /usr/bin/weed s3 \
    -filer=seaweedfs-filer:8888 \
    -ip=seaweedfs-s3 \
    -ip.bind=0.0.0.0 \
    -port=8333 \
    -config="$CONFIG_PATH" \
    -iam=false \
    -autoCreateBucket=false \
    -allowDeleteBucketNotEmpty=false \
    -allowedOrigins= \
    -port.iceberg=0 \
    -port.lance=0
