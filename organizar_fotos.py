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
except ImportError:
    raise SystemExit("Falta Pillow. Instálalo con: pip install -r requirements.txt")

MEDIA = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff",
         ".dng", ".raw", ".cr2", ".nef", ".arw",
         ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts", ".mp"}
# Vídeos cuya fecha se puede leer de sus metadatos (formato MP4/MOV)
VIDEO_ISO = {".mp4", ".mov", ".m4v", ".3gp"}
EPOCH_MP4 = 2082844800  # segundos entre 1904-01-01 (inicio de MP4/MOV) y 1970-01-01

# Takeout a veces añade ".supplemental-metadata" (o una versión recortada) antes de ".json"
SUFIJO_META = re.compile(r"\.supp?l?e?m?e?n?t?a?l?-?m?e?t?a?d?a?t?a?$", re.IGNORECASE)
# Duplicados: "IMG_1234.jpg(1).json" corresponde a "IMG_1234(1).jpg"
# Carpetas por año que crea Takeout ("Photos from 2016"): no son álbumes
CARPETA_ANIO = re.compile(r"^(photos from|fotos de|fotos del)\s+\d{4}$", re.IGNORECASE)
DUPLICADO = re.compile(r"^(.*?)(\.[^.]+)\((\d+)\)$")


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
    base = SUFIJO_META.sub("", base) if "." in base else base
    nombres = [base]
    m = DUPLICADO.match(base)
    if m:
        nombres.append(f"{m.group(1)}({m.group(3)}){m.group(2)}")
    if datos.get("title"):
        nombres.append(datos["title"])
    return nombres


def album_de(foto, origen):
    """Nombre de la subcarpeta de ORIGEN a la que pertenece la foto; None si está suelta."""
    partes = foto.relative_to(origen).parts
    if len(partes) < 2 or CARPETA_ANIO.match(partes[0]):
        return None
    return partes[0]


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


def main():
    p = argparse.ArgumentParser(description="Organiza fotos de Google Takeout por mes.")
    p.add_argument("origen", type=Path, help="Carpeta del Takeout (con fotos y .json)")
    p.add_argument("destino", type=Path, help="Carpeta donde crear AAAA/MM")
    p.add_argument("--ejecutar", action="store_true", help="Hacer los cambios (sin esto solo simula)")
    p.add_argument("--mover", action="store_true", help="Mover en lugar de copiar")
    args = p.parse_args()

    asignadas = {}  # foto -> fecha
    for json_path in args.origen.rglob("*.json"):
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
                asignadas[foto] = fecha
                # La versión editada comparte el .json del original
                editada = foto.with_name(f"{foto.stem}-editada{foto.suffix}")
                edited = foto.with_name(f"{foto.stem}-edited{foto.suffix}")
                for e in (editada, edited):
                    if e.is_file():
                        asignadas[e] = fecha
                break

    todas = [f for f in args.origen.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA]
    sin_json = [f for f in todas if f not in asignadas]

    # Sin .json: fecha de los metadatos (EXIF / vídeo); si tampoco hay, irán a "sin_fecha"
    n_json = len(asignadas)
    n_meta = 0
    sin_fecha = []
    for foto in sin_json:
        fecha = fecha_metadatos(foto)
        if fecha:
            asignadas[foto] = fecha
            n_meta += 1
        else:
            sin_fecha.append(foto)

    # Álbumes: cada subcarpeta de ORIGEN va entera al mes de su foto más antigua
    albumes = {}
    for foto in list(asignadas) + sin_fecha:
        nombre = album_de(foto, args.origen)
        if nombre:
            albumes.setdefault(nombre, []).append(foto)

    # Una foto suelta idéntica a una de un álbum se descarta: ya está en el álbum
    en_album = {f for fotos in albumes.values() for f in fotos}
    huellas = {}
    for f in en_album:
        huellas.setdefault(f.stat().st_size, set()).add(hash_archivo(f))
    duplicadas = []
    for foto in list(asignadas) + sin_fecha:
        tam = foto.stat().st_size
        if foto not in en_album and tam in huellas and hash_archivo(foto) in huellas[tam]:
            duplicadas.append(foto)
    for foto in duplicadas:
        asignadas.pop(foto, None)
        if foto in sin_fecha:
            sin_fecha.remove(foto)

    plan = []  # (foto, carpeta de destino, fecha o None)
    por_mes = Counter()
    for foto, fecha in sorted(asignadas.items(), key=lambda x: x[1]):
        if foto not in en_album:
            por_mes[f"{fecha:%Y-%m}"] += 1
            plan.append((foto, args.destino / f"{fecha:%Y}" / f"{fecha:%m}", fecha))
    for foto in sin_fecha:
        if foto not in en_album:
            plan.append((foto, args.destino / "sin_fecha", None))

    resumen_albumes = []
    for nombre, fotos in sorted(albumes.items()):
        fechas = [asignadas[f] for f in fotos if f in asignadas]
        if fechas:
            primera = min(fechas)
            base = args.destino / f"{primera:%Y}" / f"{primera:%m}" / nombre
            resumen_albumes.append(f"{primera:%Y-%m}  {nombre} ({len(fotos)} archivos)")
        else:
            base = args.destino / "sin_fecha" / nombre
            resumen_albumes.append(f"sin_fecha  {nombre} ({len(fotos)} archivos)")
        for foto in fotos:
            plan.append((foto, base, asignadas.get(foto)))

    accion = shutil.move if args.mover else shutil.copy2
    if args.ejecutar:
        for foto, carpeta, fecha in plan:
            carpeta.mkdir(parents=True, exist_ok=True)
            nuevo = destino_libre(carpeta / foto.name)
            accion(str(foto), str(nuevo))
            if fecha:
                ts = fecha.timestamp()
                os.utime(nuevo, (ts, ts))  # que la fecha del archivo sea la de captura
        if args.mover:
            for foto in duplicadas:
                foto.unlink()  # su copia idéntica ya está en el álbum

    print("\nFotos/vídeos por mes (fuera de álbumes):")
    for mes, n in sorted(por_mes.items()):
        print(f"  {mes}: {n}")
    if resumen_albumes:
        print("\nÁlbumes (van al mes de su foto más antigua):")
        for linea in resumen_albumes:
            print(f"  {linea}")
    print(f"\nTotal con fecha: {len(asignadas)}")
    print(f"  fecha del .json: {n_json}")
    print(f"  fecha de los metadatos, EXIF o vídeo (sin .json): {n_meta}")
    eliminadas = args.mover and args.ejecutar
    print(f"Repetidas de un álbum ({'eliminadas' if eliminadas else 'no se copian'}): {len(duplicadas)}")
    for f in duplicadas[:20]:
        print(f"   - {f.relative_to(args.origen)}")
    print(f"Sin .json ni metadatos con fecha (irán a 'sin_fecha'): {len(sin_fecha)}")
    for f in sin_fecha[:20]:
        print(f"   - {f.relative_to(args.origen)}")
    if len(sin_fecha) > 20:
        print(f"   ... y {len(sin_fecha) - 20} más")
    if not args.ejecutar:
        print("\n(Simulación: no se ha tocado nada. Añade --ejecutar para hacerlo de verdad.)")


if __name__ == "__main__":
    main()
