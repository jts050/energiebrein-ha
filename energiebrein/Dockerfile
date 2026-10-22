# Vaste basis (Debian met Python). Home Assistant gebruikt build.yaml niet altijd meer,
# en dan zou het een Alpine-basis zonder apt-get pakken.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENERGIEBREIN_DATA=/config/data

WORKDIR /app
COPY app/requirements.txt app/requirements-auto.txt /app/
RUN pip install --prefer-binary -r /app/requirements.txt \
 && (pip install --prefer-binary -r /app/requirements-auto.txt || echo 'autokoppeling overgeslagen')

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
