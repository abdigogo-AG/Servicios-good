import os
import logging
import random
import string
import shutil
import psycopg2
import psycopg2.extras
import bcrypt
import re
import mercadopago
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles 
from contextlib import asynccontextmanager
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import date, datetime, timedelta
from fastapi.middleware.cors import CORSMiddleware

# ==========================================
# 1. CONFIGURACIÓN
# ==========================================
log = logging.getLogger("uvicorn")
POSTGRES_URL = os.environ.get("POSTGRES_URL")
db_connections = {}

# Leer Token
mp_token = os.environ.get("MP_ACCESS_TOKEN")

# URL del frontend (usada en las back_urls de Mercado Pago)
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8080")

# Verificación de Seguridad
if not mp_token:
    print("\n⚠️  ADVERTENCIA: No se encontró MP_ACCESS_TOKEN en las variables de entorno.")
    print("⚠️  La API arrancará, pero los pagos fallarán.\n")
    sdk = None # Evita que explote al inicio
else:
    try:
        sdk = mercadopago.SDK(mp_token)
        print("✅ Mercado Pago SDK inicializado correctamente.")
    except Exception as e:
        print(f"❌ Error al iniciar Mercado Pago: {e}")
        sdk = None


UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# ==========================================
# 2. MODELOS DE DATOS (Pydantic)
# ==========================================

# --- AUTH & REGISTRO ---
class RegistroCliente(BaseModel):
    nombre: str
    apellidos: str
    correo_electronico: EmailStr
    password: str
    telefono: str
    fecha_nacimiento: date
    calle: str
    colonia: str
    numero_exterior: str
    numero_interior: Optional[str] = None
    codigo_postal: str
    ciudad: str
    referencias: Optional[str] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None

    # Modelo de datos que recibimos del frontend
class SolicitudPago(BaseModel):
    servicio_id: str
    titulo: str
    precio: float
    trabajador_id: str
    propuesta_id: str

class RegistroTrabajador(BaseModel):
    nombre: str
    apellidos: str
    correo_electronico: EmailStr
    password: str
    telefono: str
    fecha_nacimiento: date
    descripcion_bio: str
    anios_experiencia: int
    tarifa_hora: float
    oficios_ids: List[int]
    latitud: Optional[float] = None
    longitud: Optional[float] = None

class DatosVerificacion(BaseModel):
    correo: EmailStr
    codigo: str

class LoginRequest(BaseModel):
    correo: EmailStr
    password: str

# --- PERFILES ---
# --- MODELO LIMPIO (Sin dirección) ---
class PerfilTrabajadorUpdate(BaseModel):
    nombre: str
    apellidos: str
    telefono: str
    # Datos Profesionales
    descripcion_bio: str
    anios_experiencia: int
    tarifa_hora: float
    # Docs y Fotos
    foto_perfil_url: Optional[str] = None
    foto_ine_frente_url: Optional[str] = None
    foto_ine_reverso_url: Optional[str] = None
    antecedentes_penales_url: Optional[str] = None

class PerfilClienteUpdate(BaseModel):
    nombre: str
    apellidos: str
    telefono: str
    calle: str
    colonia: str
    codigo_postal: str
    ciudad: str
    foto_perfil_url: Optional[str] = None
    password_nuevo: Optional[str] = None

# --- SERVICIOS Y PROPUESTAS ---
class CrearServicio(BaseModel):
    cliente_id: str
    categoria_id: int
    titulo: str
    descripcion: str
    fecha_programada: Optional[datetime] = None
    precio_estimado: Optional[float] = None
    direccion_texto: str
    latitud: float
    longitud: float
    foto_evidencia_url: Optional[str] = None

class CrearPropuesta(BaseModel):
    servicio_id: str
    trabajador_id: str
    precio_oferta: float
    mensaje: str

class AceptarPropuesta(BaseModel):
    servicio_id: str
    trabajador_id: str
    propuesta_id: str

class CalificarServicio(BaseModel):
    servicio_id: str
    calificacion: int # 1 a 5
    resena: str

# --- ADMIN ---
class AccionAdmin(BaseModel):
    usuario_id: str
    accion: str
    dias_bloqueo: Optional[int] = 0

    # --- NUEVO MODELO PAGO ---
