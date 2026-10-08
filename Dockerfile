FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && rm -rf /build

WORKDIR /
# certificates, pairings, cached wallbox description, traffic log
ENV STATE_DIR=/data
VOLUME /data

# needs host networking (mDNS): docker run --network host ...
ENTRYPOINT ["elli-eebus-proxy"]
CMD ["run"]
