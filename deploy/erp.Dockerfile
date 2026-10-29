# RT-927 (design: RT-926 v2, Confluence 60391425): the ERP lab image, a thin layer over the
# digest-pinned ERPNext base. Built by .github/workflows/build-erp-image.yml; the build context
# is a directory that holds the Connector tree (git archive of one main commit) at CONNECTOR_SRC.
#
# The base is v16.51.0 (Frappe 16.52.0), the same digest lab-03 runs today and RT-495 validated.
# Never a tag: build-erp-image.yml compares this digest with its own BASE_DIGEST constant.
#
# The recipe was recovered from `docker history` on lab-03. The original lives only on the host
# (frappe_docker custom image), so this file is its first copy in a repository.
FROM frappe/erpnext@sha256:f2d409c58ed1b688b6f2914d4a7f8b2468ee60bd1a3f1bd82d6fe32071806cb1

ARG CONNECTOR_SRC=connector
ARG CONNECTOR_SHA
ARG CONNECTOR_TREE
ARG CONNECTOR_VERSION

LABEL org.opencontainers.image.revision="${CONNECTOR_SHA}" \
      rt.connector.tree="${CONNECTOR_TREE}" \
      rt.connector.version="${CONNECTOR_VERSION}" \
      rt.base.digest="sha256:f2d409c58ed1b688b6f2914d4a7f8b2468ee60bd1a3f1bd82d6fe32071806cb1"

USER frappe
WORKDIR /home/frappe/frappe-bench

COPY --chown=1000:0 ${CONNECTOR_SRC} apps/retail_tower_erpnext_connector

# pip is in the bench env on this base; uv is the fallback if a later base drops it.
RUN set -eu; \
    if [ -x ./env/bin/pip ]; then \
      ./env/bin/pip install --no-cache-dir -e apps/retail_tower_erpnext_connector; \
    else \
      uv pip install --python ./env/bin/python -e apps/retail_tower_erpnext_connector; \
    fi; \
    ./env/bin/python -c "import retail_tower_erpnext_connector as m; print(m.__version__)"
