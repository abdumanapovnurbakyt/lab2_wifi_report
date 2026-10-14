FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      iproute2 iperf3 iputils-ping mtr-tiny tcpdump openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 MPLBACKEND=Agg
CMD ["sleep", "infinity"]
