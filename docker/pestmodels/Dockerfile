# PestWatch model worker.
#
# Built on the project's GeoNode image so the worker shares GeoNode's settings and
# can publish layers. The model libraries live in a separate virtualenv
# (/opt/pestmodels) because the trained models need numpy 2 and newer
# scikit-learn / xgboost than GeoNode's own environment: GeoNode's packages are
# never changed.
#
# "geonode-base" is a named build context that docker-compose.yml points at the
# django service (additional_contexts: geonode-base: service:django). To build this
# file by hand, supply it yourself, e.g.:
#   docker build -f docker/pestmodels/Dockerfile \
#     --build-context geonode-base=docker-image://geonode_project/geonode:master .
FROM geonode-base

COPY src/pestwatch/pipeline/requirements.txt /tmp/pestmodels-requirements.txt
RUN /usr/src/venv/bin/python -m venv /opt/pestmodels \
    && /opt/pestmodels/bin/pip install --no-cache-dir -U pip setuptools wheel \
    && /opt/pestmodels/bin/pip install --no-cache-dir -r /tmp/pestmodels-requirements.txt \
    && /opt/pestmodels/bin/python -c "import numpy, sklearn, xgboost, pygam, torch, rasterio, ee, cdsapi; print('model environment OK', numpy.__version__, sklearn.__version__, torch.__version__)"

ENV PESTWATCH_MODELS_PYTHON=/opt/pestmodels/bin/python \
    MPLBACKEND=Agg
