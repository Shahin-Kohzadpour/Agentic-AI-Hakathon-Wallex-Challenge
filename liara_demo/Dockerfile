FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app
COPY site/ /app/site/
COPY server.py /app/server.py

USER 65532:65532
EXPOSE 8080
CMD ["python", "/app/server.py"]
