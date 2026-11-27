ARG BASE_IMAGE=scratch
FROM scratch AS patch_package
# Debian trixie patch 2.8-2+deb13u1 for amd64, used only while building.
ADD --checksum=sha256:1161166bbb69f0a95ceae88bd98dac4844091aef3b0b59ba33a956c526c460c5 \
    https://snapshot.debian.org/file/1734f0456685f843f1f3a8c9aad7d728ef3f08b2 /gnu-patch.deb

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG SOURCE_VERSION
ARG SOURCE_REVISION
LABEL org.opencontainers.image.base.name="${BASE_IMAGE}" \
      org.opencontainers.image.version="${SOURCE_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
RUN --network=none \
    --mount=type=bind,from=patch_package,source=/gnu-patch.deb,target=/tmp/gnu-patch.deb \
    --mount=type=bind,target=/tmp/adapter \
    --mount=type=tmpfs,target=/tmp/patch-tool \
    --mount=type=tmpfs,target=/data \
    test "$(cat /app/VERSION)" = "${SOURCE_VERSION}" && \
    dpkg-deb -x /tmp/gnu-patch.deb /tmp/patch-tool && \
    PATH="/tmp/patch-tool/usr/bin:${PATH}" python -B /tmp/adapter/install.py
