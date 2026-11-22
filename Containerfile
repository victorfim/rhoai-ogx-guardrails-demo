FROM ubi9/python-311:latest

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Standard non-root user configuration
USER 1001

EXPOSE 8080

CMD ["python", "app.py"]
