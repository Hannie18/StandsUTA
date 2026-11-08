import io
import json
import os
import re
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlparse
from uuid import uuid4
from conexion import obtener_conexion

import pymysql
import qrcode
from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

from datetime import datetime, timedelta

load_dotenv()

# Configurar Cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    secure=True
)

app = Flask(__name__)
app.secret_key = "cambia-esta-clave-por-una-propia"
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # fotos y videos de hasta 15 MB

EDAD_MINIMA = 13  # controlador de edad: cámbialo a 18 si lo prefieres
USER_RE = r"[A-Za-z0-9._]{3,30}"
SEXOS = {"masculino": "Masculino", "femenino": "Femenino", "otro": "Otro"}
FORMATOS_FOTO = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
CARPETA_FOTOS = os.path.join(app.static_folder, "uploads")
os.makedirs(CARPETA_FOTOS, exist_ok=True)


# ---------- Utilidades ----------
def get_db():
    return obtener_conexion()


def obtener_usuario(campo, valor):
    assert campo in ("id", "username")
    db = get_db()
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM usuarios WHERE {campo}=%s", (valor,))
        u = cur.fetchone()
    db.close()
    return u


def calcular_edad(nacimiento):
    hoy = date.today()
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


def fecha_maxima():
    """Última fecha de nacimiento permitida (para el atributo max del campo)."""
    hoy = date.today()
    try:
        return hoy.replace(year=hoy.year - EDAD_MINIMA)
    except ValueError:  # 29 de febrero
        return hoy.replace(year=hoy.year - EDAD_MINIMA, day=28)


def password_valida(p):
    # controlador de contraseña: 8+ caracteres, minúscula, mayúscula y número
    return (len(p) >= 8 and re.search(r"[a-z]", p) and re.search(r"[A-Z]", p) and re.search(r"\d", p))


def login_requerido(vista):
    @wraps(vista)
    def interior(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return vista(*args, **kwargs)
    return interior


def destino_seguro(destino):
    # solo rutas internas del propio sitio
    return destino.startswith("/") and not destino.startswith("//")


# ---------- Inicio de sesión y registro ----------
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("inicio"))
    if request.method == "POST":
        usuario = request.form["usuario"].strip()
        clave = request.form["password"]
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE username=%s OR email=%s OR telefono=%s",
                        (usuario, usuario, usuario))
            u = cur.fetchone()
        db.close()
        if u and check_password_hash(u["password_hash"], clave):
            session["user_id"], session["username"] = u["id"], u["username"]
            destino = request.args.get("next", "")
            return redirect(destino if destino_seguro(destino) else url_for("inicio"))
        flash("El usuario o la contraseña no son correctos.", "error")
    return render_template("login.html")


@app.route("/registro", methods=["GET", "POST"])
def registro():
    datos = request.form
    if request.method == "POST":
        username = datos["username"].strip()
        email = datos["email"].strip().lower() or None
        telefono = datos["telefono"].strip().replace(" ", "") or None
        clave = datos["password"]
        errores = []

        if not re.fullmatch(USER_RE, username):
            errores.append("El nombre de usuario debe tener de 3 a 30 letras, números, puntos o guiones bajos.")
        if not email and not telefono:
            errores.append("Añade un correo o un número de teléfono.")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errores.append("El correo no tiene un formato válido.")
        if telefono and not re.fullmatch(r"\+?\d{7,15}", telefono):
            errores.append("El número debe tener entre 7 y 15 dígitos.")
        if not password_valida(clave):
            errores.append("La contraseña necesita 8 caracteres, una mayúscula, una minúscula y un número.")
        try:
            nacimiento = datetime.strptime(datos["nacimiento"], "%Y-%m-%d").date()
            if calcular_edad(nacimiento) < EDAD_MINIMA:
                errores.append(f"Debes tener al menos {EDAD_MINIMA} años para crear una cuenta.")
        except ValueError:
            errores.append("Añade una fecha de nacimiento válida.")

        if not errores:
            db = get_db()
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO usuarios (username, email, telefono, password_hash, fecha_nacimiento) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (username, email, telefono, generate_password_hash(clave), nacimiento))
                db.commit()
                flash("Cuenta creada. Ya puedes iniciar sesión.", "ok")
                return redirect(url_for("login"))
            except pymysql.err.IntegrityError:
                errores.append("Ese usuario, correo o número ya está registrado.")
            finally:
                db.close()

        for e in errores:
            flash(e, "error")
    return render_template("registro.html", datos=datos, max_fecha=fecha_maxima().isoformat())


