# syntax=docker/dockerfile:1

# The app talks to the host Docker daemon (mounted socket) to launch
# SimulationCraft containers, so it needs the Docker CLI inside the image.
FROM docker:cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt VERSION ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker ./worker

ARG PORT=8030
ENV PORT=${PORT}
EXPOSE ${PORT}

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
