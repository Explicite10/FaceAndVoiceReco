#!/usr/bin/env bash
# Revisa que el servicio publicado solo deje entrar a proyectos autorizados.
#
#   scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx
#   scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx pv_LLAVE
#   scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx pv_LLAVE ine.jpg
#
# Con llave revisa que el proyecto entre; con una foto de INE ademas crea una
# sesion de prueba (y la cancela, asi que su webhook recibe "sesion.cancelada").
set -u
URL="${1:?Uso: $0 https://dominio [api_key] [ine.jpg]}"
URL="${URL%/}"
KEY="${2:-}"
INE="${3:-}"
fallas=0

revisar() {  # nombre, codigo esperado, codigo obtenido
  if [ "$3" = "$2" ]; then echo "  OK    $1 ($3)"; else echo "  FALLA $1: esperaba $2, llego $3"; fallas=$((fallas + 1)); fi
}
codigo() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }

echo "Servicio: $URL"
revisar "El servicio responde (/health)"                 200 "$(codigo "$URL/health")"
revisar "Pagina de pruebas apagada (DEMO=false)"         404 "$(codigo "$URL/")"
revisar "Crear sesion sin llave se rechaza"              401 "$(codigo -X POST "$URL/api/v1/sesiones")"
revisar "Crear sesion con llave falsa se rechaza"        401 "$(codigo -X POST -H 'X-API-Key: pv_falsa_000000000000000000000000' "$URL/api/v1/sesiones")"
revisar "API directa sin llave se rechaza"               401 "$(codigo -X POST "$URL/api/verify-identity")"
revisar "Reto sin llave se rechaza"                      401 "$(codigo -X POST "$URL/api/challenge")"
revisar "Captura con enlace inventado no existe"         404 "$(codigo "$URL/api/v1/captura/enlace-inventado")"

case "$URL" in https://*) ;; *) echo "  FALLA la URL debe ser https:// (sin eso la camara no funciona)"; fallas=$((fallas + 1));; esac

if [ -n "$KEY" ]; then
  revisar "El proyecto entra con su llave"               200 "$(codigo -H "X-API-Key: $KEY" "$URL/api/v1/sesiones?limite=1")"
fi

if [ -n "$KEY" ] && [ -n "$INE" ]; then
  resp=$(curl -s --max-time 60 -X POST "$URL/api/v1/sesiones" -H "X-API-Key: $KEY" \
    -F "ine_imagen=@$INE" -F "frase=prueba de instalacion del servicio de prueba de vida" \
    -F "referencia_externa=verificacion-instalacion")
  id=$(printf '%s' "$resp" | sed -n 's/.*"id":"\(ses_[a-z0-9]*\)".*/\1/p')
  if [ -n "$id" ]; then
    echo "  OK    Sesion creada: $id"
    echo "        url_captura: $(printf '%s' "$resp" | sed -n 's/.*"url_captura":"\([^"]*\)".*/\1/p')"
    revisar "Cancelar la sesion de prueba"               200 "$(codigo -X POST -H "X-API-Key: $KEY" "$URL/api/v1/sesiones/$id/cancelar")"
  else
    echo "  FALLA No se creo la sesion: $resp"; fallas=$((fallas + 1))
  fi
fi

echo
if [ "$fallas" -eq 0 ]; then echo "Todo bien."; else echo "$fallas revision(es) fallaron."; exit 1; fi
