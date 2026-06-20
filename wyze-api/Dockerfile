# Use specific version for reproducibility
FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install iproute2 for setting default route in macvlan mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Wyze's api.wyzecam.com DigiCert chain fails verification against Python's bundled
# certifi on some hosts (SSLCertVerificationError: unable to get local issuer cert),
# while the OS ca-certificates store verifies it fine. Point requests/ssl at the OS store.
# (upstream fix candidate)
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose the port
EXPOSE 8080

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
