FROM fedora:latest

RUN dnf -y install python3 python3-pip && \
    dnf clean all
RUN pip3 install kubernetes
COPY api.py /usr/local/
ENTRYPOINT ["python3", "/usr/local/api.py"]
