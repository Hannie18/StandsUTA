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


# ---------- Para ti (Feed) ----------
@app.route("/inicio")
@login_requerido
def inicio():
    db = get_db()
    with db.cursor() as cur:
        # Extraemos todas las publicaciones y los datos del autor
        cur.execute("""
            SELECT p.*, u.username, u.foto AS usuario_foto 
            FROM publicaciones p 
            JOIN usuarios u ON p.usuario_id = u.id 
            ORDER BY p.creado_en DESC
        """)
        publicaciones = cur.fetchall()
    db.close()
    return render_template("para_ti.html", publicaciones=publicaciones, seccion="inicio")

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

@app.route("/perfil")
@login_requerido
def perfil():
    u = obtener_usuario("id", session["user_id"])
    if not u:
        session.clear()
        return redirect(url_for("login"))
    return mostrar_perfil(u)


@app.route("/u/<username>")
@login_requerido
def perfil_publico(username):
    u = obtener_usuario("username", username)
    if not u:
        abort(404)
    return mostrar_perfil(u)


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

    nueva_foto = None
    archivo = request.files.get("foto")
    if archivo and archivo.filename:
        try:
            img = Image.open(archivo.stream)
            formato = img.format
            img.verify()
            archivo.stream.seek(0)
            nueva_foto = f"{uuid4().hex}.{FORMATOS_FOTO[formato]}"
        except Exception:
            errores.append("La foto debe ser una imagen PNG, JPG o WEBP.")

    if not errores:
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE usuarios SET username=%s, nombre=%s, apellido=%s, pronombres=%s, "
                    "sexo=%s, enlaces=%s, foto=%s WHERE id=%s",
                    (username, nombre or None, apellido or None, pronombres or None, sexo or None,
                     json.dumps(enlaces), nueva_foto or u["foto"], u["id"]))
            db.commit()
            if nueva_foto:
                archivo.save(os.path.join(CARPETA_FOTOS, nueva_foto))
                if u["foto"]:
                    try:
                        os.remove(os.path.join(CARPETA_FOTOS, u["foto"]))
                    except OSError:
                        pass
            session["username"] = username
            flash("Cambios guardados.", "ok")
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
        
        nombre_archivo = None
        es_video = False
        errores = []

        if archivo and archivo.filename:
            ext = archivo.filename.rsplit('.', 1)[-1].lower()
            if ext in ['png', 'jpg', 'jpeg', 'webp']:
                nombre_archivo = f"{uuid4().hex}.{ext}"
            elif ext in ['mp4', 'webm', 'mov']:
                nombre_archivo = f"{uuid4().hex}.{ext}"
                es_video = True
            else:
                errores.append("Formato no válido. Sube imágenes (JPG/PNG) o videos cortos (MP4).")
            
            if not errores:
                archivo.save(os.path.join(CARPETA_FOTOS, nombre_archivo))

        if not errores and (nombre_archivo or descripcion):
            db = get_db()
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO publicaciones (usuario_id, descripcion, ubicacion, musica, archivo_multimedia, es_video) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (session["user_id"], descripcion or None, ubicacion or None, musica or None, nombre_archivo, es_video)
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