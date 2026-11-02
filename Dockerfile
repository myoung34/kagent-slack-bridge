FROM python:3.13.18-slim AS base
LABEL org.opencontainers.image.source=https://github.com/myoung34/kagent-slack-bridge
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# Lint and unit tests run as part of the build, so a failure fails the image.
FROM base AS test
COPY requirements-dev.txt pyproject.toml test_bot.py ./
RUN pip install --no-cache-dir -r requirements-dev.txt \
 && ruff check . && ruff format --check . \
 && python -m pytest -q \
 && touch /tested

FROM base
# Depend on the test stage so BuildKit cannot skip it.
COPY --from=test /tested /tested
USER 65534:65534
CMD ["python", "-u", "bot.py"]
