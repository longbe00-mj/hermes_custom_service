FROM nousresearch/hermes-agent@sha256:36099926da316742423544e055d4734777105ffe6341873af5459b957d0c2919
USER root
COPY runtime /service/runtime
RUN mkdir -p /service && chmod -R a+rX /service
USER hermes
WORKDIR /opt/data
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/service/runtime/boot.py"]
