"""Levanta el laboratorio local.

    .venv\\Scripts\\python scripts/lab.py

- En esta computadora: http://localhost:7861/lab (subir la INE y ver resultados).
- En el telefono: se escanea el QR, que apunta a https://<IP de la red>:7862.
  La camara del navegador exige HTTPS fuera de localhost: se usa un certificado
  propio (el telefono avisa "conexion no privada" la primera vez; se acepta).
  El telefono debe estar en la misma red WiFi que la computadora.

Misma prueba de vida que en el servidor, sin base de datos ni webhooks. Los
umbrales se igualan a los del servidor con las mismas variables
(FACE_MATCH_PERCENT, SAME_PERSON_PERCENT, ASR_MODEL_SIZE...).
"""
import asyncio
import ipaddress
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(RAIZ)
sys.path.insert(0, RAIZ)
os.environ["LAB"] = "true"
os.environ["DATABASE_URL"] = ""  # el lab no usa la base aunque haya una configurada

PUERTO = int(os.getenv("LAB_PORT", "7861"))
PUERTO_TELEFONO = int(os.getenv("LAB_PHONE_PORT", "7862"))
CARPETA_CERT = os.path.join(RAIZ, ".lab_cert")


def ip_local() -> str:
    """La IP de esta computadora en la red (la que alcanza el telefono).

    Con cable y WiFi a la vez, la elegida puede ser la del cable: se fija con
    `python scripts/lab.py 192.168.x.x` o con LAB_HOST.
    """
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.getenv("LAB_HOST"):
        return os.environ["LAB_HOST"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no manda nada: solo elige la interfaz de salida
        return s.getsockname()[0]
    finally:
        s.close()


def certificado(ip: str) -> tuple[str, str]:
    """Certificado propio para esa IP; se rehace si la IP cambio."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(CARPETA_CERT, exist_ok=True)
    crt, key = os.path.join(CARPETA_CERT, f"{ip}.crt"), os.path.join(CARPETA_CERT, f"{ip}.key")
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    llave = ec.generate_private_key(ec.SECP256R1())
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Laboratorio prueba de vida")])
    ahora = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre).issuer_name(nombre)
        .public_key(llave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - timedelta(days=1))
        .not_valid_after(ahora + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName("localhost"),
        ]), critical=False)
        .sign(llave, hashes.SHA256())
    )
    with open(key, "wb") as fh:
        fh.write(llave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    with open(crt, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key


async def main() -> None:
    import uvicorn

    ip = ip_local()
    os.environ["LAB_PHONE_URL"] = f"https://{ip}:{PUERTO_TELEFONO}"
    crt, key = certificado(ip)
    from app.main import app

    # Un solo proceso para que los dos puertos compartan las sesiones en memoria.
    # El lifespan (modelos, mantenimiento) corre una vez, en el de la computadora.
    computadora = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PUERTO))
    telefono = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PUERTO_TELEFONO, lifespan="off",
                                             ssl_certfile=crt, ssl_keyfile=key))
    print(f"Laboratorio: http://localhost:{PUERTO}/lab")
    print(f"Telefonos (por QR): https://{ip}:{PUERTO_TELEFONO}")
    await asyncio.gather(computadora.serve(), telefono.serve())


if __name__ == "__main__":
    asyncio.run(main())
