FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

RUN chmod +x /app/docker/entrypoint.sh /app/docker/run-task.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
