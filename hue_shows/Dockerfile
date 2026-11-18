ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python for the engine; openssl provides libssl.so.3 (DTLS-PSK for Hue Entertainment, via ctypes)
RUN apk add --no-cache python3 openssl

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