class SolicitudPago(BaseModel):
    titulo: str
    precio: float
    servicio_id: str
    propuesta_id: str
    trabajador_id: str

# ==========================================
# 3. HELPERS
# ==========================================
def encriptar_password(password_plana: str) -> str:
    password_bytes = password_plana[:72].encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')

def generar_codigo_verificacion():
    return ''.join(random.choices(string.digits, k=6))

def verificar_password(password_plana: str, password_hash: str) -> bool:
    password_bytes = password_plana[:72].encode('utf-8')
    hash_bytes = password_hash.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

# ==========================================
# 4. LIFESPAN & APP
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Iniciando API...")
    try:
        pg_conn = psycopg2.connect(POSTGRES_URL, cursor_factory=psycopg2.extras.DictCursor)
        db_connections["pg_conn"] = pg_conn
        
        # Admin por defecto
        with pg_conn.cursor() as cur:
            pass_admin = encriptar_password("admin123")
            cur.execute("""
                INSERT INTO usuarios (nombre, apellidos, correo_electronico, password_hash, telefono, es_admin, activo, fecha_nacimiento)
                VALUES ('Super', 'Admin', 'admin@sistema.com', %s, '000', TRUE, TRUE, '2000-01-01')
                ON CONFLICT (correo_electronico) DO NOTHING
            """, (pass_admin,))
            pg_conn.commit()
        log.info("✅ Postgres Conectado.")
    except Exception as e:
        if 'pg_conn' in locals() and pg_conn: pg_conn.rollback()
        log.error(f"❌ Error al iniciar Postgres: {e}")
    yield
    if db_connections.get("pg_conn"):
        db_connections["pg_conn"].close()

