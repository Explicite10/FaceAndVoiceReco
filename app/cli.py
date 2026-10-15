"""Administra los proyectos autorizados.

En el servidor:
    docker compose exec api python -m app.cli crear "Creditos" \\
        --webhook https://api.creditos.com/webhooks/vida --dominios creditos.com
    docker compose exec api python -m app.cli listar
    docker compose exec api python -m app.cli rotar-key prj_...
    docker compose exec api python -m app.cli desactivar prj_...
"""
import argparse
import asyncio
import json
import sys

from app import db
from app.services import proyectos


def _dominios(texto: str | None) -> list[str]:
    return [d for d in (texto or "").split(",") if d.strip()]


async def _main(args: argparse.Namespace) -> None:
    await db.connect()
    if not db.enabled():
        sys.exit("Falta DATABASE_URL.")
    try:
        if args.cmd == "crear":
            p = await proyectos.crear(args.nombre, args.webhook, _dominios(args.dominios))
            print(json.dumps(p, indent=2, ensure_ascii=False))
            print("\nGuarda api_key y webhook_secreto en el backend del proyecto: la api_key no se vuelve a mostrar.")
        elif args.cmd == "listar":
            for p in await proyectos.listar():
                estado = "activo" if p["activo"] else "DESACTIVADO"
                dominios = ",".join(p["dominios_permitidos"]) or "(cualquiera)"
                print(f"{p['id']}  {estado:11}  {p['nombre']}  webhook={p['webhook_url']}  dominios={dominios}")
        elif args.cmd == "rotar-key":
            print(await proyectos.rotar_api_key(args.id))
            print("\nLa llave anterior deja de funcionar ya.")
        elif args.cmd == "desactivar":
            await proyectos.desactivar(args.id)
            print("Desactivado.")
        elif args.cmd == "activar":
            await proyectos.activar(args.id)
            print("Activado.")
        elif args.cmd == "webhook":
            await proyectos.actualizar_webhook(args.id, args.url or None)
            print("Webhook actualizado.")
        elif args.cmd == "dominios":
            await proyectos.actualizar_dominios(args.id, _dominios(args.dominios))
            print("Dominios actualizados.")
    except KeyError as exc:
        sys.exit(str(exc.args[0]))
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Proyectos autorizados")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crear", help="Dar de alta un proyecto")
    c.add_argument("nombre")
    c.add_argument("--webhook", help="URL donde recibe los resultados (https)")
    c.add_argument("--dominios", help="Dominios permitidos separados por coma, ej. creditos.com,app.creditos.com")
    sub.add_parser("listar", help="Ver los proyectos")
    for nombre, ayuda in (("rotar-key", "Generar una API key nueva"), ("desactivar", "Quitar el acceso"),
                          ("activar", "Devolver el acceso")):
        sub.add_parser(nombre, help=ayuda).add_argument("id")
    w = sub.add_parser("webhook", help="Cambiar la URL del webhook")
    w.add_argument("id")
    w.add_argument("url", nargs="?", default="")
    d = sub.add_parser("dominios", help="Cambiar los dominios permitidos")
    d.add_argument("id")
    d.add_argument("dominios")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
