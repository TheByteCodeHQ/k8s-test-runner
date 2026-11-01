FROM python:3.12-slim

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ktr
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER 10001
EXPOSE 8080
ENTRYPOINT ["ktr-server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
