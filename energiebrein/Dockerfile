ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENERGIEBREIN_DATA=/config/data

WORKDIR /app
COPY app/requirements.txt app/requirements-auto.txt /app/
# gcc alleen tijdens het bouwen, voor het geval een onderdeel geen kant-en-klaar pakket heeft
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && pip install --prefer-binary -r /app/requirements.txt \
 && (pip install --prefer-binary -r /app/requirements-auto.txt || echo 'autokoppeling overgeslagen') \
 && apt-get purge -y gcc libc6-dev && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
