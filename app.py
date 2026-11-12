# ObsidianaMC — aplicación web (Flask + Supabase)
#
# Páginas:
#   Inicio .......... KPI + dashboard de Data Studio
#   Jugadores ....... crear, leer, editar, eliminar
#   Sesiones ........ registrar sesiones de juego (con mobs eliminados), ver, eliminar
#   Compras ......... registrar compras, reembolsar, eliminar
#   Catálogos ....... clanes, tienda (productos), modalidades, rangos y mobs (crear, editar, eliminar)
#   Reportes ........ auditoría de TODOS los cambios; permite restaurar lo eliminado y descargar CSV

import atexit
import csv
import io
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import errors, sql                 # errores de PostgreSQL y armado seguro de SQL
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool         # reutiliza conexiones: la app responde más rápido
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, url_for)

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
LOOKER_EMBED_URL = os.environ.get("LOOKER_EMBED_URL", "")   # enlace de inserción de Data Studio

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "clave-de-desarrollo")   # necesaria para los mensajes


# =====================================================================
# Conexión con Supabase
# Pool de conexiones: se abren unas pocas al arrancar y se REUTILIZAN en cada clic,
# en vez de abrir una conexión nueva (lenta) cada vez.
# =====================================================================
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1, max_size=5,                          # entre 1 y 5 conexiones abiertas
    max_idle=240,                                    # cierra las que llevan 4 min sin usarse
    check=ConnectionPool.check_connection,           # verifica que la conexión siga viva antes de usarla
    kwargs={"row_factory": dict_row,                 # filas como {"columna": valor}
            "prepare_threshold": None,               # compatible con el pooler de Supabase
            "sslmode": "require",                    # conexión cifrada
            "connect_timeout": 10},
    open=True)
atexit.register(pool.close)                          # cierra las conexiones al apagar la app (Ctrl + C)


def conectar():
    """Presta una conexión del pool. Al terminar el 'with' hace COMMIT (o ROLLBACK si hubo error)
    y la devuelve al pool para reutilizarla."""
    return pool.connection()


def consultar(consulta, parametros=None):
    """SELECT: devuelve la lista de filas."""
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute(consulta, parametros)
        return cursor.fetchall()


def ejecutar(consulta, parametros=None):
    """INSERT, UPDATE o DELETE. Al salir del 'with' sin error se hace COMMIT (se guarda en Supabase)."""
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute(consulta, parametros)
        return cursor.fetchone() if cursor.description else None


POR_PAGINA = 5                                        # filas por página en TODAS las tablas


# =====================================================================
# Imágenes
#  1) Imagen subida desde el formulario (tabla "imagenes" en Supabase): solo productos, rangos, mobs y modalidades.
#  2) Pixel art incluido en static/img/<carpeta>/<nombre-sin-tildes>.png
#  3) Si no hay ninguna: static/img/<carpeta>/_default.png
#     (jugador nuevo = minero, clan nuevo = escudo con espada, lo demás = signo de pregunta)
# =====================================================================
CARPETA_IMG = os.path.join(app.root_path, "static", "img")
CARPETA_DE_TABLA = {"jugadores": "avatares", "clanes": "clanes", "productos_tienda": "productos",
                    "rangos": "rangos", "mobs": "mobs", "modalidades": "modalidades"}
TABLAS_CON_SUBIDA = ("productos_tienda", "rangos", "mobs", "modalidades")
MAX_IMAGEN = 512 * 1024                               # 512 KB por imagen
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024    # límite de cualquier envío de formulario


def slug(texto):
    """'Mascota: Ajolote' -> 'mascota-ajolote'  ·  'VIP+' -> 'vip-plus'  ·  'Dragón del End' -> 'dragon-del-end'"""
    texto = (texto or "").replace("+1000", " 1000").replace("+", "-plus")
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", texto).strip("-")


def ids_con_imagen(tabla):
    """ids de la tabla que tienen imagen subida. Se consulta una sola vez por página."""
    if "imagenes" not in g:
        g.imagenes = {}
    if tabla not in g.imagenes:
        try:
            filas = consultar("SELECT id_registro FROM imagenes WHERE tabla = %s", (tabla,))
            g.imagenes[tabla] = {f["id_registro"] for f in filas}
        except errors.UndefinedTable:                 # aún no se ejecutó sql/migracion_imagenes.sql
            g.imagenes[tabla] = set()
    return g.imagenes[tabla]


@app.template_global()
def imagen_url(tabla, nombre, id_registro=None):
    """Dirección de la imagen de un registro (se usa en las plantillas)."""
    if id_registro is not None and tabla in TABLAS_CON_SUBIDA and id_registro in ids_con_imagen(tabla):
        return url_for("imagen_subida", tabla=tabla, id_registro=id_registro)
    carpeta = CARPETA_DE_TABLA[tabla]
    archivo = f"{slug(nombre)}.png"
    if not os.path.exists(os.path.join(CARPETA_IMG, carpeta, archivo)):
        archivo = "_default.png"
    return url_for("static", filename=f"img/{carpeta}/{archivo}")


