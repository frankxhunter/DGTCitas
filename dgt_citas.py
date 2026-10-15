#!/usr/bin/env python3
"""
Comprueba si la cita previa de la DGT (área Conductores) tiene hueco en una
jefatura/oficina. Reproduce el mismo recorrido que haría un navegador:

  1. GET  inicio.faces                     -> formulario de centro
  2. POST AJAX (cambio de centro)          -> la web pinta el selector de área
  3. POST inicio.faces (centro + área CND) -> catálogo de trámites
  4. POST catalogo.faces ("Pedir cita")    -> aviso "sin capacidad" o calendario

Requisitos:  pip install requests beautifulsoup4

Uso:
  python dgt_citas.py --listar                       # lista los centros y sus códigos
  python dgt_citas.py --centro-id 546                # una comprobación (Sabadell)
  python dgt_citas.py --centro-id 546,545,530        # varias oficinas en una ronda 
  python dgt_citas.py --centro Sabadell,Barcelona --vigilar --cada 10
  python dgt_citas.py --probar-aviso                 # envía un aviso de prueba

Avisos (variables de entorno; si no pones ninguna, solo se escribe en pantalla):
  NTFY_TOPIC   tema de ntfy.sh -> notificación push al móvil (app ntfy)
  SMTP_HOST, SMTP_PORT (587 por defecto), SMTP_USER, SMTP_PASS, MAIL_TO -> correo

Para ejecutarlo programado en un servidor, lanza UNA ronda sin --vigilar
(cron / GitHub Actions) y avisará si hay posible disponibilidad.
Úsalo con moderación (no consultes más de una vez cada 10 minutos).
"""
import argparse
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

BASE = "https://sedeclave.dgt.gob.es/WEB_CITE_CONSULTA/paginas/"
URL_INICIO = BASE + "inicio.faces"
URL_CATALOGO = BASE + "catalogo.faces"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Nombres de campo vistos en tu HAR (se usan solo si no se detectan en el HTML)
CAMPO_CENTRO_POR_DEFECTO = "formselectorCentro:j_id_2i"
CAMPO_AREA = "formselectorCentro:idAreaSelector"
AREA_CONDUCTORES = "CND"
BOTON_POR_DEFECTO = "formselectorCentro:j_id_2t"
DEPURAR = False


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def campos_formulario(form):
    """Devuelve todos los campos del formulario (incluido javax.faces.ViewState)."""
    datos = {}
    for inp in form.find_all("input"):
        n = inp.get("name")
        if not n:
            continue
        t = (inp.get("type") or "text").lower()
        if t in ("submit", "button", "image", "file"):
            continue
        if t in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        datos[n] = inp.get("value", "")
    for sel in form.find_all("select"):
        n = sel.get("name")
        if not n:
            continue
        opt = sel.find("option", attrs={"selected": True}) or sel.find("option")
        datos[n] = opt.get("value", "") if opt else ""
    return datos


def selects_de_centros(form):
    """Selects del formulario que no son el de área: [(nombre_campo, [(valor, texto)])]"""
    res = []
    for sel in form.find_all("select"):
        n = sel.get("name", "")
        if n.endswith("idAreaSelector"):
            continue
        opciones = [(o.get("value"), o.get_text(" ", strip=True))
                    for o in sel.find_all("option") if o.get("value")]
        res.append((n, opciones))
    return res


