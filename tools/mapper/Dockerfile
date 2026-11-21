# Builds the environment used to MAKE the coverage map (run locally, not by the scorer).
# Same base image + packages the organizers labeled with, sqlglot at the pinned commit.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage==7.10.7
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir /src && tar xzf /tmp/sqlglot.tar.gz -C /src --strip-components=1 && rm /tmp/sqlglot.tar.gz
COPY tools/mapper/ /plugin/
RUN echo "import child_cov" > /usr/local/lib/python3.13/site-packages/zz_child_cov.pth
ENV PYTHONPATH=/src:/plugin SKIP_INTEGRATION=0 MAP_SRC=/src MAP_OUT=/out
WORKDIR /src
CMD ["python", "-m", "pytest", "-p", "covmap_plugin", "-q", "-p", "no:cacheprovider", "tests"]
