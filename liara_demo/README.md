# Liara read-only demo site

This is a small, public, read-only presentation site for the Wallex Challenge.
It is isolated from Condor's authenticated dashboard and never connects to
exchange accounts or submits orders. The Video Script lives in the GitHub docs
folder and is not copied into this site's Docker image.

## Local run

    python liara_demo/server.py

Open http://127.0.0.1:8080. The deployment health endpoint is /health.

## Liara

First authenticate with Liara and create the app if it does not already exist.
Choose the service plan and region in the Liara account. Then deploy:

    cd liara_demo
    liara app create --app wallex-challenge-demo --platform docker
    liara deploy --platform docker --port 8080 --app wallex-challenge-demo

The CLI will prompt for the plan/network if the app is new. Do not put a Liara
token in this repository. See Liara's
[Docker app guide](https://github.com/liara-cloud/docs/blob/master/src/pages/paas/docker/how-tos/create-app.mdx).
