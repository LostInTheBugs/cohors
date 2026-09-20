# syntax=docker/dockerfile:1

# Le worker de simulation : la SEULE partie qui voit /var/run/docker.sock.
# Minuscule et sans dépendances applicatives (python stdlib + le client docker
# pour lancer les conteneurs simc) ; il ne parle qu'à l'app, via un socket Unix.
# Tout ce qu'il reçoit est re-validé (shared/simvalidate.py) : il ne fait jamais
# confiance à ce que l'app lui envoie.

FROM docker:cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /w
COPY VERSION ./VERSION
COPY worker ./worker
COPY shared ./shared

CMD ["python", "-m", "worker.simworker"]
