# Map-matching local con GraphHopper (TrackCam)

Motor de *map-matching*: encaja una traza GPS real en la red viaria de
OpenStreetMap, de forma que la línea dibujada sigue las calles (los giros se
reconstruyen por la calzada y desaparecen los imposibles).

Reutiliza el grafo **ya construido** de
`/home/alvaro/Escritorio/proyectos/eurocams/routing/` (no se reconstruye nada).

---

## 1. Arrancar el servidor

```bash
cd /home/alvaro/Escritorio/proyectos/trackcam
./tools/arrancar-mapmatching.sh --daemon      # segundo plano (recomendado)
./tools/arrancar-mapmatching.sh               # primer plano (Ctrl+C para parar)
./tools/arrancar-mapmatching.sh --status
./tools/arrancar-mapmatching.sh --stop
```

* Puerto por defecto **8098**; si estuviera ocupado el script coge el siguiente
  libre (nunca 8080, 8099 ni 8100). El puerto admin es el de la app + 1000.
* No reconstruye el grafo: si `graph-cache/` no existiera, **aborta** con un
  mensaje claro en vez de lanzar horas de importación (para forzarlo:
  `--reconstruir`).
* Genera su propio config en `tools/runtime/mapmatching-config.yml` a partir del
  config original (solo cambia rutas a absolutas y puertos). El `config.yml` de
  eurocams no se toca.
* PID y log en `tools/runtime/` (en `.gitignore`).

Estado actual: **en marcha** en `http://127.0.0.1:8098` (admin 9098), pid en
`tools/runtime/graphhopper.pid`.

## 2. Endpoint de map-matching

| | |
|---|---|
| URL | `POST http://127.0.0.1:8098/match` |
| Cuerpo | **GPX** (`Content-Type: application/gpx+xml`) ← el que funciona |
| Query | `profile=car` (obligatorio), `points_encoded=false`, `gps_accuracy=80` |
| Salida | `paths[0].points.coordinates` (geometría pegada a las calles) y `map_matching` |

`/match` **no necesita ninguna opción en `config.yml`**: se registra siempre que
el módulo `graphhopper-map-matching` esté en el jar (lo está). Comprobado: `GET
/match` devuelve `405` con `Allow: POST,OPTIONS`, y `POST` con GPX devuelve `200`.

> El endpoint exige `POST` y **rechaza `Content-Type: application/json` con 415**.
> Hay que mandar el GPX tal cual (o un GPX JSON, pero no una lista de puntos).

## 3. Evidencia real (traza de 46 km del usuario 1)

Traza: usuario 1, 13-09-2026 13:40–14:20 **hora local (CEST)** = 11:40–12:20 UTC,
1167 puntos cada ~2 s, 46,07 km por Bizkaia (Bilbao → hacia Durango/Gernika).

> ⚠️ La columna `ts` de `tracks.db` está en epoch **UTC**, pero el usuario ve
> hora local. Las 13:40–14:20 UTC son *otro* tramo distinto: 40 min prácticamente
> parado (3,6 km). Ojo con la zona horaria al exportar.

Comando exacto y salida real:

```bash
cd /home/alvaro/Escritorio/proyectos/trackcam/tools
curl -s -X POST "http://127.0.0.1:8098/match?profile=car&points_encoded=false&gps_accuracy=80" \
     -H 'Content-Type: application/gpx+xml' \
     --data-binary @traza-2026-09-13.gpx -o match-response.json \
     -w 'HTTP %{http_code}  %{size_download} bytes  %{time_total}s\n'
# HTTP 200  31871 bytes  9.105124s
```

Extracto de la respuesta (`match-response.json`):

```json
{"hints":{},
 "info":{"copyrights":["GraphHopper","OpenStreetMap contributors"],"took":8922,
         "road_data_timestamp":"2026-08-14T20:21:03Z"},
 "paths":[{"distance":47570.731,"time":2655236,"points_encoded":false,
           "bbox":[-3.025686,43.223446,-2.682493,43.316479],
           "points":{"type":"LineString","coordinates":[[-3.017217,43.316432],
                     [-3.016814,43.316323],[-3.017381,43.316479], ...]}}],
 "map_matching":{"original_distance":46069.46761356823,
                 "distance":47570.73078023184,"time":2655236}}
```

