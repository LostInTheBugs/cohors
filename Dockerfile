# syntax=docker/dockerfile:1

# L'app ne voit plus /var/run/docker.sock : elle soumet ses simulations au
# service `worker` via un socket Unix partagé (voir docker-compose.yml). Cette
# image n'a donc PLUS besoin du client docker, et le conteneur tourne sans
# droits root (uid/gid : voir la directive USER ci-dessous, surchargée par
# compose via APP_UID).

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt VERSION ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY shared ./shared
COPY addon ./addon

# gid 10001 = groupe du socket du worker, partagé avec personne d'autre.
USER 1000:10001

ARG PORT=8030
ENV PORT=${PORT}
EXPOSE ${PORT}

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