app = FastAPI(lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ==========================================
# 5. ENDPOINTS: GENERAL & AUTH
# ==========================================

@app.get("/")
def read_root(): return {"mensaje": "API ISF Funcionando"}


# --- UPLOAD CORREGIDO (Sanitizar Nombres) ---
@app.post("/upload")
async def subir_imagen(file: UploadFile = File(...)):
    try:
        # 1. Limpiar nombre: reemplazar espacios y caracteres raros por guion bajo
        nombre_limpio = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
        
        # 2. Crear nombre único
        nombre_archivo = f"{generar_codigo_verificacion()}_{nombre_limpio}"
        
        # 3. Guardar
        ruta_guardado = os.path.join(UPLOAD_DIR, nombre_archivo)
        with open(ruta_guardado, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # 4. Devolver URL válida
        url_publica = f"http://localhost:8080/uploads/{nombre_archivo}"
        return {"url": url_publica}
        
    except Exception as e:
        log.error(f"Error subiendo: {e}")
        raise HTTPException(500, "Error subiendo imagen")
    
    

@app.get("/categorias")
def obtener_categorias():
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, nombre, icono_url FROM categorias_oficios")
            return [dict(cat) for cat in cursor.fetchall()]
    except Exception: raise HTTPException(500, "Error")

@app.post("/registro-cliente")
def registrar_cliente(datos: RegistroCliente):
    conn = db_connections.get("pg_conn")
    if conn is None: raise HTTPException(503, "Sin BD")
    try:
        with conn.cursor() as cursor:
            hashed_pass = encriptar_password(datos.password)
            codigo = generar_codigo_verificacion()
            cursor.execute("INSERT INTO usuarios (nombre, apellidos, correo_electronico, password_hash, telefono, fecha_nacimiento, activo, codigo_verificacion) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id", (datos.nombre, datos.apellidos, datos.correo_electronico, hashed_pass, datos.telefono, datos.fecha_nacimiento, codigo))
            nuevo_id = cursor.fetchone()['id']
            cursor.execute("INSERT INTO detalles_cliente (usuario_id, calle, colonia, numero_exterior, numero_interior, codigo_postal, ciudad, referencias_domicilio, latitud, longitud) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (nuevo_id, datos.calle, datos.colonia, datos.numero_exterior, datos.numero_interior, datos.codigo_postal, datos.ciudad, datos.referencias, datos.latitud, datos.longitud))
            conn.commit()
            print(f"\n===  CLIENTE: {datos.correo_electronico} | : {codigo} ===\n")
            return {"mensaje": "Cliente registrado.", "correo": datos.correo_electronico}
    except psycopg2.IntegrityError: conn.rollback(); raise HTTPException(400, "Correo ya registrado.")
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, f"Error: {str(e)}")

@app.post("/registro-trabajador")
def registrar_trabajador(datos: RegistroTrabajador):
    conn = db_connections.get("pg_conn")
    if conn is None: raise HTTPException(503, "Sin BD")
    try:
        with conn.cursor() as cursor:
            hashed_pass = encriptar_password(datos.password)
            codigo = generar_codigo_verificacion()
            cursor.execute("INSERT INTO usuarios (nombre, apellidos, correo_electronico, password_hash, telefono, fecha_nacimiento, activo, codigo_verificacion) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s) RETURNING id", (datos.nombre, datos.apellidos, datos.correo_electronico, hashed_pass, datos.telefono, datos.fecha_nacimiento, codigo))
            nuevo_id = cursor.fetchone()['id']
            cursor.execute("INSERT INTO detalles_trabajador (usuario_id, descripcion_bio, anios_experiencia, tarifa_hora_estimada, latitud, longitud) VALUES (%s, %s, %s, %s, %s, %s)", (nuevo_id, datos.descripcion_bio, datos.anios_experiencia, datos.tarifa_hora, datos.latitud, datos.longitud))
            if datos.oficios_ids:
                for oficio_id in datos.oficios_ids:
                    cursor.execute("INSERT INTO trabajador_oficios (usuario_id, categoria_id) VALUES (%s, %s)", (nuevo_id, oficio_id))
            conn.commit()
            print(f"\n=== 📧 TRABAJADOR: {datos.correo_electronico} | 🔑: {codigo} ===\n")
            return {"mensaje": "Trabajador registrado.", "correo": datos.correo_electronico}
    except psycopg2.IntegrityError: conn.rollback(); raise HTTPException(400, "Correo ya registrado.")
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, f"Error interno")

# ... (El inicio del archivo sigue igual) ...

# --- ENDPOINT PAGO CORREGIDO ---
@app.post("/pagos/crear-preferencia") # <--- 1. CORREGIDO EL NOMBRE
def crear_preferencia_pago(datos: SolicitudPago):
    if sdk is None:
        raise HTTPException(500, "Error: Mercado Pago no configurado.")

    print(f"💰 Creando preferencia para: {datos.titulo} - ${datos.precio}")

    # 1. Configuración de la preferencia
    preference_data = {
        "items": [
            {
                "id": datos.servicio_id,
                "title": datos.titulo,
                "quantity": 1,
                "currency_id": "MXN",
                "unit_price": float(datos.precio)
            }
        ],
        # 2. CORREGIDO: USAR VARIABLE FRONTEND_URL (No 127.0.0.1 fijo)
        "back_urls": {
            "success": f"{FRONTEND_URL}/frontend/dashboard.html",
            "failure": f"{FRONTEND_URL}/frontend/dashboard.html",
            "pending": f"{FRONTEND_URL}/frontend/dashboard.html"
        },
        "auto_return": "approved",
        "external_reference": f"{datos.servicio_id}|{datos.propuesta_id}|{datos.trabajador_id}"
    }

    try:
        # 3. Crear la preferencia
        preference_response = sdk.preference().create(preference_data)

        # Logs para ver qué pasa: imprimir la respuesta completa de MP
        print("\n--- RESPUESTA MP RAW ---")
        print(preference_response)

        # Extraer código de estado de la respuesta de la forma más robusta posible
        status_code = None
        if isinstance(preference_response, dict):
            status_code = preference_response.get("status")
            response_data = preference_response.get("response", {})
        else:
            # Fallback si la SDK devolviera un objeto distinto
            status_code = getattr(preference_response, 'status', None)
            response_data = getattr(preference_response, 'response', {}) or {}

        if status_code == 201:
            # Usamos sandbox_init_point si existe (para pruebas), si no el normal
            link_pago = response_data.get("sandbox_init_point", response_data.get("init_point"))

            # Devolver 'init_point' (lo que tu HTML espera)
            return {
                "preference_id": response_data.get("id"), 
                "init_point": link_pago 
            }
        else:
            # Incluir la respuesta completa en el log y en el error para diagnóstico
            print(f"MP Error al crear preferencia: {preference_response}")
            raise HTTPException(400, f"MP Error: {preference_response}")

    except Exception as e:
        if isinstance(e, HTTPException): raise e
        print(f"Error interno MP: {e}")
        raise HTTPException(500, "Error procesando el pago.")

# ... (El resto del archivo sigue igual) ...
    
@app.post("/verificar-cuenta")
def verificar_cuenta(datos: DatosVerificacion):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, codigo_verificacion, activo FROM usuarios WHERE correo_electronico = %s", (datos.correo,))
            u = cursor.fetchone()
            if not u: raise HTTPException(404, "Usuario no encontrado.")
            if u['activo']: return {"mensaje": "Cuenta ya activa."}
            if u['codigo_verificacion'] == datos.codigo:
                cursor.execute("UPDATE usuarios SET activo = TRUE WHERE id = %s", (u['id'],))
                conn.commit()
                return {"mensaje": "¡Cuenta activada!"}
            else: raise HTTPException(400, "Código incorrecto.")
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error interno.")

@app.post("/login")
def login(datos: LoginRequest):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, nombre, password_hash, activo, es_admin, bloqueado_hasta FROM usuarios WHERE correo_electronico = %s", (datos.correo,))
            u = cursor.fetchone()
            if not u or not u['activo'] or not verificar_password(datos.password, u['password_hash']): 
                raise HTTPException(401, "Credenciales incorrectas o inactiva.")
            
            if u['bloqueado_hasta']:
                bloqueo = u['bloqueado_hasta'].replace(tzinfo=None) if u['bloqueado_hasta'].tzinfo else u['bloqueado_hasta']
                if bloqueo > datetime.now(): raise HTTPException(403, "Cuenta bloqueada.")
            
            es_trabajador = False
            cursor.execute("SELECT 1 FROM detalles_trabajador WHERE usuario_id = %s", (u['id'],))
            if cursor.fetchone(): es_trabajador = True

            return {"mensaje": "Login exitoso", "usuario": {"id": str(u['id']), "nombre": u['nombre'], "es_admin": u['es_admin'], "es_trabajador": es_trabajador}}
    except Exception as e: log.error(e); raise HTTPException(500, "Error interno")

# ==========================================
# 6. ENDPOINTS: PERFILES
# ==========================================


# --- ENDPOINT GET (Sin pedir dirección) ---
@app.get("/mi-perfil/{usuario_id}")
def obtener_perfil_trabajador(usuario_id: str):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            # Solo traemos lo que existe en tu BD
            cursor.execute("""
                SELECT u.nombre, u.apellidos, u.telefono, u.foto_perfil_url,
                       dt.descripcion_bio, dt.anios_experiencia, dt.tarifa_hora_estimada, 
                       dt.calificacion_promedio, dt.total_evaluaciones, dt.validado_por_admin,
                       dt.foto_ine_frente_url, dt.foto_ine_reverso_url, dt.antecedentes_penales_url
                FROM usuarios u
                JOIN detalles_trabajador dt ON u.id = dt.usuario_id
                WHERE u.id = %s
            """, (usuario_id,))
            perfil = cursor.fetchone()
            if not perfil: raise HTTPException(404, "Perfil no encontrado")
            return dict(perfil)
    except Exception as e: log.error(e); raise HTTPException(500, "Error interno")


# --- ENDPOINT PUT (Sin actualizar dirección) ---
@app.put("/mi-perfil/{usuario_id}")
def actualizar_perfil_trabajador(usuario_id: str, datos: PerfilTrabajadorUpdate):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            # 1. Actualizar tabla usuarios (Nombre, Teléfono, Foto Perfil)
            cursor.execute("""
                UPDATE usuarios 
                SET nombre=%s, apellidos=%s, telefono=%s, foto_perfil_url=%s 
                WHERE id=%s
            """, (datos.nombre, datos.apellidos, datos.telefono, datos.foto_perfil_url, usuario_id))
            
            # 2. Actualizar tabla detalles_trabajador (Bio, Experiencia, Tarifa, Docs)
            cursor.execute("""
                UPDATE detalles_trabajador SET 
                    descripcion_bio=%s, anios_experiencia=%s, tarifa_hora_estimada=%s,
                    foto_ine_frente_url=%s, foto_ine_reverso_url=%s, antecedentes_penales_url=%s
                WHERE usuario_id=%s
            """, (
                datos.descripcion_bio, datos.anios_experiencia, datos.tarifa_hora,
                datos.foto_ine_frente_url, datos.foto_ine_reverso_url, datos.antecedentes_penales_url, 
                usuario_id
            ))
            conn.commit()
            return {"mensaje": "Perfil actualizado correctamente"}
    except Exception as e: 
        conn.rollback()
        log.error(e)
        raise HTTPException(500, "Error al actualizar perfil")
    
@app.get("/mi-perfil-cliente/{usuario_id}")
def get_perfil_cliente(usuario_id: str):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.nombre, u.apellidos, u.telefono, u.correo_electronico, u.foto_perfil_url, u.fecha_nacimiento,
                       dc.calle, dc.colonia, dc.codigo_postal, dc.ciudad, 
                       dc.numero_exterior, dc.numero_interior, dc.referencias_domicilio,
                       dc.latitud, dc.longitud
                FROM usuarios u
                JOIN detalles_cliente dc ON u.id = dc.usuario_id
                WHERE u.id = %s
            """, (usuario_id,))
            p = cur.fetchone()
            if not p: raise HTTPException(404, "Perfil no encontrado")
            return dict(p)
    except Exception as e: log.error(e); raise HTTPException(500, "Error")

@app.put("/mi-perfil-cliente/{usuario_id}")
def update_perfil_cliente(usuario_id: str, d: PerfilClienteUpdate):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cur:
            if d.password_nuevo:
                h = encriptar_password(d.password_nuevo)
                cur.execute("UPDATE usuarios SET nombre=%s, apellidos=%s, telefono=%s, correo_electronico=%s, foto_perfil_url=%s, password_hash=%s WHERE id=%s", (d.nombre, d.apellidos, d.telefono, d.correo_electronico, d.foto_perfil_url, h, usuario_id))
            else:
                cur.execute("UPDATE usuarios SET nombre=%s, apellidos=%s, telefono=%s, correo_electronico=%s, foto_perfil_url=%s WHERE id=%s", (d.nombre, d.apellidos, d.telefono, d.correo_electronico, d.foto_perfil_url, usuario_id))

            cur.execute("""
                UPDATE detalles_cliente 
                SET calle=%s, colonia=%s, codigo_postal=%s, ciudad=%s, numero_exterior=%s, numero_interior=%s, referencias_domicilio=%s, latitud=%s, longitud=%s
                WHERE usuario_id=%s
            """, (d.calle, d.colonia, d.codigo_postal, d.ciudad, d.numero_exterior, d.numero_interior, d.referencias, d.latitud, d.longitud, usuario_id))
            conn.commit()
            return {"mensaje": "Perfil actualizado"}
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error update")

# ==========================================
# 7. ENDPOINTS: SERVICIOS Y PROPUESTAS
# ==========================================

@app.post("/servicios")
def crear_servicio(datos: CrearServicio):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO servicios (cliente_id, categoria_id, titulo, descripcion, fecha_programada, precio_estimado, direccion_texto, latitud, longitud, foto_evidencia_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (datos.cliente_id, datos.categoria_id, datos.titulo, datos.descripcion, datos.fecha_programada, datos.precio_estimado, datos.direccion_texto, datos.latitud, datos.longitud, datos.foto_evidencia_url))
            nid = cursor.fetchone()['id']
            conn.commit()
            return {"mensaje": "Solicitud creada", "servicio_id": str(nid)}
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error crear servicio")

@app.get("/servicios/{usuario_id}")
def listar_servicios_cliente(usuario_id: str):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.titulo, s.estado, s.fecha_solicitud, c.nombre as categoria,
                       (SELECT COUNT(*) FROM propuestas p WHERE p.servicio_id = s.id) as num_propuestas
                FROM servicios s
                JOIN categorias_oficios c ON s.categoria_id = c.id
                WHERE s.cliente_id = %s
                ORDER BY s.fecha_solicitud DESC
            """, (usuario_id,))
            res = []
            for s in cur.fetchall():
                d = dict(s)
                d['id'] = str(d['id'])
                d['fecha_solicitud'] = str(d['fecha_solicitud'])
                res.append(d)
            return res
    except Exception as e: log.error(e); raise HTTPException(500, "Error servicios")

