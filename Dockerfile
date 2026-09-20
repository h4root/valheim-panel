FROM debian:bookworm-slim

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl lib32gcc-s1 python3 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/steamcmd && \
    curl -sL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
        | tar zxf - -C /opt/steamcmd

COPY panel /panel
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/server"]
EXPOSE 2456/udp 2457/udp 3030/tcp

ENTRYPOINT ["/entrypoint.sh"]
