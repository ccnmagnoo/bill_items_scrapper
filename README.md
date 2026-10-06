# bill_items.py

Extrae los **items de boletas/facturas** de [chilquinta.cl](https://www.chilquinta.cl) a partir de una lista de pares **RUT + número de cliente (NIS)**, y los exporta a un CSV.

## Remitir requisitos

- Python 3.10+
- [`selenium`](https://pypi.org/project/selenium/) 4.x instalado
- Google Chrome instalado (el `chromedriver` se resuelve automáticamente vía Selenium Manager)

```bash
pip install selenium
```

## Cómo funciona

Por cada par `(rut, cliente)` el script ejecuta 3 requests firmadas contra la API del sitio:

1. **Validación** — `GET /profile/public/validate-notlogged/006/rut/{rut}/nis/{cliente}`
   - Lee `validInvoice.existVoucher`. Si es `false`, el cliente **se omite** (no tiene boleta).
2. **Detalle** — `GET /invoice/only/detail/006/nis/{cliente}?isLogged=false`
   - Obtiene `detailInvoice.currentDetail.invoiceNumber` (número de la boleta actual).
3. **Items** — `GET /invoice/only/items/006/typeDocument/39/invoice-number/{invoice}`
   - Cada elemento del arreglo de items genera **una fila** en el CSV de salida.

Las requests se ejecutan con `fetch()` **dentro del navegador** (mismas cookies, user-agent y origen que el sitio) y llevan la firma HMAC que exige la API Gateway:

- `x-api-key` / `x-system-key` (claves por servicio, extraídas del bundle de la web)
- `X-Authorization-Token` = `base64(HMAC-SHA512(mensaje, clave))`
- `X-Request-Epoch`, `X-Request-Host`
- Clave derivada: `MD5("TCSyW66963wwGNtm:{userAgent}:{epoch}")` y `DateToken` = `base64(MD5(año+mesUTC))`

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `bill_items.py` | Script principal |
| `invoices_input.csv` | **Entrada**: pares `rut,cliente` (una fila por par) |
| `invoices_output.csv` | **Salida**: items de boletas (una fila por item) |
| `bill_items.log` | Bitácora de la ejecución |

## Entrada (`invoices_input.csv`)

Formato (el archivo ya viene con el encabezado):

```csv
rut,cliente
12345678-9,1234567890
98765432-1,555666777
```

- `rut`: con guión y dígito verificador (ej. `12345678-9`)
- `cliente`: número NIS / de cliente, solo dígitos

## Uso

```bash
# Lote completo desde invoices_input.csv (modo por defecto: Chrome con ventana fuera de pantalla)
python bill_items.py

# Chrome con ventana visible
python bill_items.py --visible

# Un solo par
python bill_items.py --rut 12345678-9 --cliente 1234567890

# Otros archivos de entrada/salida y delimitador
python bill_items.py --input otro.csv --output otro_out.csv --delimiter ","
```

### Opciones

| Opción | Descripción | Default |
|--------|-------------|---------|
| `--input PATH` | CSV de entrada | `invoices_input.csv` |
| `--output PATH` | CSV de salida | `invoices_output.csv` |
| `--rut RUT` | Procesar un solo rut (junto a `--cliente`) | — |
| `--cliente NIS` | Procesar un solo cliente (junto a `--rut`) | — |
| `--delimiter DELIM` | Delimitador del CSV de salida | `;` |
| `--pause-min S` | Pausa mínima entre pares | `1.0` |
| `--pause-max S` | Pausa máxima entre pares | `2.5` |
| `--visible` | Chrome con ventana visible | — |
| `--headless` | Chrome headless (**validate fallará**, ver abajo) | — |

## Salida (`invoices_output.csv`)

- Codificación `utf-8-sig` (compatible con Excel)
- Delimitador `;` (configurable con `--delimiter`)
- Columnas fijas: `rut`, `cliente`, `invoice_number`
- Columnas dinámicas: las propiedades de cada item (según devuelva la API)
- Una fila por item; los campos que no aplican quedan vacíos

Al final muestra un resumen en consola:

```
Resumen: N procesado(s), M sin boleta, K fallido(s)
```

## Notas importantes

### El WAF bloquea Chrome headless
El Bot Control de chilquinta.cl **bloquea `/profile/public/*` en Chrome headless** (devuelve `403` HTML). Por eso el modo por defecto usa Chrome *headful* con la **ventana fuera de pantalla** (no se ve pero supera el WAF), y existe `--visible` para ejecutar con ventana visible.

> Evita `--headless`: la validación de cada par fallará y no se generarán filas útiles.

### Throttling de API Gateway (HTTP 504 / 429)
Si se hacen demasiadas requests seguidas, la API puede responder `504`/`429` transitoriamente. El script reintenta con re-calentamiento del navegador y **backoff de 10–20 s** para esos códigos. Para lotes grandes, conviene pausas más largas:

```bash
python bill_items.py --pause-min 3 --pause-max 6
```

### Fallos por par
Un fallo en la validación, el detalle o los items de un par **no aborta el lote**: se registra en el log y se continúa con el siguiente par.