def tipo_de_imagen(datos):
    """Reconoce el formato por los primeros bytes del archivo (no se confía en el nombre)."""
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if datos.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if datos[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp"
    return None


def leer_imagen_del_formulario():
    """Devuelve (tipo, bytes) de la imagen elegida, o None si no se eligió ninguna."""
    archivo = request.files.get("imagen")
    if not archivo or not archivo.filename:
        return None
    datos = archivo.read()
    if len(datos) > MAX_IMAGEN:
        raise ValueError("La imagen pesa más de 512 KB.")
    tipo = tipo_de_imagen(datos)
    if not tipo:
        raise ValueError("La imagen debe ser PNG, JPG, GIF o WEBP.")
    return tipo, datos


def guardar_imagen(tabla, id_registro, imagen):
    """Guarda (o reemplaza) la imagen del registro en Supabase, o la quita si se marcó 'Quitar imagen'."""
    if imagen:
        ejecutar("""
            INSERT INTO imagenes (tabla, id_registro, tipo, datos) VALUES (%s, %s, %s, %s)
            ON CONFLICT (tabla, id_registro)
            DO UPDATE SET tipo = EXCLUDED.tipo, datos = EXCLUDED.datos, actualizado = now()
        """, (tabla, id_registro, imagen[0], imagen[1]))
    elif "quitar_imagen" in request.form:
        ejecutar("DELETE FROM imagenes WHERE tabla = %s AND id_registro = %s", (tabla, id_registro))


@app.route("/imagen/<tabla>/<int:id_registro>")
def imagen_subida(tabla, id_registro):
    if tabla not in TABLAS_CON_SUBIDA:
        abort(404)
    fila = consultar("SELECT tipo, datos FROM imagenes WHERE tabla = %s AND id_registro = %s",
                     (tabla, id_registro))
    if not fila:
        abort(404)
    return Response(bytes(fila[0]["datos"]), mimetype=fila[0]["tipo"],
                    headers={"Cache-Control": "no-cache"})


def paginar(consulta_total, consulta_filas, parametros, por_pagina, parametros_total=None):
    """Devuelve las filas de la página pedida (?pagina=N) y los datos para la paginación.
    Las dos consultas usan la MISMA conexión del pool."""
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute(consulta_total, parametros if parametros_total is None else parametros_total)
        total = cursor.fetchone()["n"]
        paginas = max(1, -(-total // por_pagina))                  # división redondeando hacia arriba
        pagina = request.args.get("pagina", 1, type=int) or 1
        pagina = min(max(pagina, 1), paginas)                      # evita páginas inválidas (0, -3, 999)
        cursor.execute(consulta_filas + " LIMIT %s OFFSET %s",
                       tuple(parametros) + (por_pagina, (pagina - 1) * por_pagina))
        filas = cursor.fetchall()
    return {"filas": filas, "pagina": pagina, "paginas": paginas, "total": total,
            "desde": (pagina - 1) * por_pagina + 1 if total else 0,
            "hasta": (pagina - 1) * por_pagina + len(filas)}


def recalcular_jugador(cursor, id_jugador):
    """Actualiza el nivel (según la experiencia acumulada) y el rango del jugador.
    Rango: el rango de pago más alto que haya comprado; si no tiene, el rango por nivel que le corresponde."""
    cursor.execute("""
        UPDATE jugadores SET nivel = LEAST(100, 1 + floor(sqrt(COALESCE(
            (SELECT sum(experiencia) FROM sesiones WHERE id_jugador = %(j)s), 0)) / 1.2))::int
        WHERE id_jugador = %(j)s
    """, {"j": id_jugador})
    cursor.execute("""
        UPDATE jugadores SET id_rango = COALESCE(
            (SELECT r.id_rango FROM compras c
             JOIN productos_tienda p ON p.id_producto = c.id_producto
             JOIN rangos r ON r.id_rango = p.id_rango
             WHERE c.id_jugador = %(j)s AND c.estado = 'completada'
             ORDER BY r.orden DESC LIMIT 1),
            (SELECT r.id_rango FROM rangos r
             WHERE r.tipo = 'nivel' AND COALESCE(r.nivel_requerido, 1) <= jugadores.nivel
             ORDER BY r.orden DESC LIMIT 1),
            (SELECT id_rango FROM rangos ORDER BY orden LIMIT 1))
        WHERE id_jugador = %(j)s
    """, {"j": id_jugador})


# Ciudades disponibles: país y coordenadas (para el mapa de Data Studio)
CIUDADES = {
    "Manizales": ("Colombia", 5.06889, -75.51738), "Bogotá": ("Colombia", 4.71099, -74.07209),
    "Medellín": ("Colombia", 6.24420, -75.58121), "Cali": ("Colombia", 3.45165, -76.53199),
    "Pereira": ("Colombia", 4.81333, -75.69611), "Armenia": ("Colombia", 4.53389, -75.68111),
    "Barranquilla": ("Colombia", 10.96854, -74.78132),
    "Ciudad de México": ("México", 19.43261, -99.13321), "Guadalajara": ("México", 20.65970, -103.34961),
    "Monterrey": ("México", 25.68661, -100.31611),
    "Buenos Aires": ("Argentina", -34.60372, -58.38159), "Córdoba": ("Argentina", -31.42008, -64.18878),
    "Santiago": ("Chile", -33.44889, -70.66927), "Lima": ("Perú", -12.04637, -77.04279),
    "Quito": ("Ecuador", -0.18065, -78.46784), "Caracas": ("Venezuela", 10.48801, -66.87919),
    "Madrid": ("España", 40.41678, -3.70379), "Miami": ("Estados Unidos", 25.76168, -80.19179),
}
METODOS_PAGO = ["PSE", "tarjeta crédito", "PayPal", "tarjeta regalo"]


# =====================================================================
# Inicio
# =====================================================================
@app.route("/")
def inicio():
    # Indicadores generales (una sola consulta)
    kpis = consultar("""
        SELECT
          (SELECT count(*) FROM jugadores)                                   AS jugadores,
          (SELECT count(*) FROM jugadores WHERE activo)                      AS activos,
          (SELECT COALESCE(sum(duracion_min), 0) / 60 FROM sesiones)         AS horas,
          (SELECT count(*) FROM sesiones)                                    AS sesiones,
          (SELECT COALESCE(round(avg(duracion_min)), 0) FROM sesiones)       AS promedio_min,
          (SELECT COALESCE(sum(monto_usd), 0) FROM compras
            WHERE estado = 'completada')                                     AS ingresos,
          (SELECT count(*) FROM compras WHERE estado = 'completada')         AS compras_ok,
          (SELECT count(*) FROM compras WHERE estado = 'reembolsada')        AS reembolsadas,
          (SELECT count(*) FROM jugadores j JOIN rangos r ON r.id_rango = j.id_rango
            WHERE r.tipo = 'pago')                                           AS con_rango_pago,
          (SELECT COALESCE(sum(bloques_minados), 0) FROM sesiones)           AS bloques,
          (SELECT COALESCE(sum(cantidad), 0) FROM sesion_mobs)               AS mobs,
          (SELECT count(*) FROM clanes)                                      AS clanes,
          (SELECT count(*) FROM jugadores WHERE id_clan IS NOT NULL)         AS en_clan,
          (SELECT count(*) FROM jugadores WHERE edicion = 'Java')            AS java,
          (SELECT count(*) FROM jugadores WHERE edicion = 'Bedrock')         AS bedrock
    """)[0]

    top_nivel = consultar("""
        SELECT j.nickname, j.nivel, r.nombre AS rango
        FROM jugadores j JOIN rangos r ON r.id_rango = j.id_rango
        ORDER BY j.nivel DESC, j.nickname
        LIMIT 5""")

    modalidades = consultar("""
        SELECT m.id_modalidad, m.nombre, count(s.id_sesion) AS sesiones
        FROM modalidades m LEFT JOIN sesiones s ON s.id_modalidad = m.id_modalidad
        GROUP BY m.id_modalidad, m.nombre
        ORDER BY sesiones DESC, m.nombre
        LIMIT 6""")

    # Los 5 países con más jugadores; el resto se agrupa en "Otros"
    paises = consultar("SELECT pais, count(*) AS n FROM jugadores GROUP BY pais ORDER BY n DESC, pais")
    if len(paises) > 6:
        otros = paises[5:]
        paises = paises[:5] + [{"pais": f"Otros ({len(otros)})", "n": sum(p["n"] for p in otros)}]

    mob_top = consultar("""
        SELECT mb.id_mob, mb.nombre, sum(sm.cantidad) AS n
        FROM sesion_mobs sm JOIN mobs mb ON mb.id_mob = sm.id_mob
        GROUP BY mb.id_mob, mb.nombre ORDER BY n DESC LIMIT 1""")
    mas_vendido = consultar("""
        SELECT p.id_producto, p.nombre, count(*) AS n
        FROM compras c JOIN productos_tienda p ON p.id_producto = c.id_producto
        WHERE c.estado = 'completada'
        GROUP BY p.id_producto, p.nombre ORDER BY n DESC, p.nombre LIMIT 1""")
    mas_factura = consultar("""
        SELECT p.id_producto, p.nombre, sum(c.monto_usd) AS total
        FROM compras c JOIN productos_tienda p ON p.id_producto = c.id_producto
        WHERE c.estado = 'completada'
        GROUP BY p.id_producto, p.nombre ORDER BY total DESC LIMIT 1""")

    return render_template(
        "index.html", kpis=kpis, top_nivel=top_nivel, modalidades=modalidades, paises=paises,
        mob_top=mob_top[0] if mob_top else None,
        mas_vendido=mas_vendido[0] if mas_vendido else None,
        mas_factura=mas_factura[0] if mas_factura else None,
        looker_url=LOOKER_EMBED_URL)


# =====================================================================
# Jugadores
# =====================================================================


COLOR_RANGO = {"Usuario": "#9e9e9e", "VIP": "#5fbf3f", "VIP+": "#3fa9f5", "MVP": "#a874ff", "Leyenda": "#ffb020"}


@app.template_global()
def color_rango(nombre):
    """Color del borde de la tarjeta y de la barra de nivel según el rango."""
    return COLOR_RANGO.get(nombre, "#8d879b")


# Opciones de "Ordenar" (lista cerrada: nunca se pega texto del usuario en el SQL)
ORDEN_JUGADORES = {
    "nivel":     ("Nivel ↓",       "j.nivel DESC, j.nickname"),
    "nivel_asc": ("Nivel ↑",       "j.nivel ASC, j.nickname"),
    "nombre":    ("Nombre A-Z",    "j.nickname"),
    "recientes": ("Más recientes", "j.id_jugador DESC"),
}


@app.route("/jugadores")
def jugadores():
    a = request.args
    filtros = {                                          # lo que llega en la URL (?q=...&rango=...)
        "q": a.get("q", "").strip(),
        "estado": a.get("estado", "") if a.get("estado") in ("activos", "inactivos") else "",
        "rango": a.get("rango", ""),
        "edicion": a.get("edicion", "") if a.get("edicion") in ("Java", "Bedrock") else "",
        "pais": a.get("pais", ""),
        "clan": a.get("clan", ""),
        "orden": a.get("orden", "") if a.get("orden") in ORDEN_JUGADORES else "",
    }

    # Condiciones del WHERE (sin el estado, para poder contar Todos / Activos / Inactivos)
    condiciones, parametros = [], []
    if filtros["q"]:
        condiciones.append("(j.nickname ILIKE %s OR j.ciudad ILIKE %s OR j.pais ILIKE %s)")
        parametros += [f"%{filtros['q']}%"] * 3
    if filtros["rango"] == "pago":
        condiciones.append("r.tipo = 'pago'")
    elif filtros["rango"].isdigit():
        condiciones.append("j.id_rango = %s")
        parametros.append(int(filtros["rango"]))
    if filtros["edicion"]:
        condiciones.append("j.edicion = %s")
        parametros.append(filtros["edicion"])
    if filtros["pais"]:
        condiciones.append("j.pais = %s")
        parametros.append(filtros["pais"])
    if filtros["clan"] == "sin":
        condiciones.append("j.id_clan IS NULL")
    elif filtros["clan"].isdigit():
        condiciones.append("j.id_clan = %s")
        parametros.append(int(filtros["clan"]))

    desde = "FROM jugadores j JOIN rangos r ON r.id_rango = j.id_rango LEFT JOIN clanes c ON c.id_clan = j.id_clan"
    where_base = " AND ".join(condiciones) or "TRUE"
    conteo = consultar(f"""SELECT count(*) AS todos,
                                  count(*) FILTER (WHERE j.activo)     AS activos,
                                  count(*) FILTER (WHERE NOT j.activo) AS inactivos
                           {desde} WHERE {where_base}""", tuple(parametros))[0]

    where = where_base
    if filtros["estado"]:
        where += " AND " + ("j.activo" if filtros["estado"] == "activos" else "NOT j.activo")

    if filtros["orden"]:
        orden = ORDEN_JUGADORES[filtros["orden"]][1]
    elif filtros["q"]:                                   # al buscar: primero los que EMPIEZAN por lo escrito
        orden = "j.nickname ILIKE %s DESC, j.nivel DESC"
    else:
        orden = "j.id_jugador DESC"
    parametros_filas = parametros + ([f"{filtros['q']}%"] if not filtros["orden"] and filtros["q"] else [])

    datos = paginar(
        f"SELECT count(*) AS n {desde} WHERE {where}",
        f"""SELECT j.id_jugador, j.nickname, j.ciudad, j.pais, j.edicion, j.nivel, j.activo,
                   r.nombre AS rango, c.nombre AS clan
            {desde} WHERE {where} ORDER BY {orden}""",
        tuple(parametros_filas), POR_PAGINA, parametros_total=tuple(parametros))
    datos["jugadores"] = datos.pop("filas")

    opciones = {
        "rangos": consultar("SELECT id_rango, nombre FROM rangos ORDER BY orden"),
        "paises": [f["pais"] for f in consultar("SELECT DISTINCT pais FROM jugadores ORDER BY pais")],
        "clanes": consultar("SELECT id_clan, nombre FROM clanes ORDER BY nombre"),
        "orden": {k: v[0] for k, v in ORDEN_JUGADORES.items()},
    }

    # Etiquetas de los filtros activos (con ✕ para quitarlos)
    nombres_rango = {str(r["id_rango"]): r["nombre"] for r in opciones["rangos"]}
    nombres_clan = {str(c["id_clan"]): c["nombre"] for c in opciones["clanes"]}
    etiquetas = []
    if filtros["rango"]:
        etiquetas.append(("rango", "De pago" if filtros["rango"] == "pago" else nombres_rango.get(filtros["rango"], "Rango")))
    if filtros["edicion"]:
        etiquetas.append(("edicion", filtros["edicion"]))
    if filtros["pais"]:
        etiquetas.append(("pais", filtros["pais"]))
    if filtros["clan"]:
        etiquetas.append(("clan", "Sin clan" if filtros["clan"] == "sin" else nombres_clan.get(filtros["clan"], "Clan")))

    filtros_url = {k: v for k, v in filtros.items() if v}   # solo los filtros usados, para armar los enlaces
    return render_template("jugadores.html", filtros=filtros, filtros_url=filtros_url, conteo=conteo,
                           opciones=opciones, etiquetas=etiquetas, busqueda=filtros["q"], **datos)


def datos_del_formulario():
    """Lee el formulario de jugador y devuelve los valores en el orden de las columnas."""
    f = request.form
    ciudad = f["ciudad"]
    pais, latitud, longitud = CIUDADES[ciudad]          # el país y las coordenadas salen de la ciudad
    id_clan = int(f["id_clan"]) if f["id_clan"] else None
    return (f["nickname"].strip(), f["email"].strip().lower(), pais, ciudad, latitud, longitud,
            f["edicion"], int(f["id_rango"]), id_clan, "activo" in f)


def listas_para_el_formulario():
    """Opciones de los <select>: rangos, clanes y ciudades."""
    return {
        "rangos": consultar("SELECT id_rango, nombre FROM rangos ORDER BY orden"),
        "clanes": consultar("SELECT id_clan, nombre FROM clanes ORDER BY nombre"),
        "ciudades": CIUDADES,
    }


@app.route("/jugadores/nuevo", methods=["GET", "POST"])
def jugador_nuevo():
    if request.method == "POST":                        # se presionó "Guardar"
        try:
            fila = ejecutar("""
                INSERT INTO jugadores (nickname, email, pais, ciudad, latitud, longitud,
                                       edicion, id_rango, id_clan, activo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id_jugador
            """, datos_del_formulario())
            flash(f"Jugador guardado en Supabase (id {fila['id_jugador']}).", "ok")
            return redirect(url_for("jugadores"))
        except errors.UniqueViolation:                  # nickname o email repetido
            flash("Ese nickname o email ya está registrado.", "error")
    return render_template("jugador_form.html", titulo="Nuevo jugador",
                           j=request.form, **listas_para_el_formulario())


@app.route("/jugadores/<int:id_jugador>/editar", methods=["GET", "POST"])
def jugador_editar(id_jugador):
    actual = consultar("SELECT * FROM jugadores WHERE id_jugador = %s", (id_jugador,))
    if not actual:
        abort(404)                                      # no existe ese jugador
    if request.method == "POST":
        try:
            ejecutar("""
                UPDATE jugadores
                SET nickname = %s, email = %s, pais = %s, ciudad = %s, latitud = %s, longitud = %s,
                    edicion = %s, id_rango = %s, id_clan = %s, activo = %s
                WHERE id_jugador = %s
            """, datos_del_formulario() + (id_jugador,))
            flash("Jugador actualizado en Supabase.", "ok")
            return redirect(url_for("jugadores"))
        except errors.UniqueViolation:
            flash("Ese nickname o email ya está registrado.", "error")
    return render_template("jugador_form.html", titulo="Editar jugador",
                           j=request.form or actual[0], **listas_para_el_formulario())


@app.route("/jugadores/<int:id_jugador>/eliminar", methods=["POST"])
def jugador_eliminar(id_jugador):
    try:
        ejecutar("DELETE FROM jugadores WHERE id_jugador = %s", (id_jugador,))
        flash("Jugador eliminado de Supabase (sus sesiones se borraron en cascada). "
              "Puede restaurarlo desde Reportes.", "ok")
    except errors.ForeignKeyViolation:                  # tiene compras: ON DELETE RESTRICT
        flash("No se puede eliminar: el jugador tiene compras registradas (ON DELETE RESTRICT).", "error")
    return redirect(url_for("jugadores"))


# =====================================================================
# Sesiones de juego (tablas: sesiones + sesion_mobs)
# =====================================================================
@app.route("/sesiones")
def sesiones():
    modalidad = request.args.get("modalidad", type=int)
    datos = paginar(
        "SELECT count(*) AS n FROM sesiones WHERE (%s::int IS NULL OR id_modalidad = %s)",
        """SELECT s.id_sesion, s.inicio, s.duracion_min, s.bloques_minados, s.muertes, s.experiencia,
                  j.nickname, m.id_modalidad, m.nombre AS modalidad, m.tipo,
                  (SELECT COALESCE(sum(cantidad), 0) FROM sesion_mobs sm
                   WHERE sm.id_sesion = s.id_sesion) AS mobs
           FROM sesiones s
           JOIN jugadores j   ON j.id_jugador   = s.id_jugador
           JOIN modalidades m ON m.id_modalidad = s.id_modalidad
           WHERE (%s::int IS NULL OR s.id_modalidad = %s)
           ORDER BY s.inicio DESC""",
        (modalidad, modalidad), POR_PAGINA)
    return render_template("sesiones.html", modalidad=modalidad,
                           modalidades=consultar("SELECT id_modalidad, nombre FROM modalidades ORDER BY nombre"),
                           **datos)


@app.route("/sesiones/nueva", methods=["GET", "POST"])
def sesion_nueva():
    if request.method == "POST":
        f = request.form
        try:
            id_jugador = int(f.get("id_jugador", ""))
            mobs = {}                                   # {id_mob: cantidad}
            for id_mob, cantidad in zip(f.getlist("id_mob"), f.getlist("cantidad")):
                if id_mob and cantidad and int(cantidad) > 0:
                    mobs[int(id_mob)] = mobs.get(int(id_mob), 0) + int(cantidad)

            # Una sola transacción: si algo falla, no se guarda nada
            with conectar() as conexion, conexion.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO sesiones (id_jugador, id_modalidad, duracion_min,
                                          bloques_minados, bloques_colocados, muertes)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id_sesion
                """, (id_jugador, int(f.get("id_modalidad", "")), int(f.get("duracion_min", "")),
                      int(f.get("bloques_minados") or 0), int(f.get("bloques_colocados") or 0),
                      int(f.get("muertes") or 0)))
                id_sesion = cursor.fetchone()["id_sesion"]

                for id_mob, cantidad in mobs.items():   # tabla sesion_mobs
                    cursor.execute("INSERT INTO sesion_mobs (id_sesion, id_mob, cantidad) VALUES (%s, %s, %s)",
                                   (id_sesion, id_mob, cantidad))

                # Experiencia = XP de los mobs + 1 XP por cada 20 bloques + 3 XP por minuto en PvP
                cursor.execute("""
                    UPDATE sesiones s SET experiencia =
                        COALESCE((SELECT sum(sm.cantidad * mb.experiencia) FROM sesion_mobs sm
                                  JOIN mobs mb ON mb.id_mob = sm.id_mob WHERE sm.id_sesion = s.id_sesion), 0)
                        + s.bloques_minados / 20
                        + CASE WHEN m.tipo = 'pvp' THEN s.duracion_min * 3 ELSE 0 END
                    FROM modalidades m
                    WHERE m.id_modalidad = s.id_modalidad AND s.id_sesion = %s
                """, (id_sesion,))
                cursor.execute("UPDATE jugadores SET activo = TRUE WHERE id_jugador = %s", (id_jugador,))
                recalcular_jugador(cursor, id_jugador)

            flash(f"Sesión #{id_sesion} guardada en Supabase.", "ok")
            return redirect(url_for("sesion_detalle", id_sesion=id_sesion))
        except (ValueError, errors.CheckViolation):
            flash("Revise los números: la duración debe ser mayor que 0 y no se aceptan negativos.", "error")

    return render_template(
        "sesion_form.html", s=request.form,
        jugadores=consultar("SELECT id_jugador, nickname FROM jugadores ORDER BY nickname"),
        modalidades=consultar("SELECT id_modalidad, nombre, tipo FROM modalidades ORDER BY nombre"),
        mobs=consultar("SELECT id_mob, nombre, tipo, experiencia FROM mobs ORDER BY nombre"))


@app.route("/sesiones/<int:id_sesion>")
def sesion_detalle(id_sesion):
    fila = consultar("""
        SELECT s.*, j.nickname, j.nivel, m.nombre AS modalidad, m.tipo
        FROM sesiones s
        JOIN jugadores j   ON j.id_jugador   = s.id_jugador
        JOIN modalidades m ON m.id_modalidad = s.id_modalidad
        WHERE s.id_sesion = %s
    """, (id_sesion,))
    if not fila:
        abort(404)
    datos = paginar(
        "SELECT count(*) AS n FROM sesion_mobs WHERE id_sesion = %s",
        """SELECT mb.id_mob, mb.nombre, mb.tipo, mb.dimension, sm.cantidad, sm.cantidad * mb.experiencia AS xp
           FROM sesion_mobs sm JOIN mobs mb ON mb.id_mob = sm.id_mob
           WHERE sm.id_sesion = %s ORDER BY sm.cantidad DESC, mb.nombre""",
        (id_sesion,), POR_PAGINA)
    return render_template("sesion_detalle.html", s=fila[0], **datos)


@app.route("/sesiones/<int:id_sesion>/eliminar", methods=["POST"])
def sesion_eliminar(id_sesion):
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute("DELETE FROM sesiones WHERE id_sesion = %s RETURNING id_jugador", (id_sesion,))
        fila = cursor.fetchone()
        if fila:
            recalcular_jugador(cursor, fila["id_jugador"])
    flash(f"Sesión #{id_sesion} eliminada (y sus mobs, en cascada). Puede restaurarla desde Reportes.", "ok")
    return redirect(url_for("sesiones"))


# =====================================================================
# Compras
# =====================================================================
@app.route("/compras")
def compras():
    estado = request.args.get("estado") or None
    datos = paginar(
        "SELECT count(*) AS n FROM compras WHERE (%s::text IS NULL OR estado = %s)",
        """SELECT c.id_compra, c.fecha, c.cantidad, c.monto_usd, c.metodo_pago, c.estado,
                  j.nickname, p.id_producto, p.nombre AS producto, p.categoria
           FROM compras c
           JOIN jugadores j        ON j.id_jugador  = c.id_jugador
           JOIN productos_tienda p ON p.id_producto = c.id_producto
           WHERE (%s::text IS NULL OR c.estado = %s)
           ORDER BY c.fecha DESC""",
        (estado, estado), POR_PAGINA)
    return render_template("compras.html", estado=estado, **datos)


@app.route("/compras/nueva", methods=["GET", "POST"])
def compra_nueva():
    if request.method == "POST":
        f = request.form
        try:
            id_jugador, id_producto = int(f.get("id_jugador", "")), int(f.get("id_producto", ""))
            cantidad = int(f.get("cantidad") or 1)
            if cantidad < 1 or f.get("metodo_pago") not in METODOS_PAGO:
                raise ValueError
            with conectar() as conexion, conexion.cursor() as cursor:
                cursor.execute("SELECT nombre, categoria, precio_usd FROM productos_tienda WHERE id_producto = %s",
                               (id_producto,))
                producto = cursor.fetchone()
                if producto["categoria"] == "rango":    # un rango se compra una sola vez
                    cantidad = 1
                cursor.execute("""
                    INSERT INTO compras (id_jugador, id_producto, cantidad, monto_usd, metodo_pago)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id_compra
                """, (id_jugador, id_producto, cantidad, producto["precio_usd"] * cantidad, f["metodo_pago"]))
                id_compra = cursor.fetchone()["id_compra"]
                recalcular_jugador(cursor, id_jugador)   # si compró un rango, sube de rango
            flash(f"Compra #{id_compra} guardada en Supabase: {producto['nombre']} x{cantidad}.", "ok")
            return redirect(url_for("compras"))
        except ValueError:
            flash("Revise la cantidad y el método de pago.", "error")

    return render_template(
        "compra_form.html", c=request.form, metodos=METODOS_PAGO,
        jugadores=consultar("SELECT id_jugador, nickname FROM jugadores ORDER BY nickname"),
        productos=consultar("""SELECT id_producto, nombre, categoria, precio_usd FROM productos_tienda
                               WHERE activo ORDER BY categoria, nombre"""))


@app.route("/compras/<int:id_compra>/reembolsar", methods=["POST"])
def compra_reembolsar(id_compra):
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute("""UPDATE compras SET estado = 'reembolsada'
                          WHERE id_compra = %s AND estado = 'completada' RETURNING id_jugador""", (id_compra,))
        fila = cursor.fetchone()
        if fila:
            recalcular_jugador(cursor, fila["id_jugador"])   # si era un rango, lo pierde
    if fila:
        flash(f"Compra #{id_compra} reembolsada.", "ok")
    else:
        flash("Esa compra ya estaba reembolsada.", "error")
    return redirect(url_for("compras"))


@app.route("/compras/<int:id_compra>/eliminar", methods=["POST"])
def compra_eliminar(id_compra):
    with conectar() as conexion, conexion.cursor() as cursor:
        cursor.execute("DELETE FROM compras WHERE id_compra = %s RETURNING id_jugador", (id_compra,))
        fila = cursor.fetchone()
        if fila:
            recalcular_jugador(cursor, fila["id_jugador"])
    flash(f"Compra #{id_compra} eliminada. Puede restaurarla desde Reportes.", "ok")
    return redirect(url_for("compras"))


# =====================================================================
# Catálogos: clanes, tienda, modalidades, rangos y mobs
# Una sola configuración por tabla; las mismas rutas y plantillas sirven para todas.
# =====================================================================
def campo(nombre, etiqueta, tipo="texto", requerido=True, maximo=None, opciones=None, minimo=None):
    return {"nombre": nombre, "etiqueta": etiqueta, "tipo": tipo, "requerido": requerido,
            "maximo": maximo, "opciones": opciones or [], "minimo": minimo}


CATALOGOS = {
    "clanes": {
        "titulo": "Clanes", "singular": "clan", "pk": "id_clan",
        "lista": """SELECT c.id_clan AS id, c.nombre, c.etiqueta, count(j.id_jugador) AS miembros
                    FROM clanes c LEFT JOIN jugadores j ON j.id_clan = c.id_clan
                    GROUP BY c.id_clan ORDER BY c.nombre""",
        "columnas": [("nombre", "Nombre"), ("etiqueta", "Etiqueta"), ("miembros", "Miembros")],
        "campos": [campo("nombre", "Nombre del clan", maximo=60),
                   campo("etiqueta", "Etiqueta (máx. 5)", maximo=5)],
        "nota": "Al eliminar un clan, sus miembros quedan sin clan (ON DELETE SET NULL).",
    },
    "productos_tienda": {
        "titulo": "Tienda", "singular": "producto", "pk": "id_producto",
        "lista": """SELECT p.id_producto AS id, p.nombre, p.categoria, r.nombre AS rango, p.precio_usd,
                           CASE WHEN p.activo THEN 'Sí' ELSE 'No' END AS disponible,
                           (SELECT COALESCE(sum(cantidad), 0) FROM compras c
                            WHERE c.id_producto = p.id_producto AND c.estado = 'completada') AS vendidos
                    FROM productos_tienda p LEFT JOIN rangos r ON r.id_rango = p.id_rango
                    ORDER BY p.categoria, p.nombre""",
        "columnas": [("nombre", "Producto"), ("categoria", "Categoría"), ("rango", "Rango que da"),
                     ("precio_usd", "Precio USD"), ("disponible", "Disponible"), ("vendidos", "Vendidos")],
        "campos": [campo("nombre", "Nombre", maximo=80),
                   campo("categoria", "Categoría", "select",
                         opciones=["rango", "llave", "cosmético", "potenciador", "kit"]),
                   campo("id_rango", "Rango que otorga (solo categoría rango)", "select_bd", requerido=False),
                   campo("precio_usd", "Precio (USD)", "decimal", minimo=0),
                   campo("activo", "Disponible en la tienda", "check", requerido=False)],
        "nota": "Un producto con compras no se puede eliminar (ON DELETE RESTRICT); desactívelo.",
    },
    "modalidades": {
        "titulo": "Modalidades", "singular": "modalidad", "femenino": True, "pk": "id_modalidad",
        "lista": """SELECT m.id_modalidad AS id, m.nombre, m.tipo, m.descripcion,
                           (SELECT count(*) FROM sesiones s WHERE s.id_modalidad = m.id_modalidad) AS sesiones
                    FROM modalidades m ORDER BY m.nombre""",
        "columnas": [("nombre", "Modalidad"), ("tipo", "Tipo"), ("descripcion", "Descripción"),
                     ("sesiones", "Sesiones")],
        "campos": [campo("nombre", "Nombre", maximo=40),
                   campo("tipo", "Tipo", "select", opciones=["pve", "pvp", "creativo"]),
                   campo("descripcion", "Descripción", "textarea", requerido=False)],
        "nota": "Una modalidad con sesiones no se puede eliminar (ON DELETE RESTRICT).",
    },
    "rangos": {
        "titulo": "Rangos", "singular": "rango", "pk": "id_rango",
        "lista": """SELECT r.id_rango AS id, r.nombre, r.tipo, r.orden, r.nivel_requerido, r.precio_usd,
                           (SELECT count(*) FROM jugadores j WHERE j.id_rango = r.id_rango) AS jugadores
                    FROM rangos r ORDER BY r.orden""",
        "columnas": [("nombre", "Rango"), ("tipo", "Tipo"), ("orden", "Orden"),
                     ("nivel_requerido", "Nivel requerido"), ("precio_usd", "Precio USD"), ("jugadores", "Jugadores")],
        "campos": [campo("nombre", "Nombre", maximo=20),
                   campo("tipo", "Tipo", "select", opciones=["nivel", "pago"]),
                   campo("orden", "Orden (número único; mayor = más importante)", "numero"),
                   campo("nivel_requerido", "Nivel requerido (solo rangos por nivel)", "numero",
                         requerido=False, minimo=1),
                   campo("precio_usd", "Precio (USD)", "decimal", minimo=0)],
        "nota": "Un rango asignado a jugadores no se puede eliminar (ON DELETE RESTRICT).",
    },
    "mobs": {
        "titulo": "Mobs", "singular": "mob", "pk": "id_mob",
        "lista": """SELECT mb.id_mob AS id, mb.nombre, mb.tipo, mb.dimension, mb.experiencia,
                           (SELECT COALESCE(sum(cantidad), 0) FROM sesion_mobs sm
                            WHERE sm.id_mob = mb.id_mob) AS eliminados
                    FROM mobs mb ORDER BY mb.nombre""",
        "columnas": [("nombre", "Mob"), ("tipo", "Tipo"), ("dimension", "Dimensión"),
                     ("experiencia", "XP que otorga"), ("eliminados", "Eliminados")],
        "campos": [campo("nombre", "Nombre", maximo=40),
                   campo("tipo", "Tipo", "select", opciones=["hostil", "neutral", "jefe"]),
                   campo("dimension", "Dimensión", "select", opciones=["Superficie", "Nether", "End", "Océano"]),
                   campo("experiencia", "XP que otorga", "numero", minimo=0)],
        "nota": "Un mob que ya aparece en sesiones no se puede eliminar (ON DELETE RESTRICT).",
    },
}


def terminacion(config, palabra):
    """Concordancia de género: 'guardado' -> 'guardada' para modalidad."""
    return palabra[:-1] + "a" if config.get("femenino") else palabra


def catalogo_o_404(tabla):
    if tabla not in CATALOGOS:
        abort(404)
    return CATALOGOS[tabla]


def opciones_bd():
    """Opciones de los <select> que vienen de la base de datos."""
    return {"id_rango": consultar("SELECT id_rango AS valor, nombre AS texto FROM rangos ORDER BY orden")}


def leer_campos(config):
    """Convierte lo escrito en el formulario al tipo de cada columna."""
    valores = []
    for c in config["campos"]:
        crudo = request.form.get(c["nombre"], "").strip()
        if c["tipo"] == "check":
            valores.append(c["nombre"] in request.form)
            continue
        if crudo == "":
            if c["requerido"]:
                raise ValueError(f"El campo «{c['etiqueta']}» es obligatorio.")
            valores.append(None)
            continue
        if c["tipo"] == "select" and crudo not in c["opciones"]:
            raise ValueError(f"Valor no permitido en «{c['etiqueta']}».")
        if c["tipo"] in ("numero", "select_bd"):
            valores.append(int(crudo))
        elif c["tipo"] == "decimal":
            valores.append(Decimal(crudo))
        else:
            valores.append(crudo)
    return valores


@app.route("/catalogo/<tabla>")
def catalogo(tabla):
    config = catalogo_o_404(tabla)
    datos = paginar("SELECT count(*) AS n FROM (" + config["lista"] + ") t", config["lista"], (), POR_PAGINA)
    return render_template("catalogo.html", tabla=tabla, config=config, **datos)


@app.route("/catalogo/<tabla>/nuevo", methods=["GET", "POST"])
@app.route("/catalogo/<tabla>/<int:id_registro>/editar", methods=["GET", "POST"])
def catalogo_form(tabla, id_registro=None):
    config = catalogo_o_404(tabla)
    columnas = [c["nombre"] for c in config["campos"]]
    actual = None
    if id_registro:
        filas = consultar(sql.SQL("SELECT * FROM {} WHERE {} = %s").format(
            sql.Identifier(tabla), sql.Identifier(config["pk"])), (id_registro,))
        if not filas:
            abort(404)
        actual = filas[0]

    if request.method == "POST":
        try:
            valores = leer_campos(config)
            imagen = leer_imagen_del_formulario() if tabla in TABLAS_CON_SUBIDA else None
            if id_registro:
                ejecutar(sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                    sql.Identifier(tabla),
                    sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columnas),
                    sql.Identifier(config["pk"])), valores + [id_registro])
                if tabla in TABLAS_CON_SUBIDA:
                    guardar_imagen(tabla, id_registro, imagen)
                flash(f"{config['singular'].capitalize()} {terminacion(config, 'actualizado')} en Supabase.", "ok")
            else:
                fila = ejecutar(sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
                    sql.Identifier(tabla),
                    sql.SQL(", ").join(map(sql.Identifier, columnas)),
                    sql.SQL(", ").join(sql.Placeholder() * len(columnas)),
                    sql.Identifier(config["pk"])), valores)
                if tabla in TABLAS_CON_SUBIDA:
                    guardar_imagen(tabla, fila[config["pk"]], imagen)
                flash(f"{config['singular'].capitalize()} {terminacion(config, 'guardado')} en Supabase "
                      f"(id {fila[config['pk']]}).", "ok")
            return redirect(url_for("catalogo", tabla=tabla))
        except errors.UniqueViolation:
            flash("Ya existe un registro con ese valor (nombre, etiqueta u orden repetido).", "error")
        except (errors.CheckViolation, errors.NumericValueOutOfRange, InvalidOperation):
            flash("Algún valor está fuera de lo permitido (por ejemplo, un número negativo).", "error")
        except errors.StringDataRightTruncation:
            flash("Algún texto es demasiado largo.", "error")
        except ValueError as e:
            flash(str(e) if str(e).startswith(("El campo", "Valor", "La imagen")) else "Revise los números.", "error")

    return render_template("catalogo_form.html", tabla=tabla, config=config,
                           r=request.form if request.method == "POST" else (actual or {}),
                           editando=bool(id_registro), opciones=opciones_bd(),
                           con_imagen=tabla in TABLAS_CON_SUBIDA, id_registro=id_registro,
                           tiene_subida=bool(id_registro) and tabla in TABLAS_CON_SUBIDA
                                        and id_registro in ids_con_imagen(tabla))


@app.route("/catalogo/<tabla>/<int:id_registro>/eliminar", methods=["POST"])
def catalogo_eliminar(tabla, id_registro):
    config = catalogo_o_404(tabla)
    try:
        ejecutar(sql.SQL("DELETE FROM {} WHERE {} = %s").format(
            sql.Identifier(tabla), sql.Identifier(config["pk"])), (id_registro,))
        flash(f"{config['singular'].capitalize()} {terminacion(config, 'eliminado')}. "
              f"Puede {terminacion(config, 'restaurarlo')} desde Reportes.", "ok")
    except errors.ForeignKeyViolation:
        flash(f"No se puede eliminar: {config['nota']}", "error")
    return redirect(url_for("catalogo", tabla=tabla))


# =====================================================================
# Reportes: auditoría de todos los cambios (la llena el trigger fn_auditoria en Supabase)
# =====================================================================
TABLAS_AUDITADAS = ["jugadores", "sesiones", "sesion_mobs", "compras", "clanes",
                    "productos_tienda", "modalidades", "rangos", "mobs"]
PK_DE = {"jugadores": "id_jugador", "sesiones": "id_sesion", "sesion_mobs": "id_sesion_mob",
         "compras": "id_compra", "clanes": "id_clan", "productos_tienda": "id_producto",
         "modalidades": "id_modalidad", "rangos": "id_rango", "mobs": "id_mob"}


def filtros_reporte():
    tabla = request.args.get("tabla") or None
    operacion = request.args.get("operacion") or None
    if tabla not in TABLAS_AUDITADAS:
        tabla = None
    if operacion not in ("INSERT", "UPDATE", "DELETE"):
        operacion = None
    return tabla, operacion


FILTRO_REPORTE = """WHERE (%s::text IS NULL OR tabla = %s) AND (%s::text IS NULL OR operacion = %s)"""


@app.route("/reportes")
def reportes():
    tabla, operacion = filtros_reporte()
    parametros = (tabla, tabla, operacion, operacion)
    datos = paginar(
        "SELECT count(*) AS n FROM auditoria_logs " + FILTRO_REPORTE,
        """SELECT a.*,
                  COALESCE(a.datos ->> 'nickname', a.datos ->> 'nombre',
                           CASE WHEN a.tabla = 'sesiones' THEN a.datos ->> 'duracion_min' || ' min'
                                WHEN a.tabla = 'compras' THEN 'US$ ' || (a.datos ->> 'monto_usd')
                                WHEN a.tabla = 'sesion_mobs' THEN (a.datos ->> 'cantidad') || ' mobs' END) AS resumen,
                  (a.operacion = 'DELETE') AS se_puede_restaurar
           FROM auditoria_logs a """ + FILTRO_REPORTE + " ORDER BY a.id_log DESC",
        parametros, POR_PAGINA)
    resumen = consultar("""SELECT operacion, count(*) AS n FROM auditoria_logs
                           GROUP BY operacion ORDER BY operacion""")
    return render_template("reportes.html", tabla=tabla, operacion=operacion, tablas=TABLAS_AUDITADAS,
                           resumen={r["operacion"]: r["n"] for r in resumen}, **datos)


@app.route("/reportes/<int:id_log>")
def reporte_detalle(id_log):
    fila = consultar("SELECT * FROM auditoria_logs WHERE id_log = %s", (id_log,))
    if not fila:
        abort(404)
    return render_template("reporte_detalle.html", log=fila[0])


@app.route("/reportes/<int:id_log>/restaurar", methods=["POST"])
def reporte_restaurar(id_log):
    """Vuelve a insertar un registro eliminado, con los mismos datos y el mismo id."""
    filas = consultar("SELECT * FROM auditoria_logs WHERE id_log = %s AND operacion = 'DELETE'", (id_log,))
    if not filas or filas[0]["tabla"] not in TABLAS_AUDITADAS:
        abort(404)
    log = filas[0]
    tabla = log["tabla"]
    # Columnas que se pueden insertar (se excluyen las calculadas, como fecha_dia)
    columnas = [c["column_name"] for c in consultar("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND is_generated = 'NEVER'
        ORDER BY ordinal_position""", (tabla,))]
    try:
        with conectar() as conexion, conexion.cursor() as cursor:
            cursor.execute(sql.SQL(
                "INSERT INTO {tabla} ({cols}) OVERRIDING SYSTEM VALUE "
                "SELECT {cols} FROM jsonb_populate_record(NULL::{tabla}, %s)").format(
                    tabla=sql.Identifier(tabla), cols=sql.SQL(", ").join(map(sql.Identifier, columnas))),
                (Jsonb(log["datos"]),))
            if tabla in ("sesiones", "compras") and log["datos"].get("id_jugador"):
                recalcular_jugador(cursor, log["datos"]["id_jugador"])
        flash(f"Registro restaurado en la tabla {tabla} (id {log['registro_id']}).", "ok")
    except errors.UniqueViolation:
        flash("Ese registro ya existe (ya fue restaurado o se creó otro con el mismo nombre).", "error")
    except errors.ForeignKeyViolation:
        flash("Primero restaure el registro del que depende (por ejemplo, el jugador antes que su sesión).", "error")
    return redirect(request.referrer or url_for("reportes"))


@app.route("/reportes/descargar")
def reportes_descargar():
    """Reporte en CSV (se abre en Excel) con los mismos filtros de la página."""
    tabla, operacion = filtros_reporte()
    filas = consultar("SELECT * FROM auditoria_logs " + FILTRO_REPORTE + " ORDER BY id_log DESC",
                      (tabla, tabla, operacion, operacion))
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(["id_log", "fecha", "tabla", "operacion", "registro_id", "usuario_db", "datos"])
    for f in filas:
        escritor.writerow([f["id_log"], f["fecha"].strftime("%Y-%m-%d %H:%M:%S"), f["tabla"],
                           f["operacion"], f["registro_id"], f["usuario_db"], f["datos"]])
    return Response("﻿" + salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=reporte_auditoria.csv"})


if __name__ == "__main__":
    app.run(debug=True)
