"""
Obtener items de boletas/facturas desde www.chilquinta.cl usando Selenium.

Flujo por cada par (rut, cliente):
  1. GET /profile/public/validate-notlogged/006/rut/{rut}/nis/{cliente}
       -> validInvoice.existVoucher (bool). Si es False se omite el cliente.
  2. GET /invoice/only/detail/006/nis/{cliente}?isLogged=false
       -> detailInvoice.currentDetail.invoiceNumber
  3. GET /invoice/only/items/006/typeDocument/39/invoice-number/{invoice}
       -> arreglo de items; cada item = una fila del CSV de salida.

Las requests se ejecutan con fetch() dentro del contexto de la pagina
(drive.execute_async_script), por lo que salen con cookies, user-agent y
origen reales del navegador (evita el 403 de una request directa).

Cada request lleva ademas las firmas que exige la API Gateway del sitio:
  x-api-key / x-system-key  (clave por servicio)
  X-Authorization-Token     = base64(HMAC-SHA512(mensaje, clave))
  X-Request-Epoch, X-Request-Host
con la clave derivada de MD5("TCSyW66963wwGNtm:{ua}:{epoch}") y DateToken =
base64(MD5(año+mesUTC)), tal como lo calcula el interceptor de la web.

Uso:
  python chilquinta_items.py                         # lee invoices_input.csv
  python chilquinta_items.py --rut 12345678-9 --cliente 1234567890
  python chilquinta_items.py --input otro.csv --output otro_out.csv
  python chilquinta_items.py --visible               # ventana visible
  python chilquinta_items.py --headless              # oculto pero validate fallara (WAF)

El WAF/Bot Control de chilquinta.cl bloquea /profile/public/* en Chrome
headless, por lo que el modo por defecto usa Chrome con ventana fuera de
pantalla (no molesta y pasa el WAF).
"""

import argparse
import base64
import csv
import hashlib
import hmac
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE = "https://www.chilquinta.cl"
HERE = Path(__file__).resolve().parent

# User-Agent realista: las versiones con "HeadlessChrome" son bloqueadas por el WAF
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

# Claves de API Gateway por servicio (sacadas del bundle de Nuxt de la web)
KEY_PROFILE = "bUsbh2FgJz7mCWXfhXFny9PK7S6TA5Ge5pYYaNHs"   # profile/public
KEY_INVOICE = "iRzXDJweeu1KdpnJaT1Du5IOLYxMR0jz5pgJhQCB"   # invoice/only/*
SYSTEM_KEY_INVOICE = "web"

LOG = logging.getLogger("chilquinta")

# Ejecuta fetch() dentro de la pagina y devuelve {status, body}.
# arguments: [0]=url, [1]=headers(dict), [2]=callback
FETCH_JS = """
const url = arguments[0];
const headers = arguments[1];
const done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', headers: headers})
  .then(async r => {
      const text = await r.text();
      let body = null;
      try { body = JSON.parse(text); } catch (e) { body = text.slice(0, 300); }
      done({status: r.status, body: body});
  })
  .catch(e => done({status: 0, error: String(e)}));
"""


def sign_request(url: str, api_key: str, method: str = "GET", body: str = ""):
    """Reproduce la firma HMAC que agrega el interceptor axios de chilquinta.cl.

    Devuelve los headers {X-Authorization-Token, X-Request-Epoch, X-Request-Host}.
    """
    p = urlparse(url)
    resource = re.sub(r"/$", "", p.path)
    params = urlencode(sorted(parse_qsl(p.query)))
    epoch = int(time.time() * 1000)
    ua_mod = re.sub(r"\s", "@+=?", UA).lower()
    signing_key = hashlib.md5(
        f"TCSyW66963wwGNtm:{ua_mod}:{epoch}".encode()).hexdigest()
    g = time.gmtime()
    date_token = base64.b64encode(
        hashlib.md5(str(g.tm_year + (g.tm_mon - 1)).encode()).digest()).decode()
    msg = (
        f"Method: {method}\n"
        f"X-API-Key: {api_key}\n"
        f"X-Host: {p.netloc}\n"
        f"RequestBody: {body}\n"
        f"X-Request-Epoch: {epoch}\n"
        f"Resource: {resource}\n"
        f"SearchParams: {params}\n"
        f"DateToken: {date_token}\n"
    )
    mac = hmac.new(signing_key.encode(), msg.encode(), hashlib.sha512).digest()
    return {
        "X-Authorization-Token": base64.b64encode(mac).decode(),
        "X-Request-Epoch": str(epoch),
        "X-Request-Host": p.netloc,
    }

MAX_RETRIES = 3


