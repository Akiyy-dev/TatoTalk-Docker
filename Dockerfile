# syntax=docker/dockerfile:1
ARG ANDROID_BASE_IMAGE=budtmo/docker-android:emulator_11.0@sha256:ee03fa21a88ea86fe5be803936391d82ee1a853cb19ac6a6e0800ebfcaaf6dc2
FROM ${ANDROID_BASE_IMAGE}
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends aapt python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/probe.py scripts/start.sh /opt/tatotalk/
RUN chmod 755 /opt/tatotalk/start.sh \
    && python3 /opt/tatotalk/probe.py --help > /dev/null \
    && aapt version \
    && /bin/sh -n /opt/tatotalk/start.sh
USER androidusr
CMD ["/opt/tatotalk/start.sh"]
