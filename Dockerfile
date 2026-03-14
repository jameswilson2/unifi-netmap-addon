ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python3
RUN apk add --no-cache python3

# Copy add-on files
COPY run.sh /run.sh
COPY server.py /server.py
COPY www/ /www/

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
