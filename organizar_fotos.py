"""
Organiza las fotos de un Google Takeout en carpetas por mes (AAAA/MM),
usando la fecha real de captura que viene en los archivos .json.
Cada subcarpeta de ORIGEN es un álbum: se guarda entera en el mes de su foto más antigua
(AAAA/MM/Álbum) y las fotos sueltas idénticas a las del álbum se descartan.

Uso:
    python organizar_fotos.py "E:/Takeout/Google Fotos" "E:/Fotos ordenadas"             # simulación (no toca nada)
    python organizar_fotos.py "E:/Takeout/Google Fotos" "E:/Fotos ordenadas" --ejecutar  # copia de verdad
    python organizar_fotos.py ... --ejecutar --mover                                     # mueve en vez de copiar
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image  # obligatorio para leer el EXIF
    from pillow_heif import register_heif_opener  # permite a Pillow abrir .heic / .heif (iPhone)
except ImportError:
    raise SystemExit("Faltan dependencias. Instálalas con: pip install -r requirements.txt")

register_heif_opener()

MEDIA = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
         ".dng", ".raw", ".cr2", ".nef", ".arw",
         ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts", ".mp"}
# Vídeos cuya fecha se puede leer de sus metadatos (formato MP4/MOV)
VIDEO_ISO = {".mp4", ".mov", ".m4v", ".3gp"}
EPOCH_MP4 = 2082844800  # segundos entre 1904-01-01 (inicio de MP4/MOV) y 1970-01-01

# Takeout a veces añade ".supplemental-metadata" (o una versión recortada) antes de ".json"
_META = r"\.supp?l?e?m?e?n?t?a?l?-?m?e?t?a?d?a?t?a?"
SUFIJO_META = re.compile(_META + r"$", re.IGNORECASE)
# En los duplicados el número va después: "IMG_1234.jpg.supplemental-metadata(1).json"
SUFIJO_META_NUM = re.compile(_META + r"(\(\d+\))$", re.IGNORECASE)
# Duplicados: "IMG_1234.jpg(1).json" corresponde a "IMG_1234(1).jpg"
DUPLICADO = re.compile(r"^(.*?)(\.[^.]+)\((\d+)\)$")
# Carpetas por año ("Photos from 2016", "Fotos del 2017"): no son álbumes, pero pueden contenerlos
CARPETA_ANIO = re.compile(r"^(photos from|fotos de|fotos del)\s+\d{4}$", re.IGNORECASE)
MAX_LISTADO = 20  # archivos que se listan en cada sección del resumen


def fecha_de_json(datos):
    for clave in ("photoTakenTime", "creationTime"):
        ts = datos.get(clave, {}).get("timestamp")
        if ts:
            return datetime.fromtimestamp(int(ts))
    return None


def fecha_exif(foto):
    """Fecha de captura de los datos EXIF; None si no hay."""
    try:
        with Image.open(foto) as img:
            exif = img.getexif()
            texto = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)  # DateTimeOriginal / DateTime
        if texto:
            return datetime.strptime(str(texto).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _buscar_mvhd(f, inicio, fin):
    """Recorre los bloques ('atoms') de un MP4/MOV hasta el 'mvhd' y devuelve su fecha de creación."""
    pos = inicio
    while pos + 8 <= fin:
        f.seek(pos)
        tam, tipo = struct.unpack(">I4s", f.read(8))
        cabecera = 8
        if tam == 1:  # tamaño de 64 bits
            tam = struct.unpack(">Q", f.read(8))[0]
            cabecera = 16
        elif tam == 0:  # hasta el final del archivo
            tam = fin - pos
        if tam < cabecera:
            return None
        if tipo == b"moov":
            return _buscar_mvhd(f, pos + cabecera, pos + tam)
        if tipo == b"mvhd":
            version = f.read(1)[0]
            f.read(3)  # flags
            segundos = struct.unpack(">Q" if version == 1 else ">I", f.read(8 if version == 1 else 4))[0]
            unix = segundos - EPOCH_MP4
            return datetime.fromtimestamp(unix) if unix > 0 else None  # 0 = sin fecha
        pos += tam
    return None


def fecha_video(video):
    """Fecha de creación guardada en los metadatos de un vídeo MP4/MOV; None si no hay."""
    try:
        with open(video, "rb") as f:
            return _buscar_mvhd(f, 0, os.fstat(f.fileno()).st_size)
    except (OSError, struct.error, IndexError, ValueError, OverflowError):
        return None


def fecha_metadatos(archivo):
    """Fecha de captura de los metadatos del propio archivo: EXIF en fotos, 'mvhd' en vídeos."""
    if archivo.suffix.lower() in VIDEO_ISO:
        return fecha_video(archivo)
    return fecha_exif(archivo)


def candidatos(json_path, datos):
    """Posibles nombres del archivo de foto/vídeo al que pertenece este .json."""
    base = json_path.name[:-len(".json")]
    m = SUFIJO_META_NUM.search(base)
    if m:
        base = base[:m.start()] + m.group(1)  # "x.jpg(1)", que DUPLICADO convierte en "x(1).jpg"
    elif "." in base:
        base = SUFIJO_META.sub("", base)
    nombres = [base]
    m = DUPLICADO.match(base)
    if m:
        nombres.append(f"{m.group(1)}({m.group(3)}){m.group(2)}")
    if datos.get("title"):
        nombres.append(datos["title"])
    return nombres


def album_de(foto, origen):
    """Álbum de la foto: la primera carpeta de su ruta que no sea una carpeta de año; None si está suelta."""
    for parte in foto.relative_to(origen).parts[:-1]:
        if not CARPETA_ANIO.match(parte):
            return parte
    return None


def hash_archivo(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.digest()


def destino_libre(path):
    if not path.exists():
        return path
    n = 1
    while True:
        alt = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not alt.exists():
            return alt
        n += 1


def leer_argumentos():
    p = argparse.ArgumentParser(description="Organiza fotos de Google Takeout por mes.")
    p.add_argument("origen", type=Path, help="Carpeta del Takeout (con fotos y .json)")
    p.add_argument("destino", type=Path, help="Carpeta donde crear AAAA/MM")
    p.add_argument("--ejecutar", action="store_true", help="Hacer los cambios (sin esto solo simula)")
    p.add_argument("--mover", action="store_true", help="Mover en lugar de copiar")
    args = p.parse_args()
    if not args.origen.is_dir():
        raise SystemExit(f"No existe la carpeta de origen: {args.origen}")
    return args


def fechas_de_los_json(origen):
    """Asocia cada foto con la fecha y el .json que le corresponden: (fotos -> fecha, fotos -> .json)."""
    fechas, jsons = {}, {}
    for json_path in origen.rglob("*.json"):
        try:
            datos = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(datos, dict):
            continue
        fecha = fecha_de_json(datos)
        if not fecha:
            continue
        for nombre in candidatos(json_path, datos):
            foto = json_path.parent / nombre
            if foto.is_file() and foto.suffix.lower() in MEDIA:
                # La versión editada comparte el .json del original
                editadas = (foto.with_name(f"{foto.stem}{sufijo}{foto.suffix}") for sufijo in ("-editada", "-edited"))
                for archivo in (foto, *(e for e in editadas if e.is_file())):
                    fechas[archivo] = fecha
                    jsons[archivo] = json_path
                break
    return fechas, jsons


def fechas_de_metadatos(origen, fechas):
    """Añade a `fechas` la de los archivos sin .json (EXIF / vídeo); devuelve los que siguen sin fecha."""
    sin_fecha = []
    for foto in origen.rglob("*"):
        if not foto.is_file() or foto.suffix.lower() not in MEDIA or foto in fechas:
            continue
        fecha = fecha_metadatos(foto)
        if fecha:
            fechas[foto] = fecha
        else:
            sin_fecha.append(foto)
    return sin_fecha


def agrupar_albumes(fotos, origen):
    """Fotos de cada álbum: {nombre del álbum: [fotos]}."""
    albumes = {}
    for foto in fotos:
        nombre = album_de(foto, origen)
        if nombre:
            albumes.setdefault(nombre, []).append(foto)
    return albumes


def buscar_repetidas(fotos, albumes):
    """Fotos sueltas idénticas (mismo contenido) a una de un álbum: ya están en el álbum."""
    en_album = {f for fotos_album in albumes.values() for f in fotos_album}
    huellas = {}  # tamaño -> contenidos; solo se calcula el hash si coincide el tamaño
    for f in en_album:
        huellas.setdefault(f.stat().st_size, set()).add(hash_archivo(f))
    repetidas = []
    for foto in fotos:
        tam = foto.stat().st_size
        if foto not in en_album and tam in huellas and hash_archivo(foto) in huellas[tam]:
            repetidas.append(foto)
    return repetidas


def planificar(destino, fechas, sin_fecha, albumes):
    """Qué va a dónde: (plan [(foto, carpeta, fecha o None)], fotos por mes, resumen de álbumes)."""
    en_album = {f for fotos in albumes.values() for f in fotos}
    plan = []
    por_mes = Counter()
    for foto, fecha in sorted(fechas.items(), key=lambda x: x[1]):
        if foto not in en_album:
            por_mes[f"{fecha:%Y-%m}"] += 1
            plan.append((foto, destino / f"{fecha:%Y}" / f"{fecha:%m}", fecha))
    for foto in sin_fecha:
        if foto not in en_album:
            plan.append((foto, destino / "sin_fecha", None))

    resumen_albumes = []
    for nombre, fotos in sorted(albumes.items()):
        fechas_album = [fechas[f] for f in fotos if f in fechas]
        if fechas_album:
            primera = min(fechas_album)  # el álbum entero va al mes de su foto más antigua
            carpeta = destino / f"{primera:%Y}" / f"{primera:%m}" / nombre
            resumen_albumes.append(f"{primera:%Y-%m}  {nombre} ({len(fotos)} archivos)")
        else:
            carpeta = destino / "sin_fecha" / nombre
            resumen_albumes.append(f"sin_fecha  {nombre} ({len(fotos)} archivos)")
        plan.extend((foto, carpeta, fechas.get(foto)) for foto in fotos)
    return plan, por_mes, resumen_albumes


def calcular_limpieza(origen, plan, repetidas, jsons):
    """Con --mover el origen se vacía: (.json de las fotos movidas, .json que sobran, carpetas vacías)."""
    desaparecen = {f for f, _, _ in plan} | set(repetidas)
    borrar_jsons = {jsons[f] for f in desaparecen if f in jsons}
    desaparecen |= borrar_jsons
    sobrantes = sorted(j for j in origen.rglob("*.json") if j not in borrar_jsons)
    carpetas = sorted((d for d in origen.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True)
    vacias = [d for d in carpetas if all(f in desaparecen for f in d.rglob("*") if f.is_file())]
    return borrar_jsons, sobrantes, vacias


def aplicar(mover, plan, repetidas, borrar_jsons, vacias):
    accion = shutil.move if mover else shutil.copy2
    for foto, carpeta, fecha in plan:
        carpeta.mkdir(parents=True, exist_ok=True)
        nuevo = destino_libre(carpeta / foto.name)
        accion(str(foto), str(nuevo))
        if fecha:
            ts = fecha.timestamp()
            os.utime(nuevo, (ts, ts))  # que la fecha del archivo sea la de captura
    if mover:
        for foto in repetidas:
            foto.unlink()  # su copia idéntica ya está en el álbum
        for j in borrar_jsons:
            j.unlink(missing_ok=True)
        for d in vacias:  # de la más profunda a la menos
            try:
                d.rmdir()
            except OSError:
                pass  # no estaba vacía


def listar(archivos, origen):
    for f in archivos[:MAX_LISTADO]:
        print(f"   - {f.relative_to(origen)}")
    if len(archivos) > MAX_LISTADO:
        print(f"   ... y {len(archivos) - MAX_LISTADO} más")


def main():
    args = leer_argumentos()
    origen = args.origen

    fechas, jsons = fechas_de_los_json(origen)
    n_json = len(fechas)
    sin_fecha = fechas_de_metadatos(origen, fechas)  # irán a "sin_fecha" si tampoco tienen metadatos
    n_meta = len(fechas) - n_json

    albumes = agrupar_albumes([*fechas, *sin_fecha], origen)
    repetidas = buscar_repetidas([*fechas, *sin_fecha], albumes)
    for foto in repetidas:
        fechas.pop(foto, None)
    descartadas = set(repetidas)
    sin_fecha = [f for f in sin_fecha if f not in descartadas]

    plan, por_mes, resumen_albumes = planificar(args.destino, fechas, sin_fecha, albumes)
    borrar_jsons, sobrantes, vacias = calcular_limpieza(origen, plan, repetidas, jsons) if args.mover else (set(), [], [])
    if args.ejecutar:
        aplicar(args.mover, plan, repetidas, borrar_jsons, vacias)

    eliminadas = args.mover and args.ejecutar
    print("\nFotos/vídeos por mes (fuera de álbumes):")
    for mes, n in sorted(por_mes.items()):
        print(f"  {mes}: {n}")
    if resumen_albumes:
        print("\nÁlbumes (van al mes de su foto más antigua):")
        for linea in resumen_albumes:
            print(f"  {linea}")
    print(f"\nTotal con fecha: {len(fechas)}")
    print(f"  fecha del .json: {n_json}")
    print(f"  fecha de los metadatos, EXIF o vídeo (sin .json): {n_meta}")
    print(f"Repetidas de un álbum ({'eliminadas' if eliminadas else 'no se copian'}): {len(repetidas)}")
    listar(repetidas, origen)
    print(f"Sin .json ni metadatos con fecha (irán a 'sin_fecha'): {len(sin_fecha)}")
    listar(sin_fecha, origen)
    if args.mover:
        print(f"\n.json de las fotos movidas ({'eliminados' if eliminadas else 'se eliminarían'}): {len(borrar_jsons)}")
        print(f"Carpetas vacías del origen ({'eliminadas' if eliminadas else 'se eliminarían'}): {len(vacias)}")
        print(f".json que se quedan (no pertenecen a ninguna foto): {len(sobrantes)}")
        listar(sobrantes, origen)
    if not args.ejecutar:
        print("\n(Simulación: no se ha tocado nada. Añade --ejecutar para hacerlo de verdad.)")


if __name__ == "__main__":
    main()
