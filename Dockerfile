FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY soqindexer ./soqindexer
RUN pip install --no-cache-dir ".[postgres]"
# non-root
RUN useradd -r -u 10001 soq && mkdir -p /data && chown soq /data
USER soq
VOLUME ["/data"]
ENTRYPOINT ["soqindexer"]
CMD ["run"]
