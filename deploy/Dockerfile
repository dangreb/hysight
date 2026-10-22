# Pass the existing deployment's pinned Hindsight image as HINDSIGHT_IMAGE.
ARG HINDSIGHT_IMAGE
FROM ${HINDSIGHT_IMAGE}
USER root
COPY dist/hysight-0.1.0-py3-none-any.whl /tmp/hysight-0.1.0-py3-none-any.whl
RUN uv pip install --offline --python /app/api/.venv/bin/python --no-deps /tmp/hysight-0.1.0-py3-none-any.whl \
    && rm /tmp/hysight-0.1.0-py3-none-any.whl \
    && /app/api/.venv/bin/python -c "from hysight.extension import PromotionExtension, PromotionLifecycle"

