#!/usr/bin/env python3
# Northpharm - servidor local para comparar factura vs SIFACO con Gemini API.
# Escucha solo en localhost: la clave no se entrega al navegador ni se publica en internet.

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, base64, urllib.request, urllib.error, mimetypes

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
HOST, PORT = "0.0.0.0", 8765
MODEL = "gemini-3.1-flash-lite"
MAX_BODY = 28 * 1024 * 1024

def read_key():
    if not CONFIG.exists():
        return ""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("api_key", "")
    except Exception:
        return ""

class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/status":
            self.send_json(200, {"configured": bool(read_key())})
            return
        if self.path in ("/", "/index.html"):
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path not in ("/api/key", "/api/compare"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self.send_json(413, {"error": "El envío está vacío o es demasiado grande. Probá con menos fotos o imágenes más livianas."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json(400, {"error": "No se pudo leer la solicitud."})
            return

        if self.path == "/api/key":
            key = str(payload.get("api_key", "")).strip()
            if len(key) < 15:
                self.send_json(400, {"error": "La clave parece incompleta."})
                return
            CONFIG.write_text(json.dumps({"api_key": key}, ensure_ascii=False), encoding="utf-8")
            try:
                CONFIG.chmod(0o600)
            except Exception:
                pass
            self.send_json(200, {"ok": True})
            return

        key = read_key()
        if not key:
            self.send_json(400, {"error": "Primero configurá tu clave de Google AI Studio."})
            return
        images = payload.get("images", [])
        if not images or len(images) > 10:
            self.send_json(400, {"error": "Cargá entre 1 y 10 imágenes en total."})
            return

        prompt = """Sos un auditor cuidadoso de recepción de mercadería de una farmacia argentina.
Voy a enviarte imágenes en este orden: primero todas las fotos de la FACTURA DEL PROVEEDOR; después todas las capturas del INGRESO EN SIFACO. El usuario eligió los archivos en dos grupos, y el programa conserva ese orden.

Compará exclusivamente lo que se ve. Identificá proveedor y número de factura si son legibles. Relacioná productos aunque los nombres estén abreviados o escritos distinto SOLO si coinciden razonablemente droga/ingrediente, concentración, forma farmacéutica, presentación, unidades por envase y laboratorio/marca cuando figure. No unas productos parecidos si hay una diferencia relevante.

Compará CANTIDADES DE ENVASES/UNIDADES ingresadas, no precios. Sumá bonificaciones/unidades sin cargo a las cantidades facturadas cuando estén claramente indicadas y explicalo en la nota. Si un producto aparece solo en factura, informá unidades_sifaco=0 y resultado FALTA EN SIFACO. Si aparece solo en SIFACO, informá unidades_factura=0 y resultado SOBRA EN SIFACO. Si no podés leer un dato con certeza, usá null y agregá una duda; nunca inventes ni estimes.

Devolvé SOLO JSON válido con esta estructura:
{
 "nro_factura": string|null,
 "proveedor_factura": string|null,
 "proveedor_sifaco": string|null,
 "total_factura": number|null,
 "total_sifaco": number|null,
 "estado_general": "COINCIDE"|"HAY DIFERENCIAS"|"REVISAR",
 "items": [
  {"producto": string, "detalle_factura": string|null, "detalle_sifaco": string|null,
   "unidades_factura": number|null, "unidades_sifaco": number|null,
   "resultado": "OK"|"FALTA EN SIFACO"|"SOBRA EN SIFACO"|"CANTIDAD DISTINTA"|"REVISAR",
   "nota": string}
 ],
 "dudas": [string]
}
No agregues productos que no puedas respaldar visualmente. Si una imagen está borrosa, explicalo. No confundas renglones de precios, descuentos o importes con cantidades.
"""

        parts = [{"text": prompt}]
        for item in images:
            mime = str(item.get("mime_type", "image/jpeg"))
            data = str(item.get("data", ""))
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                self.send_json(400, {"error": "Formato de imagen no admitido."})
                return
            try:
                decoded = base64.b64decode(data, validate=True)
            except Exception:
                self.send_json(400, {"error": "Una imagen llegó en un formato inválido."})
                return
            if len(decoded) > 8 * 1024 * 1024:
                self.send_json(413, {"error": "Una imagen pesa demasiado. Probá con una foto más liviana."})
                return
            parts.append({"inline_data": {"mime_type": mime, "data": data}})

        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json"
            }
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                api_data = json.loads(response.read().decode("utf-8"))
            text = api_data["candidates"][0]["content"]["parts"][0]["text"]
            # Remove accidental markdown fences if the model adds them.
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(text)
            self.send_json(200, {"result": result})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except Exception:
                pass
            self.send_json(502, {"error": f"Google no pudo procesar la comparación: {detail}"})
        except Exception as e:
            self.send_json(502, {"error": f"No se pudo completar la comparación. {str(e)}"})

    def log_message(self, fmt, *args):
        print("[Northpharm]", fmt % args)

if __name__ == "__main__":
    print(f"\nNorthpharm - Control de facturas\nAbrí en tu navegador: http://{HOST}:{PORT}\nPara cerrar, volvé a esta ventana y presioná Ctrl+C.\n")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
