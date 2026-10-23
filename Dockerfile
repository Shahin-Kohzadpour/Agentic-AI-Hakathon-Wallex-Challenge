FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 WALLEX_DATA_DIR=/data DROP_PRIVILEGES=1
WORKDIR /app
COPY requirements-live.txt /app/requirements-live.txt
RUN pip install --no-cache-dir -r /app/requirements-live.txt
COPY wallex_live/ /app/wallex_live/
COPY condor/ /app/condor/
COPY wallex_sandbox/__init__.py wallex_sandbox/fair_value_core.py wallex_sandbox/fair_value_feeds.py wallex_sandbox/live_paper.py wallex_sandbox/sandbox.py wallex_sandbox/wallex_fee_probe.py wallex_sandbox/excluded-markets.json /app/wallex_sandbox/
ARG SOURCE_REVISION=unrecorded
ENV SOURCE_REVISION=${SOURCE_REVISION}
EXPOSE 8080
CMD ["python", "-m", "wallex_live"]
