"""Variables de entorno de las pruebas. Van aqui porque app.core.config lee el
entorno una sola vez, al importarse, y pytest carga este archivo antes que las pruebas."""
import os

os.environ.setdefault("DATABASE_URL", os.getenv("TEST_DATABASE_URL", ""))
os.environ["ALLOW_HTTP_URLS"] = "true"
os.environ["DELIVERY_RETRIES"] = "0"
