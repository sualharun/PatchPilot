FROM python:3.11-slim

RUN python -m pip install --upgrade pip
WORKDIR /workspace