Comprobaciones hechas con `verificar-mapmatching.py`:

| Comprobación | Resultado |
|---|---|
| HTTP / tamaño | **200**, 31.871 bytes |
| Puntos de entrada | **1167** |
| Vértices de la geometría devuelta | **1113** |
| Puntos de entrada a ≤ 25 m de la geometría | **1056/1167 (90,5 %)**; peor caso 106,6 m |
| Distancia matcheada vs. cruda | **47,57 km vs 46,07 km → ratio 1,03** |
| Vértices devueltos sobre la red (`/nearest`) | min 0,00 m — mediana **0,04 m** — max 0,07 m |
| (control) puntos GPS crudos → red | mediana 3,4 m, max 41,5 m |

Que los vértices estén a ~4 cm de la red demuestra que la línea devuelta va
**por las calles**, no por los puntos GPS crudos.

## 4. Limitaciones encontradas (importante)

1. **`gps_accuracy` es imprescindible en trazas reales.** Con el valor por
   defecto el matching se dispara: devuelve una línea en zigzag de 835 km (18×)
   con 23.500 vértices para una traza de 46 km. Medido sobre esta misma traza:

   | gps_accuracy | distancia devuelta | ratio |
   |---|---|---|
   | por defecto | 835,65 km | 18,1× |
   | 15 | 506,00 km | 11,0× |
   | 50 | 79,26 km | 1,72× |
   | **60** | **48,70 km** | **1,06×** |
   | **80** | **47,57 km** | **1,03×** |
   | **90–120** | **45,5 km** | **0,99×** |

   Usar **`gps_accuracy=80`** (rango sano 60–120). Partir la traza en trozos de
   ~120 puntos y comprobar el ratio (< 1,15) es una buena red de seguridad.

2. **El motor y el grafo están bien**: con una traza sintética perfecta (la
   geometría de una ruta `/route`, 194 puntos, 42 km) el matching sale con
   ratio 1,01 **incluso con gps_accuracy=15**. La sensibilidad al parámetro es
   culpa del ruido de la traza real (la mediana de `acc` es 4 m, pero hay 12
   puntos con `acc` > 100 m y saltos de posición de hasta 85 m), no del grafo ni
   del jar. Filtrar por `acc` o quitar duplicados **no** arregla el problema por
   sí solo: hay que subir `gps_accuracy`.

3. **La cobertura del grafo es toda España** (bbox del `/info`:
   `-16.24,28.14 → 11.78,52.25`), no solo Bizkaia. Fuera de esa caja no habría
   matching.

4. **El `config.yml` de eurocams no se podía parsear** con el jar actual
   (GraphHopper 11.0): el bloque `server:` estaba indentado dentro de
   `graphhopper:` y en formato lista, lo que da
   `Cannot deserialize DefaultServerFactory from Array value`. Lo he corregido
   (copia de seguridad en `config.yml.bak-20260914-151553`) al formato Dropwizard
   con `application_connectors`/`admin_connectors` en la raíz, manteniendo su
   puerto 8989.

5. **Puerto 8098**: durante la sesión estuvo ocupado por un mock
   (`python3 /tmp/mm_mock.py 8098`, un simulador de `/match` para probar
   `backend/mapmatch.py`). El script detecta los puertos ocupados y salta al
   siguiente libre. **Ojo**: si algo apunta a 8098 mientras ese mock corre,
   recibirá respuestas falsas; comprobar siempre `pid`/`/info` antes de fiarse.

## 5. Ficheros de esta carpeta

| Fichero | Para qué |
|---|---|
| `arrancar-mapmatching.sh` | arrancar/parar el servidor (esto es lo que se usa) |
| `exportar-traza-gpx.py` | saca una traza de `data/tracks.db` a GPX (solo lectura, con `--tz`) |
| `verificar-mapmatching.py` | manda un GPX a `/match` y da las cuentas + prueba `/nearest` |
| `traza-2026-09-13.gpx` | traza real de 46 km usada como prueba |
| `match-response.json` | respuesta real de GraphHopper (evidencia) |
| `match-payload.json` | la misma traza en JSON (para quien prefiera ese formato) |
| `runtime/` | config generado, pid y log (ignorado por git) |
