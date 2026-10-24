# Organizador de Google Fotos con Takeout.

Script en Python que ordena fotos y vídeos en carpetas por el mes en que se tomaron, con la estructura `AAAA/MM`. Está pensado para exportaciones de Google Fotos (Google Takeout), pero también funciona con carpetas de fotos normales.

```
Fotos ordenadas/
├── 2015/
│   ├── 09/
│   │   └── IMG_20150916_105525575.jpg
│   └── 10/
│       ├── 2015-10-04 03.42.56 1.jpg
│       └── ...
└── 2016/
    └── ...
```

## Requisitos

- Python 3.8 o superior.
- [Pillow](https://pypi.org/project/Pillow/), obligatorio: lee la fecha EXIF de las fotos sin `.json`.
- [pillow-heif](https://pypi.org/project/pillow-heif/), obligatorio: permite leer el EXIF de las fotos `.heic` / `.heif` del iPhone. Sin Pillow ni pillow-heif, el script no arranca.

Instalación en un entorno virtual (PowerShell, desde la carpeta del proyecto):

```
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Después, para ejecutar el script siempre con el entorno virtual, sin tener que activarlo:

```
.\.venv\Scripts\python.exe organizar_fotos.py "Fotos del 2015" "Fotos ordenadas"
```

## Uso

```
python organizar_fotos.py ORIGEN DESTINO [--ejecutar] [--mover]
```

| Argumento    | Descripción                                                          |
|--------------|----------------------------------------------------------------------|
| `ORIGEN`     | Carpeta con las fotos (y los `.json` de Takeout, si los hay).         |
| `DESTINO`    | Carpeta donde se creará la estructura `AAAA/MM`.                      |
| `--ejecutar` | Hace los cambios de verdad. Sin esta opción solo simula.              |
| `--mover`    | Mueve los archivos en lugar de copiarlos (solo con `--ejecutar`).     |

Ejemplos:

```
# 1. Simulación: muestra el resumen sin tocar nada
python organizar_fotos.py "Fotos del 2015" "Fotos ordenadas"

# 2. Copia las fotos a sus carpetas
python organizar_fotos.py "Fotos del 2015" "Fotos ordenadas" --ejecutar

# 3. Las mueve en lugar de copiarlas
python organizar_fotos.py "Fotos del 2015" "Fotos ordenadas" --ejecutar --mover
```

Se recomienda ejecutar siempre primero la simulación y revisar el resumen.

## Cómo funciona

1. **Busca los archivos multimedia** en `ORIGEN` y sus subcarpetas (fotos: jpg, png, heic, raw...; vídeos: mp4, mov, avi...).
2. **Asocia cada foto con su `.json`** de Takeout (por ejemplo `foto.jpg.supplemental-metadata.json`) y lee la fecha de captura (`photoTakenTime`, o `creationTime` si falta). También reconoce los nombres con duplicados, como `foto(1).jpg`, y las versiones editadas (`-editada` / `-edited`), que usan la fecha del original.
3. **Si una foto no tiene `.json`**, busca la fecha de captura en sus metadatos: el EXIF en las imágenes y la fecha de creación guardada dentro del archivo en los vídeos `.mp4`, `.mov`, `.m4v` y `.3gp`. Si tampoco la tiene, la manda a la carpeta `sin_fecha` para que la clasifiques a mano; la fecha de modificación del archivo no se usa porque no es fiable.
4. **Trata cada subcarpeta de `ORIGEN` como un álbum**: la guarda entera en `AAAA/MM/Nombre del álbum`, donde `AAAA/MM` es el mes de su foto más antigua. Las carpetas por año (`Photos from 2016`, `Fotos del 2017`) no cuentan como álbumes, pero sí los álbumes que haya dentro de ellas (`Fotos del 2017/11. Colación`).
5. **Descarta las repetidas**: si una foto suelta es idéntica (mismo contenido) a una de un álbum, no se copia, porque ya está en el álbum. Con `--mover` se elimina del origen.
6. **Crea la carpeta `AAAA/MM`** y copia o mueve ahí el archivo. Si ya existe uno con el mismo nombre, añade un sufijo (`_1`, `_2`...) para no sobrescribirlo.
7. **Ajusta la fecha del archivo** a la de captura.
8. **Muestra un resumen**: número de archivos por mes, los álbumes y su mes, las repetidas descartadas, cuántos se ordenaron por `.json` y por metadatos, y la lista de los que irán a `sin_fecha`.

## Limitaciones

- Los archivos sin `.json` ni metadatos con fecha (capturas, imágenes de WhatsApp...) quedan en `sin_fecha` y hay que clasificarlos a mano.
- Los vídeos `.avi`, `.mkv`, `.webm` y `.mts` no se pueden leer: sin `.json` van a `sin_fecha`.
- Algunos vídeos guardan la fecha en UTC, así que uno grabado a última hora del día puede caer en el mes siguiente.
- La fecha de los `.json` se interpreta en la zona horaria del equipo.
- Un álbum entero va a un solo mes, aunque sus fotos sean de varios.
- Solo se detectan duplicados entre una foto suelta y las de un álbum; dos fotos sueltas repetidas no se tocan.