@app.get("/feed-servicios")
def feed_servicios():
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.titulo, s.descripcion, s.precio_estimado, s.fecha_programada, s.direccion_texto, s.foto_evidencia_url,
                       c.nombre as categoria, u.nombre as cliente_nombre
                FROM servicios s
                JOIN categorias_oficios c ON s.categoria_id = c.id
                JOIN usuarios u ON s.cliente_id = u.id
                WHERE s.estado = 'SOLICITADO'
                ORDER BY s.fecha_solicitud DESC LIMIT 20
            """)
            servicios = cursor.fetchall()
            res = []
            for s in servicios:
                d = dict(s)
                d['id'] = str(d['id'])
                d['fecha_programada'] = str(d['fecha_programada'])
                res.append(d)
            return res
    except Exception as e: log.error(e); raise HTTPException(500, "Error feed")

@app.post("/propuestas")
def crear_propuesta(datos: CrearPropuesta):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM propuestas WHERE servicio_id = %s AND trabajador_id = %s", (datos.servicio_id, datos.trabajador_id))
            if cursor.fetchone(): raise HTTPException(400, "Ya te has postulado.")
            cursor.execute("INSERT INTO propuestas (servicio_id, trabajador_id, precio_oferta, mensaje) VALUES (%s, %s, %s, %s)", (datos.servicio_id, datos.trabajador_id, datos.precio_oferta, datos.mensaje))
            conn.commit()
            return {"mensaje": "Propuesta enviada"}
    except HTTPException as e: raise e
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error propuesta")

@app.get("/servicios/{servicio_id}/propuestas")
def ver_propuestas(servicio_id: str):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cur:
            # TRAEMOS DATOS COMPLETOS DEL TRABAJADOR
            cur.execute("""
                SELECT p.id, p.precio_oferta, p.mensaje, p.trabajador_id,
                       u.nombre, u.apellidos, u.foto_perfil_url, u.telefono,
                       dt.calificacion_promedio, dt.total_evaluaciones,
                       dt.anios_experiencia, dt.descripcion_bio
                FROM propuestas p
                JOIN usuarios u ON p.trabajador_id = u.id
                JOIN detalles_trabajador dt ON u.id = dt.usuario_id
                WHERE p.servicio_id = %s
                ORDER BY p.precio_oferta ASC
            """, (servicio_id,))
            
            # Convertimos a lista de diccionarios
            resultados = []
            for row in cur.fetchall():
                d = dict(row)
                d['id'] = str(d['id'])
                d['trabajador_id'] = str(d['trabajador_id'])
                # Convertimos decimales a float para que JS no falle
                if d['calificacion_promedio']: d['calificacion_promedio'] = float(d['calificacion_promedio'])
                if d['precio_oferta']: d['precio_oferta'] = float(d['precio_oferta'])
                resultados.append(d)
            return resultados

    except Exception as e: 
        log.error(e)
        raise HTTPException(500, "Error cargando propuestas")

@app.post("/servicios/contratar")
def contratar_trabajador(datos: AceptarPropuesta):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE servicios SET trabajador_id = %s, estado = 'EN_PROCESO', precio_estimado = (SELECT precio_oferta FROM propuestas WHERE id = %s) WHERE id = %s", (datos.trabajador_id, datos.propuesta_id, datos.servicio_id))
            cursor.execute("UPDATE propuestas SET aceptada = TRUE WHERE id = %s", (datos.propuesta_id,))
            conn.commit()
            return {"mensaje": "¡Contratado!"}
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error contratar")

@app.get("/trabajador/mis-trabajos/{trabajador_id}")
def mis_trabajos_trabajador(trabajador_id: str):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            # CORRECCIÓN: Agregamos s.calificacion y s.resena
            cursor.execute("""
                SELECT s.id, s.titulo, s.descripcion, s.estado, s.fecha_solicitud, s.direccion_texto, 
                       s.precio_estimado, s.calificacion, s.resena,
                       u.nombre as cliente_nombre, u.telefono as cliente_telefono
                FROM servicios s
                JOIN usuarios u ON s.cliente_id = u.id
                WHERE s.trabajador_id = %s
                ORDER BY s.fecha_solicitud DESC
            """, (trabajador_id,))
            return [dict(s, id=str(s['id']), fecha_solicitud=str(s['fecha_solicitud'])) for s in cursor.fetchall()]
    except Exception as e: log.error(e); raise HTTPException(500, "Error")

@app.post("/servicios/finalizar")
def finalizar_servicio(datos: CalificarServicio):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE servicios SET estado = 'TERMINADO', calificacion = %s, resena = %s WHERE id = %s RETURNING trabajador_id", (datos.calificacion, datos.resena, datos.servicio_id))
            res = cursor.fetchone()
            if not res: raise HTTPException(404, "Servicio no encontrado")
            tid = res['trabajador_id']
            cursor.execute("SELECT AVG(calificacion) as pro, COUNT(*) as tot FROM servicios WHERE trabajador_id = %s AND calificacion IS NOT NULL", (tid,))
            stats = cursor.fetchone()
            cursor.execute("UPDATE detalles_trabajador SET calificacion_promedio = %s, total_evaluaciones = %s WHERE usuario_id = %s", (float(stats['pro'] or 0), int(stats['tot']), tid))
            conn.commit()
            return {"mensaje": "Finalizado y calificado"}
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, "Error finalizar")

# ==========================================
# 8. ADMIN
# ==========================================
@app.get("/admin/usuarios")
def admin_listar_usuarios():
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            cursor.execute("""SELECT u.id, u.nombre, u.apellidos, u.correo_electronico, u.activo, u.bloqueado_hasta, CASE WHEN dt.usuario_id IS NOT NULL THEN 'Trabajador' WHEN dc.usuario_id IS NOT NULL THEN 'Cliente' WHEN u.es_admin THEN 'Admin' ELSE 'Desconocido' END as rol, dt.validado_por_admin FROM usuarios u LEFT JOIN detalles_trabajador dt ON u.id = dt.usuario_id LEFT JOIN detalles_cliente dc ON u.id = dc.usuario_id ORDER BY u.fecha_registro DESC""")
            return [dict(u, id=str(u['id']), bloqueado_hasta=str(u['bloqueado_hasta']) if u['bloqueado_hasta'] else None) for u in cursor.fetchall()]
    except Exception as e: log.error(e); raise HTTPException(500, "Error listando")

@app.post("/admin/accion")
def admin_accion_usuario(datos: AccionAdmin):
    conn = db_connections.get("pg_conn")
    try:
        with conn.cursor() as cursor:
            if datos.accion == "validar": cursor.execute("UPDATE detalles_trabajador SET validado_por_admin = TRUE WHERE usuario_id = %s", (datos.usuario_id,))
            elif datos.accion == "bloquear":
                dias = datos.dias_bloqueo if datos.dias_bloqueo else 36500
                fecha_fin = datetime.now() + timedelta(days=dias)
                cursor.execute("UPDATE usuarios SET bloqueado_hasta = %s WHERE id = %s", (fecha_fin, datos.usuario_id))
            elif datos.accion == "desbloquear": cursor.execute("UPDATE usuarios SET bloqueado_hasta = NULL WHERE id = %s", (datos.usuario_id,))
            elif datos.accion == "borrar": cursor.execute("DELETE FROM usuarios WHERE id = %s", (datos.usuario_id,))
            conn.commit()
            return {"mensaje": f"Acción '{datos.accion}' ejecutada."}
    except Exception as e: conn.rollback(); log.error(e); raise HTTPException(500, f"Error: {str(e)}")