def make_driver(headless: bool = False, visible: bool = False) -> webdriver.Chrome:
    """Crea el driver.

    headless=True -> Chrome headless (el WAF bloquea /profile/public/*, validate fallara).
    Default      -> Chrome headful con la ventana fuera de pantalla (pasa el WAF sin molestar).
    visible=True -> Chrome headful con ventana visible.
    """
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
    elif not visible:
        # Ventana fuera de pantalla: se ve "headless" pero el WAF/Bot Control lo acepta
        opts.add_argument("--window-position=-32000,-32000")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--user-agent=" + UA)
    driver = webdriver.Chrome(options=opts)
    driver.set_script_timeout(60)
    return driver


def warmup(driver: webdriver.Chrome) -> None:
    """Navega al sitio para obtener cookies/sesion inicial (evita 403)."""
    LOG.info("Calentando navegador en %s", BASE)
    driver.get(BASE)
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(3)  # deja tiempo al SDK WAF (challenge) de inicializarse


def fetch_json(driver: webdriver.Chrome, path: str, api_key: str,
               extra_headers: dict | None = None):
    """GET firmado dentro de la pagina. Devuelve (status, body)."""
    url = path if path.startswith("http") else BASE + path
    last = (0, None)
    for attempt in range(1, MAX_RETRIES + 1):
        headers = {
            "Accept": "application/json",
            "x-api-key": api_key,
            **(extra_headers or {}),
        }
        headers.update(sign_request(url, api_key))
        try:
            res = driver.execute_async_script(FETCH_JS, url, headers)
        except Exception as exc:  # timeout u otro error del script
            LOG.warning("Intento %d fallo (script): %s", attempt, exc)
            res = {"status": 0, "error": str(exc)}

        status = res.get("status", 0)
        body = res.get("body")
        # El WAF de CloudFront responde con HTML 403; tratar como bloqueo temporal
        waf_blocked = (isinstance(body, str) and body.lstrip().startswith("<!DOCTYPE"))
        if status in (200, 201):
            return status, body

        LOG.warning("Intento %d para %s -> HTTP %s%s",
                    attempt, url, status,
                    " (bloqueo WAF)" if waf_blocked else "")
        last = (status, body)
        if status in (403, 429, 0, 500, 502, 503, 504) or waf_blocked:
            if attempt < MAX_RETRIES:
                try:
                    warmup(driver)  # recarga la pagina y repone cookies/token WAF
                except Exception as exc:
                    LOG.warning("Re-warmup fallo: %s", exc)
                if status in (429, 504, 0):
                    # throttling de API Gateway: enfriar mas
                    time.sleep(10 + random.random() * 10)
                else:
                    time.sleep(3 + random.random() * 3)
    return last


def find_items(detail: dict):
    """Localiza el arreglo de items dentro de detailInvoice."""
    di = (detail or {}).get("detailInvoice") or {}
    if not isinstance(di, dict):
        return None
    for key in ("items", "item"):
        val = di.get(key)
        if isinstance(val, list):
            return val
    for val in di.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return None


def process_pair(driver, rut: str, cliente: str, rows: list) -> str:
    """Procesa un par (rut, cliente). Devuelve 'ok' | 'no_voucher' | 'failed'."""
    rut_q = rut.strip()
    cliente_q = cliente.strip()

    # 1) validacion existVoucher
    st, body = fetch_json(
        driver,
        f"/profile/public/validate-notlogged/006/rut/{rut_q}/nis/{cliente_q}",
        api_key=KEY_PROFILE,
    )
    if st != 200 or not isinstance(body, dict):
        LOG.error("rut=%s cliente=%s: validacion HTTP %s -> omitido", rut_q, cliente_q, st)
        return "failed"
    exist = None
    try:
        exist = body["validInvoice"]["existVoucher"]
    except (KeyError, TypeError):
        LOG.error("rut=%s cliente=%s: respuesta sin validInvoice.existVoucher: %s",
                  rut_q, cliente_q, json.dumps(body, ensure_ascii=False)[:300])
        return "failed"
    if exist is not True:
        LOG.info("rut=%s cliente=%s: existVoucher=%s -> sin boleta, omitido",
                 rut_q, cliente_q, exist)
        return "no_voucher"

    # 2) detalle -> invoiceNumber
    st, body = fetch_json(
        driver,
        f"/invoice/only/detail/006/nis/{cliente_q}?isLogged=false",
        api_key=KEY_INVOICE,
        extra_headers={"x-system-key": SYSTEM_KEY_INVOICE},
    )
    if st != 200 or not isinstance(body, dict):
        LOG.error("rut=%s cliente=%s: detalle HTTP %s -> omitido", rut_q, cliente_q, st)
        return "failed"
    try:
        invoice = body["detailInvoice"]["currentDetail"]["invoiceNumber"]
    except (KeyError, TypeError):
        LOG.error("rut=%s cliente=%s: sin invoiceNumber: %s",
                  rut_q, cliente_q, json.dumps(body, ensure_ascii=False)[:300])
        return "failed"
    LOG.info("rut=%s cliente=%s -> invoice=%s", rut_q, cliente_q, invoice)

    # 3) items de la boleta
    st, body = fetch_json(
        driver,
        f"/invoice/only/items/006/typeDocument/39/invoice-number/{invoice}",
        api_key=KEY_INVOICE,
    )
    if st != 200 or not isinstance(body, dict):
        LOG.error("invoice=%s: items HTTP %s -> omitido", invoice, st)
        return "failed"
    items = find_items(body)
    if not items:
        LOG.error("invoice=%s: sin items en la respuesta: %s",
                  invoice, json.dumps(body, ensure_ascii=False)[:300])
        return "failed"

    for item in items:
        if not isinstance(item, dict):
            item = {"value": item}
        row = {"rut": rut_q, "cliente": cliente_q, "invoice_number": invoice}
        row.update(item)
        rows.append(row)
    LOG.info("invoice=%s: %d item(s) agregados", invoice, len(items))
    return "ok"