def mensajes_de_aviso(html):
    """
    Devuelve los textos de los cuadros de diálogo que la página abre al cargar
    (los PF('x').show() del final). Así se distingue un aviso real de las
    plantillas de diálogo que van ocultas en el HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    mostrados = set(re.findall(r"PF\('([^']+)'\)\.show\(\)", html))
    mensajes = []
    for nombre in mostrados:
        m = re.search(r'PrimeFaces\.cw\("ConfirmDialog","%s",\{id:"([^"]+)"' % re.escape(nombre), html)
        if not m:
            mensajes.append("(diálogo '%s' sin texto localizado)" % nombre)
            continue
        div = soup.find(id=m.group(1))
        msg = div.select_one(".ui-confirm-dialog-message") if div else None
        mensajes.append(msg.get_text(" ", strip=True) if msg else "(diálogo '%s')" % nombre)
    return mensajes


FORM_ID = "formselectorCentro"


def parse_partial(xml):
    """Interpreta una respuesta JSF AJAX (<partial-response>): devuelve {id: html}."""
    ups = {}
    for m in re.finditer(r'<update id="([^"]+)">\s*<!\[CDATA\[(.*?)\]\]>\s*</update>', xml, re.S):
        ups[m.group(1)] = m.group(2)
    return ups


def control_area(cont):
    """Busca el control del área (select/input cuyo nombre contiene 'Area')."""
    for el in cont.find_all(["select", "input"]):
        if "area" in (el.get("name") or "").lower() and (el.get("type") or "").lower() != "hidden":
            return el
    for el in cont.find_all(["select", "input"]):
        if "area" in (el.get("name") or "").lower():
            return el
    return None


def boton_envio(cont):
    for el in cont.find_all(["input", "button"]):
        tipo = (el.get("type") or ("submit" if el.name == "button" else "text")).lower()
        if tipo == "submit" and el.get("name"):
            return el["name"], el.get("value", "")
    return None


class Dgt:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8"),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        })
        # La web no declara charset y requests supone ISO-8859-1: forzamos UTF-8
        self.s.hooks["response"] = [lambda r, *a, **k: setattr(r, "encoding", "utf-8")]

    def _post(self, url, datos, referer, ajax=False):
        cab = {"Referer": referer, "Origin": "https://sedeclave.dgt.gob.es",
               "Cache-Control": "no-cache", "Pragma": "no-cache",
               "Sec-Fetch-Site": "same-origin"}
        if ajax:
            cab.update({"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/xml, text/xml, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"})
        else:
            cab.update({"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-User": "?1"})
        return self.s.post(url, data=datos, timeout=30, headers=cab)

    def _ajax_cambio(self, cont, campo, valor, viewstate=None):
        """Simula jsf.ajax.request(campo, 'change', execute=@this, render=@form)."""
        datos = campos_formulario(cont)
        if viewstate:
            datos["javax.faces.ViewState"] = viewstate
        datos[campo] = valor
        datos["formselectorCentro_SUBMIT"] = "1"
        datos.update({
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": campo,
            "javax.faces.partial.execute": campo,
            "javax.faces.partial.render": FORM_ID,
            "javax.faces.behavior.event": "change",
            "javax.faces.partial.event": "change",
        })
        r = self._post(URL_INICIO, datos, URL_INICIO, ajax=True)
        r.raise_for_status()
        ups = parse_partial(r.text)
        nuevo_vs = next((v.strip() for k, v in ups.items() if "viewstate" in k.lower()), None)
        frag = "".join(v for k, v in ups.items() if "viewstate" not in k.lower())
        if DEPURAR:
            print("    [depurar] AJAX %s=%s -> HTTP %s, updates: %s, ViewState nuevo: %s"
                  % (campo.split(":")[-1], valor, r.status_code, list(ups) or "NINGUNO",
                     "sí" if nuevo_vs else "no"))
            if not ups:
                print("    [depurar] respuesta AJAX (300 car.): %r" % r.text[:300])
        return BeautifulSoup(frag, "html.parser"), nuevo_vs, r

    def formulario_inicio(self):
        r = self.s.get(URL_INICIO, timeout=30, headers={
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"})
        r.raise_for_status()
        if DEPURAR:
            print("    [depurar] GET inicio: HTTP %s, %d bytes, cookies recibidas: %s"
                  % (r.status_code, len(r.content), sorted(self.s.cookies.get_dict()) or "NINGUNA"))
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form", id="formselectorCentro")
        if form is None:
            raise RuntimeError("No encuentro el formulario de selección de centro en inicio.faces "
                               "(la DGT puede haber cambiado la página).")
        return form

    def listar_centros(self):
        form = self.formulario_inicio()
        return selects_de_centros(form)

    def comprobar(self, centro_id, campo_centro=None):
        form = self.formulario_inicio()
        if campo_centro is None:
            sels = selects_de_centros(form)
            campo_centro = sels[0][0] if sels else CAMPO_CENTRO_POR_DEFECTO
        viewstate = campos_formulario(form).get("javax.faces.ViewState")

        # 2) Cambio de centro (AJAX): la web re-pinta el formulario con el selector de área
        cont, vs, _ = self._ajax_cambio(form, campo_centro, centro_id, viewstate)
        viewstate = vs or viewstate
        ctrl = control_area(cont)
        if ctrl is not None and "jsf.ajax.request" in (ctrl.get("onchange") or ""):
            # El área también dispara AJAX al elegirla: lo reproducimos
            cont2, vs2, _ = self._ajax_cambio(cont, ctrl["name"], AREA_CONDUCTORES, viewstate)
            viewstate = vs2 or viewstate
            if cont2.find(["input", "select", "button"]):
                cont = cont2

        if DEPURAR:
            vista = re.sub(r"<option[^>]*>.*?</option>", "", str(cont), flags=re.S)
            vista = re.sub(r"\s+", " ", vista)
            print("    [depurar] formulario tras el AJAX (sin opciones): %s" % vista[:1500])

        datos = campos_formulario(cont)
        datos["javax.faces.ViewState"] = viewstate or ""
        datos[campo_centro] = centro_id
        ctrl = control_area(cont)
        datos[ctrl["name"] if ctrl is not None else CAMPO_AREA] = AREA_CONDUCTORES
        datos["formselectorCentro_SUBMIT"] = "1"
        boton = boton_envio(cont) or (BOTON_POR_DEFECTO, "")
        datos[boton[0]] = boton[1]

        if DEPURAR:
            print("    [depurar] campos enviados en el paso final:")
            for k, v in datos.items():
                print("      %s = %s" % (k, (v[:40] + "...") if len(v) > 40 else v))

        # 3) Envío final del formulario -> catálogo
        r = self._post(URL_INICIO, datos, URL_INICIO)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form_t = soup.find("form", id="tramites")
        if form_t is None:
            detalle = ["No llegué al catálogo de trámites (HTTP %s, URL final: %s, redirecciones: %d)"
                       % (r.status_code, r.url, len(r.history))]
            detalle += ["Aviso de la web: " + m for m in mensajes_de_aviso(r.text)]
            for el in soup.select(".ui-messages, .ui-message-error, .ui-messages-error, .alert"):
                txt = el.get_text(" ", strip=True)
                if txt:
                    detalle.append("Mensaje en la página: " + txt[:200])
            return "ERROR", detalle, r.text

        # 4) "Pedir cita" del primer trámite (Trámites de conductores)
        m = re.search(r"submitForm\('tramites','(tramites:items:\d+:[^']+)'\)", r.text)
        if not m:
            return "ERROR", ["No encuentro el botón 'Pedir cita' en el catálogo"], r.text
        datos_t = campos_formulario(form_t)
        datos_t["tramites_SUBMIT"] = "1"
        datos_t["tramites:_idcl"] = m.group(1)
        r2 = self._post(URL_CATALOGO, datos_t, URL_CATALOGO)
        r2.raise_for_status()

        avisos = mensajes_de_aviso(r2.text)
        if avisos:
            return "SIN_CITAS", avisos, r2.text
        return "POSIBLE_DISPONIBILIDAD", ["URL final: %s" % r2.url], r2.text


def elegir_centro(dgt, texto):
    """Resuelve --centro (nombre) a un (campo, id). Sale con un mensaje si no es unívoco."""
    q = norm(texto)
    candidatos = []
    for campo, opciones in dgt.listar_centros():
        for valor, nombre in opciones:
            if q in norm(nombre):
                candidatos.append((campo, valor, nombre))
    if not candidatos:
        sys.exit("No encontré ningún centro que contenga '%s'. Prueba con --listar." % texto)
    if len(candidatos) > 1:
        print("Hay varios centros que coinciden; usa --centro-id con uno de estos:")
        for _, v, n in candidatos:
            print("  %s  %s" % (v, n))
        sys.exit(1)
    return candidatos[0][0], candidatos[0][1], candidatos[0][2]


def notificar(asunto, cuerpo):
    """Envía el aviso por los canales configurados. Devuelve True si alguno funcionó."""
    ok = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            r = requests.post("https://ntfy.sh/", timeout=20, json={
                "topic": topic, "title": asunto, "message": cuerpo, "priority": 4,
                "tags": ["calendar"], "click": URL_INICIO})
            r.raise_for_status()
            print("    Aviso enviado por ntfy.")
            ok = True
        except requests.RequestException as e:
            print("    No pude enviar el aviso por ntfy: %s" % e)

    host = os.environ.get("SMTP_HOST", "").strip()
    destino = os.environ.get("MAIL_TO", "").strip()
    if host and destino:
        usuario = os.environ.get("SMTP_USER", "").strip()
        clave = os.environ.get("SMTP_PASS", "")
        puerto = int(os.environ.get("SMTP_PORT", "587") or 587)
        msg = EmailMessage()
        msg["Subject"] = asunto
        msg["From"] = usuario or destino
        msg["To"] = destino
        msg.set_content(cuerpo)
        try:
            ctx = ssl.create_default_context()
            if puerto == 465:
                srv = smtplib.SMTP_SSL(host, puerto, timeout=30, context=ctx)
            else:
                srv = smtplib.SMTP(host, puerto, timeout=30)
                srv.starttls(context=ctx)
            with srv:
                if usuario:
                    srv.login(usuario, clave)
                srv.send_message(msg)
            print("    Aviso enviado por correo a %s." % destino)
            ok = True
        except (smtplib.SMTPException, OSError) as e:
            print("    No pude enviar el correo: %s" % e)

    if not ok:
        print("    (No hay ningún canal de aviso configurado o han fallado: solo queda el mensaje en pantalla.)")
    return ok


def ejecutar_una_vez(dgt, centro_id, campo, etiqueta, guardar):
    estado, detalle, html = dgt.comprobar(centro_id, campo)
    hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s -> %s" % (hora, etiqueta, estado))
    for d in detalle:
        print("    " + d)
    if estado == "POSIBLE_DISPONIBILIDAD":
        fichero = "resultado_%s.html" % centro_id
        with open(fichero, "w", encoding="utf-8") as f:
            f.write(html)
        print("    Guardé la respuesta en %s." % fichero)
    elif estado == "ERROR" and (guardar or DEPURAR):
        with open("error_%s.html" % centro_id, "w", encoding="utf-8") as f:
            f.write(html)
    return estado


def ronda(centros, guardar):
    """Comprueba cada centro con sesión nueva. Devuelve las etiquetas con posible disponibilidad."""
    libres = []
    for n, (campo, cid, etiqueta) in enumerate(centros):
        if n:
            time.sleep(5)  # pausa entre oficinas para no saturar la web
        try:
            estado = ejecutar_una_vez(Dgt(), cid, campo, etiqueta, guardar)
        except (requests.RequestException, RuntimeError) as e:
            print("[%s] %s -> error: %s" % (datetime.now().strftime("%H:%M:%S"), etiqueta, e))
            continue
        if estado == "POSIBLE_DISPONIBILIDAD":
            libres.append(etiqueta)
    return libres


def avisar_libres(libres):
    asunto = "DGT: posible cita libre en %s" % ", ".join(libres)
    cuerpo = ("La comprobación automática no ha mostrado el aviso de 'sin capacidad' en:\n- %s\n\n"
              "Puede ser una cita libre: entra cuanto antes y compruébalo:\n%s\n\n(%s)"
              % ("\n- ".join(libres), URL_INICIO, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("\a", end="")
    notificar(asunto, cuerpo)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listar", action="store_true", help="lista los centros disponibles y termina")
    ap.add_argument("--centro", help="parte del nombre del centro; varios separados por comas")
    ap.add_argument("--centro-id", help="id numérico del centro; varios separados por comas (p. ej. 546,545)")
    ap.add_argument("--vigilar", action="store_true", help="repite la ronda hasta que aparezca disponibilidad")
    ap.add_argument("--cada", type=float, default=10, help="minutos entre rondas al vigilar (mín. 5)")
    ap.add_argument("--probar-aviso", action="store_true", help="envía un aviso de prueba y termina")
    ap.add_argument("--guardar-error", action="store_true", help="guarda el HTML cuando hay un error")
    ap.add_argument("--depurar", action="store_true", help="muestra los campos que se envían (ayuda a diagnosticar)")
    a = ap.parse_args()
    global DEPURAR
    DEPURAR = a.depurar

    if a.probar_aviso:
        ok = notificar("DGT: aviso de prueba", "Si lees esto, los avisos funcionan.")
        sys.exit(0 if ok else 1)

    dgt = Dgt()
    if a.listar:
        sels = dgt.listar_centros()
        if not sels:
            sys.exit("No pude leer la lista de centros; usa --centro-id con el id que veas en tu navegador.")
        for campo, opciones in sels:
            print("Campo:", campo)
            for v, n in opciones:
                print("  %s  %s" % (v, n))
        return

    if not (a.centro or a.centro_id):
        ap.error("indica --centro, --centro-id o --listar")

    nombres = {}
    try:
        for _, opciones in dgt.listar_centros():
            nombres.update({v: n for v, n in opciones})
    except (requests.RequestException, RuntimeError):
        pass

    centros = []
    if a.centro_id:
        for cid in [c.strip() for c in a.centro_id.split(",") if c.strip()]:
            centros.append((None, cid, "%s (%s)" % (nombres.get(cid, "centro"), cid)))
    if a.centro:
        for nombre in [c.strip() for c in a.centro.split(",") if c.strip()]:
            campo, cid, etiqueta = elegir_centro(dgt, nombre)
            centros.append((campo, cid, "%s (%s)" % (etiqueta, cid)))

    if not a.vigilar:
        libres = ronda(centros, a.guardar_error)
        if libres:
            avisar_libres(libres)
        return

    espera = max(a.cada, 5) * 60
    print("Vigilando %d centro(s) cada %.0f min. Ctrl+C para parar." % (len(centros), espera / 60))
    while True:
        libres = ronda(centros, a.guardar_error)
        if libres:
            avisar_libres(libres)
            break
        time.sleep(espera)


if __name__ == "__main__":
    main()
