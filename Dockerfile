# Needs NO downloads at build time except the base image from Docker Hub:
#  - the full python image already has git and bash (no apt-get)
#  - the runner uses only the Python standard library (no pip install)
FROM python:3.12

# Optional PyPI mirror, used only by the agent's own `pip install` commands
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

RUN useradd -m -u 1000 sandbox \
 && python -m venv /opt/venv \
 && mkdir -p /app /data/workspaces /data/archives \
 && chown -R sandbox:sandbox /opt/venv /app /data

ENV PATH="/opt/venv/bin:$PATH" \
    SANDBOX_HOME=/home/sandbox \
    PIP_DISABLE_PIP_VERSION_CHECK=1

USER sandbox
WORKDIR /app

# OPTIONAL offline packages (see README, "Troubleshooting"): uncomment after adding ./wheels/*.whl
# COPY --chown=sandbox:sandbox wheels/ /wheels/
# RUN pip install --no-index --find-links /wheels /wheels/*.whl

COPY --chown=sandbox:sandbox core.py server.py ./

VOLUME /data
EXPOSE 8000
CMD ["python", "server.py"]