def write_csv(rows: list, out_path: Path, delimiter: str) -> None:
    base_cols = ["rut", "cliente", "invoice_number"]
    extra_cols = []
    for row in rows:
        for key in row:
            if key not in base_cols and key not in extra_cols:
                extra_cols.append(key)
    fieldnames = base_cols + extra_cols
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_input(path: Path) -> list:
    pairs = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for rec in reader:
            rut = (rec.get("rut") or "").strip()
            cliente = (rec.get("cliente") or "").strip()
            if rut and cliente:
                pairs.append((rut, cliente))
            elif any((v or "").strip() for v in rec.values()):
                LOG.warning("Fila de entrada incompleta ignorada: %s", rec)
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="Extrae items de boletas chilquinta.cl a CSV")
    ap.add_argument("--input", default=str(HERE / "invoices_input.csv"),
                    help="CSV de entrada con columnas rut,cliente")
    ap.add_argument("--output", default=str(HERE / "invoices_output.csv"),
                    help="CSV de salida")
    ap.add_argument("--rut", help="Procesar un solo rut (junto a --cliente)")
    ap.add_argument("--cliente", help="Procesar un solo cliente (junto a --rut)")
    ap.add_argument("--delimiter", default=";", help="Delimitador del CSV de salida (default: ;)")
    ap.add_argument("--pause-min", type=float, default=1.0,
                    help="Pausa minima entre pares en segundos (default: 1.0)")
    ap.add_argument("--pause-max", type=float, default=2.5,
                    help="Pausa maxima entre pares en segundos (default: 2.5)")
    ap.add_argument("--headless", action="store_true",
                    help="Chrome headless (el WAF bloquea validate: casi todo fallara)")
    ap.add_argument("--visible", action="store_true",
                    help="Chrome headful con ventana visible (por defecto va fuera de pantalla)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(HERE / "bill_items.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    if bool(args.rut) != bool(args.cliente):
        ap.error("--rut y --cliente deben indicarse juntos")
    if args.rut:
        pairs = [(args.rut.strip(), args.cliente.strip())]
    else:
        in_path = Path(args.input)
        if not in_path.exists():
            LOG.error("No existe el archivo de entrada: %s", in_path)
            return 1
        pairs = read_input(in_path)
        if not pairs:
            LOG.error("El archivo %s no contiene pares rut,cliente validos", in_path)
            return 1

    LOG.info("Pares a procesar: %d", len(pairs))

    driver = None
    rows = []
    try:
        driver = make_driver(headless=args.headless, visible=args.visible)
        if args.headless:
            LOG.warning("Modo headless activado: el WAF de chilquinta.cl bloquea "
                        "/profile/public/* en Chrome headless, por lo que la "
                        "validacion de cada par fallara. Usar sin --headless.")
        warmup(driver)

        counts = {"ok": 0, "no_voucher": 0, "failed": 0}
        for rut, cliente in pairs:
            try:
                counts[process_pair(driver, rut, cliente, rows)] += 1
            except Exception:
                LOG.exception("Error procesando rut=%s cliente=%s", rut, cliente)
                counts["failed"] += 1
            time.sleep(args.pause_min + random.random() *
                       max(0.0, (args.pause_max - args.pause_min)))  # pausa entre pares
    finally:
        if driver is not None:
            driver.quit()

    LOG.info("Resumen: %d procesado(s), %d sin boleta, %d fallido(s)",
             counts["ok"], counts["no_voucher"], counts["failed"])

    out_path = Path(args.output)
    write_csv(rows, out_path, args.delimiter)
    LOG.info("Listo: %d fila(s) escritas en %s", len(rows), out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