@app.route("/olvide", methods=["GET", "POST"])
def olvide():
    if request.method == "POST":
        # Pendiente: enviar el correo o SMS de recuperación
        flash("Si la cuenta existe, te enviaremos instrucciones para recuperarla.", "ok")
    return render_template("olvide.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


# ---------- Para ti (Feed Global) ----------
@app.route("/inicio")
@login_requerido
def inicio():
    user_id = session["user_id"]
    db = get_db()
    with db.cursor() as cur:
        # 1. Obtener publicaciones excluyendo usuarios bloqueados/silenciados
        cur.execute("""
            SELECT p.*, u.username, u.foto AS usuario_foto,
              (SELECT COUNT(*) FROM me_gusta WHERE publicacion_id = p.id) AS total_likes,
              (SELECT COUNT(*) FROM me_gusta WHERE publicacion_id = p.id AND usuario_id = %s) AS dio_like,
              (SELECT COUNT(*) FROM guardados WHERE publicacion_id = p.id AND usuario_id = %s) AS esta_guardado
            FROM publicaciones p
            JOIN usuarios u ON p.usuario_id = u.id
            WHERE p.usuario_id NOT IN (
                SELECT perfil_id FROM bloqueos_silencios WHERE usuario_id = %s
            )
            ORDER BY p.creado_en DESC
        """, (user_id, user_id, user_id))
        publicaciones = cur.fetchall()

        for p in publicaciones:
            cur.execute("""
                SELECT c.texto, c.creado_en, u.username 
                FROM comentarios c 
                JOIN usuarios u ON c.usuario_id = u.id 
                WHERE c.publicacion_id = %s 
                ORDER BY c.creado_en ASC
            """, (p["id"],))
            p["comentarios"] = cur.fetchall()

        # 2. Obtener los otros perfiles como recomendaciones
        cur.execute("""
            SELECT id, username, nombre, apellido, foto, nota_estado 
            FROM usuarios 
            WHERE id != %s AND id NOT IN (
                SELECT perfil_id FROM bloqueos_silencios WHERE usuario_id = %s AND tipo = 'bloqueo'
            )
            LIMIT 3
        """, (user_id, user_id))
        recomendados = cur.fetchall()

    db.close()
    return render_template("para_ti.html", publicaciones=publicaciones, recomendados=recomendados, seccion="inicio")
# ---------- Borrar Publicación ----------
@app.route("/borrar_publicacion/<int:pub_id>", methods=["POST"])
@login_requerido
def borrar_publicacion(pub_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT usuario_id, archivo_multimedia FROM publicaciones WHERE id=%s", (pub_id,))
        pub = cur.fetchone()
        
        # Verifica que la publicación exista y que el usuario actual sea el dueño
        if pub and pub["usuario_id"] == session["user_id"]:
            cur.execute("DELETE FROM publicaciones WHERE id=%s", (pub_id,))
            db.commit()
            
            # Borra el archivo físico del servidor para no ocupar espacio basura
            if pub["archivo_multimedia"]:
                try:
                    os.remove(os.path.join(CARPETA_FOTOS, pub["archivo_multimedia"]))
                except OSError:
                    pass
            flash("Stand borrado correctamente.", "ok")
        else:
            flash("No tienes permiso para borrar esta publicación.", "error")
            
    db.close()
    # Devuelve al usuario a la página donde estaba (perfil o inicio)
    return redirect(request.referrer or url_for("inicio"))

# ---------- Interacciones (Likes, Guardados, Comentarios) ----------
@app.route("/like/<int:pub_id>", methods=["POST"])
@login_requerido
def dar_like(pub_id):
    db = get_db()
    user_id = session["user_id"]
    with db.cursor() as cur:
        cur.execute("SELECT * FROM me_gusta WHERE usuario_id=%s AND publicacion_id=%s", (user_id, pub_id))
        existe = cur.fetchone()
        if existe:
            cur.execute("DELETE FROM me_gusta WHERE usuario_id=%s AND publicacion_id=%s", (user_id, pub_id))
            le_gusta = False
        else:
            cur.execute("INSERT INTO me_gusta (usuario_id, publicacion_id) VALUES (%s, %s)", (user_id, pub_id))
            le_gusta = True
        db.commit()
        
        cur.execute("SELECT COUNT(*) as total FROM me_gusta WHERE publicacion_id=%s", (pub_id,))
        total = cur.fetchone()["total"]
    db.close()
    return {"status": "ok", "le_gusta": le_gusta, "total": total}

@app.route("/guardar/<int:pub_id>", methods=["POST"])
@login_requerido
def guardar_stand(pub_id):
    db = get_db()
    user_id = session["user_id"]
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardados WHERE usuario_id=%s AND publicacion_id=%s", (user_id, pub_id))
        existe = cur.fetchone()
        if existe:
            cur.execute("DELETE FROM guardados WHERE usuario_id=%s AND publicacion_id=%s", (user_id, pub_id))
            guardado = False
        else:
            cur.execute("INSERT INTO guardados (usuario_id, publicacion_id) VALUES (%s, %s)", (user_id, pub_id))
            guardado = True
        db.commit()
    db.close()
    return {"status": "ok", "guardado": guardado}

@app.route("/comentar/<int:pub_id>", methods=["POST"])
@login_requerido
def comentar(pub_id):
    texto = request.form.get("texto", "").strip()
    if texto:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("INSERT INTO comentarios (usuario_id, publicacion_id, texto) VALUES (%s, %s, %s)",
                        (session["user_id"], pub_id, texto))
            db.commit()
        db.close()
    return redirect(request.referrer or url_for("inicio"))

# ---------- Editar Publicación ----------
@app.route("/editar_publicacion/<int:pub_id>", methods=["GET", "POST"])
@login_requerido
def editar_publicacion(pub_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM publicaciones WHERE id=%s AND usuario_id=%s", (pub_id, session["user_id"]))
        pub = cur.fetchone()
        if not pub:
            db.close()
            flash("No tienes permiso o la publicación no existe.", "error")
            return redirect(url_for("inicio"))

        if request.method == "POST":
            descripcion = request.form.get("descripcion", "").strip()
            ubicacion = request.form.get("ubicacion", "").strip()
            musica = request.form.get("musica", "").strip()
            cur.execute(
                "UPDATE publicaciones SET descripcion=%s, ubicacion=%s, musica=%s WHERE id=%s",
                (descripcion or None, ubicacion or None, musica or None, pub_id)
            )
            db.commit()
            db.close()
            flash("Stand actualizado correctamente.", "ok")
            return redirect(url_for("perfil"))

    db.close()
    return render_template("editar_publicacion.html", pub=pub)

# ---------- Perfil ----------
def mostrar_perfil(u):
    nombre = " ".join(x for x in (u["nombre"], u["apellido"]) if x) or "Anónimo"
    enlaces = [{"url": url, "texto": urlparse(url).netloc.removeprefix("www.")}
               for url in json.loads(u["enlaces"] or "[]")]
    es_mio = u["id"] == session["user_id"]
    
    # NUEVO: Extraer las publicaciones del usuario (de la más nueva a la más vieja)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM publicaciones WHERE usuario_id=%s ORDER BY creado_en DESC", (u["id"],))
        publicaciones = cur.fetchall()
    db.close()

    return render_template(
        "perfil.html", u=u, nombre_completo=nombre, enlaces=enlaces,
        sexo=SEXOS.get(u["sexo"]), es_mio=es_mio, seccion="perfil" if es_mio else "",
        url_perfil=url_for("perfil_publico", username=u["username"], _external=True),
        publicaciones=publicaciones
    )

# ---------- Perfil Propio ----------
@app.route("/perfil/<username>")
@app.route("/u/<username>")
@login_requerido
def perfil_publico(username):
    user_actual_id = session["user_id"]
    db = get_db()
    with db.cursor() as cur:
        # 1. Obtener usuario objetivo
        cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
        u = cur.fetchone()

        if not u:
            db.close()
            flash("Usuario no encontrado.", "error")
            return redirect(url_for("inicio"))

        es_mio = (u["id"] == user_actual_id)
        nombre = " ".join(x for x in (u.get("nombre"), u.get("apellido")) if x) or "Anónimo"
        
        # Manejo seguro de enlaces
        enlaces = []
        if u.get("enlaces"):
            try:
                raw_enlaces = json.loads(u["enlaces"])
                for url in raw_enlaces:
                    texto_url = url.replace("https://", "").replace("http://", "").replace("www.", "")
                    enlaces.append({"url": url, "texto": texto_url})
            except Exception:
                enlaces = []

        # 2. Contadores de Seguidores y Seguidos
        cur.execute("SELECT COUNT(*) AS total FROM seguidores WHERE seguido_id = %s", (u["id"],))
        res_seg = cur.fetchone()
        total_seguidores = res_seg["total"] if res_seg else 0

        cur.execute("SELECT COUNT(*) AS total FROM seguidores WHERE seguidor_id = %s", (u["id"],))
        res_sig = cur.fetchone()
        total_seguidos = res_sig["total"] if res_sig else 0

        # 3. Verificar si el usuario actual ya sigue a este perfil
        cur.execute("SELECT 1 FROM seguidores WHERE seguidor_id = %s AND seguido_id = %s", (user_actual_id, u["id"]))
        lo_sigo = bool(cur.fetchone())

        # 4. Cargar publicaciones con comentarios
        cur.execute("""
            SELECT p.*,
              (SELECT COUNT(*) FROM me_gusta WHERE publicacion_id = p.id) AS total_likes,
              (SELECT COUNT(*) FROM me_gusta WHERE publicacion_id = p.id AND usuario_id = %s) AS dio_like,
              (SELECT COUNT(*) FROM guardados WHERE publicacion_id = p.id AND usuario_id = %s) AS esta_guardado
            FROM publicaciones p
            WHERE p.usuario_id = %s
            ORDER BY p.creado_en DESC
        """, (user_actual_id, user_actual_id, u["id"]))
        publicaciones = cur.fetchall()

        for p in publicaciones:
            cur.execute("""
                SELECT c.texto, u.username 
                FROM comentarios c 
                JOIN usuarios u ON c.usuario_id = u.id 
                WHERE c.publicacion_id = %s 
                ORDER BY c.creado_en ASC
            """, (p["id"],))
            coms = cur.fetchall()
            p["comentarios_json"] = json.dumps(coms)

        # 5. NUEVA SECCIÓN: Cargar datos para el Modal de Configuración ⚙️ (Si es el propio perfil)
        cuentas_vinculadas = []
        publicaciones_guardadas = []
        calendario_fotos = []

        if es_mio:
            # 5.1. Cuentas vinculadas por el mismo correo
            cur.execute("SELECT id, username, foto FROM usuarios WHERE email = %s AND id != %s", (u["email"], u["id"]))
            cuentas_vinculadas = cur.fetchall()

            # 5.2. Publicaciones guardadas (Favoritos)
            cur.execute("""
                SELECT p.*, u.username FROM publicaciones p
                JOIN guardados g ON p.id = g.publicacion_id
                JOIN usuarios u ON p.usuario_id = u.id
                WHERE g.usuario_id = %s
                ORDER BY g.creado_en DESC
            """, (u["id"],))
            publicaciones_guardadas = cur.fetchall()

            # 5.3. Biblioteca / Calendario por fecha de publicación
            cur.execute("""
                SELECT id, archivo_multimedia, es_video, DATE_FORMAT(creado_en, '%%d/%%m/%%Y') AS fecha_formateada
                FROM publicaciones 
                WHERE usuario_id = %s
                ORDER BY creado_en DESC
            """, (u["id"],))
            calendario_fotos = cur.fetchall()

    db.close()

    # Mapeo seguro del sexo
    sexo_texto = u.get("sexo", "")
    if sexo_texto == "F": sexo_texto = "Femenino"
    elif sexo_texto == "M": sexo_texto = "Masculino"

    return render_template(
        "perfil.html", u=u, publicaciones=publicaciones, es_mio=es_mio,
        nombre_completo=nombre, enlaces=enlaces, sexo=sexo_texto,
        seccion="perfil" if es_mio else "",
        total_seguidores=total_seguidores, total_seguidos=total_seguidos, lo_sigo=lo_sigo,
        url_perfil=url_for("perfil_publico", username=u["username"], _external=True),
        cuentas_vinculadas=cuentas_vinculadas,
        publicaciones_guardadas=publicaciones_guardadas,
        calendario_fotos=calendario_fotos
    )

#-------------- PERFIL -----------------
# 1. RUTA /perfil (Soluciona el error BuildError de Flask)
@app.route("/perfil")
@login_requerido
def perfil():
    # Redirige automáticamente al perfil público del usuario en sesión
    return redirect(url_for("perfil_publico", username=session["username"]))


# 2. RUTA /perfil/editar ACTUALIZADA CON CLOUDINARY
@app.route("/perfil/editar", methods=["GET", "POST"])
@login_requerido
def editar_perfil():
    u = obtener_usuario("id", session["user_id"])
    if not u:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "GET":
        enl = json.loads(u["enlaces"] or "[]") + ["", "", ""]
        datos = {"username": u["username"], "nombre": u["nombre"] or "",
                 "apellido": u["apellido"] or "", "pronombres": u["pronombres"] or "",
                 "sexo": u["sexo"] or "", "enlace1": enl[0], "enlace2": enl[1], "enlace3": enl[2]}
        return render_template("editar_perfil.html", u=u, datos=datos, sexos=SEXOS, seccion="perfil")

    f = request.form
    username = f["username"].strip()
    nombre, apellido = f["nombre"].strip(), f["apellido"].strip()
    pronombres = f["pronombres"].strip()
    sexo = f["sexo"] if f["sexo"] in SEXOS else ""
    errores = []

    if not re.fullmatch(USER_RE, username):
        errores.append("El nombre de usuario debe tener de 3 a 30 letras, números, puntos o guiones bajos.")
    for etiqueta, valor in (("nombre", nombre), ("apellido", apellido)):
        if valor and not re.fullmatch(r"[^\W\d_]+(?:[ '\-][^\W\d_]+)*", valor):
            errores.append(f"El {etiqueta} solo puede tener letras.")
        if len(valor) > 40:
            errores.append(f"El {etiqueta} puede tener hasta 40 caracteres.")
    if len(pronombres) > 30:
        errores.append("Los pronombres pueden tener hasta 30 caracteres.")

    enlaces = []
    for i in (1, 2, 3):
        url = f.get(f"enlace{i}", "").strip()
        if url:
            if not re.match(r"https?://", url, re.I):
                url = "https://" + url
            p = urlparse(url)
            if p.scheme in ("http", "https") and "." in p.netloc and len(url) <= 200:
                enlaces.append(url)
            else:
                errores.append(f"El enlace {i} no es válido.")

    # Subida de la nueva foto a Cloudinary
    nueva_foto_url = None
    archivo = request.files.get("foto")
    if archivo and archivo.filename:
        try:
            # Subir directamente a Cloudinary
            upload_result = cloudinary.uploader.upload(
                archivo,
                folder="stands_uta/perfiles"
            )
            nueva_foto_url = upload_result.get("secure_url")
        except Exception as e:
            errores.append("Error al subir la imagen a la nube. Inténtalo de nuevo.")

    if not errores:
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE usuarios SET username=%s, nombre=%s, apellido=%s, pronombres=%s, "
                    "sexo=%s, enlaces=%s, foto=%s WHERE id=%s",
                    (username, nombre or None, apellido or None, pronombres or None, sexo or None,
                     json.dumps(enlaces), nueva_foto_url or u["foto"], u["id"]))
            db.commit()
            
            # Actualizar el username en sesión por si lo cambió
            session["username"] = username
            flash("Cambios guardados con éxito.", "ok")
            return redirect(url_for("perfil"))
        except pymysql.err.IntegrityError:
            errores.append("Ese nombre de usuario ya está en uso.")
        finally:
            db.close()

    for e in errores:
        flash(e, "error")
    return render_template("editar_perfil.html", u=u, datos=f, sexos=SEXOS, seccion="perfil")


@app.route("/qr/<username>.png")
def codigo_qr(username):
    if not re.fullmatch(USER_RE, username):
        abort(404)
    url = url_for("perfil_publico", username=username, _external=True)
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#2a0e0c", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


@app.errorhandler(413)
def foto_muy_pesada(e):
    flash("La foto pesa más de 3 MB. Elige una más ligera.", "error")
    return redirect(url_for("editar_perfil"))


if __name__ == "__main__":
    app.run(debug=True)

# ---------- Publicaciones (Stands) ----------
@app.route("/publicar", methods=["GET", "POST"])
@login_requerido
def publicar():
    if request.method == "POST":
        descripcion = request.form.get("descripcion", "").strip()
        ubicacion = request.form.get("ubicacion", "").strip()
        musica = request.form.get("musica", "").strip()
        archivo = request.files.get("multimedia")
        
        url_multimedia = None
        es_video = False
        errores = []

        if archivo and archivo.filename:
            ext = archivo.filename.rsplit('.', 1)[-1].lower()
            
            try:
                if ext in ['png', 'jpg', 'jpeg', 'webp']:
                    # Subir imagen a Cloudinary
                    upload_result = cloudinary.uploader.upload(
                        archivo,
                        folder="stands_uta/publicaciones"
                    )
                    url_multimedia = upload_result.get('secure_url')
                    
                elif ext in ['mp4', 'webm', 'mov']:
                    # Subir video a Cloudinary
                    upload_result = cloudinary.uploader.upload(
                        archivo,
                        resource_type="video",
                        folder="stands_uta/publicaciones"
                    )
                    url_multimedia = upload_result.get('secure_url')
                    es_video = True
                else:
                    errores.append("Formato no válido. Sube imágenes (JPG/PNG) o videos cortos (MP4).")
            except Exception as e:
                errores.append(f"Error al subir archivo a la nube: {str(e)}")

        if not errores and (url_multimedia or descripcion):
            db = get_db()
            try:
                with db.cursor() as cur:
                    # Guardamos `url_multimedia` (la URL pública de Cloudinary) en la columna `archivo_multimedia`
                    cur.execute(
                        "INSERT INTO publicaciones (usuario_id, descripcion, ubicacion, musica, archivo_multimedia, es_video) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (session["user_id"], descripcion or None, ubicacion or None, musica or None, url_multimedia, es_video)
                    )
                db.commit()
                flash("Publicación subida con éxito.", "ok")
                return redirect(url_for("perfil"))
            except Exception:
                errores.append("Error al guardar en la base de datos.")
            finally:
                db.close()
        elif not errores:
            errores.append("Debes añadir al menos una foto, un video o una descripción.")
        
        for e in errores:
            flash(e, "error")

    return render_template("publicar.html", seccion="publicar")

# ------- BUSCADOR DE PERFIL -------------------

@app.route("/buscar", methods=["GET"])
@login_requerido
def buscar_perfiles():
    query = request.args.get("q", "").strip()
    resultados = []
    if query:
        db = get_db()
        with db.cursor() as cur:
            # Buscar por username, nombre o apellido evitando el usuario actual
            cur.execute("""
                SELECT id, username, nombre, apellido, foto, nota_estado 
                FROM usuarios 
                WHERE (username LIKE %s OR nombre LIKE %s OR apellido LIKE %s)
                  AND id != %s
                LIMIT 10
            """, (f"%{query}%", f"%{query}%", f"%{query}%", session["user_id"]))
            resultados = cur.fetchall()
        db.close()
    return render_template("buscar.html", query=query, resultados=resultados, seccion="buscar")

# -------------- BLOQUEAR O SILENCIAR -------------------
@app.route("/accion_perfil/<int:perfil_id>/<tipo>", methods=["POST"])
@login_requerido
def accion_perfil(perfil_id, tipo):
    if tipo not in ["bloqueo", "silencio"]:
        abort(400)
    
    user_id = session["user_id"]
    if perfil_id == user_id:
        flash("No puedes realizar esta acción sobre tu propio perfil.", "error")
        return redirect(request.referrer or url_for("inicio"))

    db = get_db()
    with db.cursor() as cur:
        # Alternar la acción (si ya existe, la quita; si no, la agrega)
        cur.execute("SELECT * FROM bloqueos_silencios WHERE usuario_id=%s AND perfil_id=%s AND tipo=%s", 
                    (user_id, perfil_id, tipo))
        existe = cur.fetchone()
        
        if existe:
            cur.execute("DELETE FROM bloqueos_silencios WHERE usuario_id=%s AND perfil_id=%s AND tipo=%s", 
                        (user_id, perfil_id, tipo))
            flash(f"Se ha quitado el {tipo}.", "ok")
        else:
            cur.execute("INSERT INTO bloqueos_silencios (usuario_id, perfil_id, tipo) VALUES (%s, %s, %s)", 
                        (user_id, perfil_id, tipo))
            flash(f"Perfil {tipo}ado con éxito.", "ok")
        db.commit()
    db.close()
    return redirect(request.referrer or url_for("inicio"))

# ------------- ACTUALIZAR NOTA DE ESTADO CON EMOJI ------------
@app.route("/actualizar_nota", methods=["POST"])
@login_requerido
def actualizar_nota():
    nota = request.form.get("nota_estado", "").strip()
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE usuarios SET nota_estado=%s WHERE id=%s", (nota or None, session["user_id"]))
        db.commit()
    db.close()
    flash("Nota de estado actualizada.", "ok")
    return redirect(url_for("perfil"))


#------------ SEGUIR -----------------
@app.route("/seguir/<int:perfil_id>", methods=["POST"])
@login_requerido
def seguir_usuario(perfil_id):
    user_id = session["user_id"]
    if perfil_id == user_id:
        return {"status": "error", "mensaje": "No te puedes seguir a ti mismo"}

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM seguidores WHERE seguidor_id=%s AND seguido_id=%s", (user_id, perfil_id))
        existe = cur.fetchone()

        if existe:
            cur.execute("DELETE FROM seguidores WHERE seguidor_id=%s AND seguido_id=%s", (user_id, perfil_id))
            siguiendo = False
        else:
            cur.execute("INSERT INTO seguidores (seguidor_id, seguido_id) VALUES (%s, %s)", (user_id, perfil_id))
            siguiendo = True
        db.commit()

        # Obtener los nuevos contadores
        cur.execute("SELECT COUNT(*) AS total FROM seguidores WHERE seguido_id=%s", (perfil_id,))
        total_seguidores = cur.fetchone()["total"]

    db.close()
    return {"status": "ok", "siguiendo": siguiendo, "total_seguidores": total_seguidores}


# -------- CONTORLAR CAMBIO DE CUENTA Y LIMITE DE TIEMPO ------------------------


# 1. Ajustes / Guardar límite de tiempo
@app.route("/configuracion/tiempo", methods=["POST"])
@login_requerido
def configurar_tiempo():
    minutos = int(request.form.get("limite_tiempo", 0))
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE usuarios SET limite_tiempo=%s WHERE id=%s", (minutos, session["user_id"]))
        db.commit()
    db.close()
    flash("Límite de tiempo actualizado.", "ok")
    return redirect(url_for("perfil"))

# 2. Cambiar de cuenta (Gestor de Cuentas por mismo Email)
@app.route("/configuracion/cambiar_cuenta/<int:nuevo_user_id>", methods=["POST"])
@login_requerido
def cambiar_cuenta(nuevo_user_id):
    user_actual = obtener_usuario("id", session["user_id"])
    db = get_db()
    with db.cursor() as cur:
        # Verificar que el usuario destino tenga el mismo email
        cur.execute("SELECT id, username, email FROM usuarios WHERE id=%s AND email=%s", (nuevo_user_id, user_actual["email"]))
        cuenta_destino = cur.fetchone()
        
    db.close()
    if cuenta_destino:
        session["user_id"] = cuenta_destino["id"]
        session["username"] = cuenta_destino["username"]
        flash(f"Cambiado a la cuenta @{cuenta_destino['username']}", "ok")
    else:
        flash("No puedes cambiar a esta cuenta.", "error")
    return redirect(url_for("perfil"))

# 3. Middleware para controlar el tiempo de uso del sitio web
@app.before_request
def verificar_bloqueo_tiempo():
    if "user_id" in session and request.endpoint not in ["salir", "static"]:
        user_id = session["user_id"]
        
        # Verificar si hay bloqueo de 12 horas activo
        bloqueado_hasta = session.get("bloqueado_hasta")
        if bloqueado_hasta:
            hasta_dt = datetime.fromisoformat(bloqueado_hasta)
            if datetime.now() < hasta_dt:
                restante = hasta_dt - datetime.now()
                horas = int(restante.total_seconds() // 3600)
                minutos = int((restante.total_seconds() % 3600) // 60)
                return f"""
                <div style="text-align:center; padding: 3rem; font-family: sans-serif;">
                    <h1>⏳ Tiempo de uso agotado</h1>
                    <p>Has alcanzado el límite diario que configuraste en StandsUTA.</p>
                    <p>La plataforma permanecerá bloqueada por <strong>{horas}h {minutos}m</strong> más.</p>
                    <a href="/salir" style="color: #c00;">Cerrar sesión</a>
                </div>
                """, 403
            else:
                session.pop("bloqueado_hasta", None)
                session["tiempo_inicio"] = datetime.now().isoformat()

        # Iniciar cronómetro si no existe
        if "tiempo_inicio" not in session:
            session["tiempo_inicio"] = datetime.now().isoformat()

        # Verificar si superó su límite de tiempo
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT limite_tiempo FROM usuarios WHERE id=%s", (user_id,))
            res = cur.fetchone()
            limite = res["limite_tiempo"] if res else 0
        db.close()

        if limite > 0:
            inicio_dt = datetime.fromisoformat(session["tiempo_inicio"])
            transcurrido_minutos = (datetime.now() - inicio_dt).total_seconds() / 60
            if transcurrido_minutos >= limite:
                desbloqueo = (datetime.now() + timedelta(hours=12)).isoformat()
                session["bloqueado_hasta"] = desbloqueo
                return redirect(request.url)