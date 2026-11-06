# Monitor de citas previas de la DGT

Script en Python para comprobar automáticamente si hay huecos libres para pedir cita previa en la [Sede Clave de la DGT](https://sedeclave.dgt.gob.es/WEB_CITE_CONSULTA/).

El script **reproduce exactamente el mismo recorrido que haría un navegador** simulando las peticiones AJAX de JavaServer Faces (JSF). De esta forma detecta cuando la web deja de mostrar el aviso *"No hay citas disponibles"* y, por tanto, **podría haber disponibilidad**.

## ¿Cómo funciona?

El script sigue estos pasos para llegar al calendario/trámites:

1. `GET inicio.faces` – Carga el formulario inicial para seleccionar jefatura/oficina.
2. `POST AJAX` (cambio de centro) – Al seleccionar el centro, la web repinta dinámicamente el selector de **área**.
3. `POST inicio.faces` (centro + área `CND`) – Envía centro y área **Conductores (CND)** para acceder al catálogo de trámites.
4. `POST catalogo.faces` (*"Pedir cita"*) – Pulsa el primer trámite de conductores. Si aparece un cuadro de diálogo con aviso de *"sin capacidad"* se considera **SIN_CITAS**. Si **no** aparece ese aviso, se considera **POSIBLE_DISPONIBILIDAD**.

## Estructura del proyecto

```text
DGTCitas/
├── dgt_citas.py                        # Script principal
├── README.md                           # Este archivo
└── .github/workflows/vigilar-citas-dgt.yml  # Workflow para GitHub Actions
```

## Requisitos

- **Python 3.10+** (probado con 3.12)
- Paquetes Python:

```bash
pip install requests beautifulsoup4
```

## Instalación

```bash
git clone https://github.com/tu-usuario/DGTCitas.git
cd DGTCitas
pip install -r requirements.txt  # o instala manualmente los paquetes
```

> *Nota:* El repositorio no incluye `requirements.txt`, pero basta con instalar `requests` y `beautifulsoup4`.

## Uso

### 1. Listar centros disponibles

Muestra todos los centros (jefaturas/oficinas) con su **ID numérico** para usar con `--centro-id`.

```bash
python dgt_citas.py --listar
```

Salida ejemplo:

```text
Campo: formselectorCentro:j_id_xx
  546  Jefatura Provincial de Barcelona
  545  Oficina de Tráfico de Sabadell
  530  ...
```

### 2. Comprobar un solo centro

Por **ID**:

```bash
python dgt_citas.py --centro-id 546
```

Por **nombre** (coincidencia parcial):

```bash
python dgt_citas.py --centro Sabadell
```

### 3. Comprobar varios centros a la vez

```bash
python dgt_citas.py --centro-id 546,545,530
```

```bash
python dgt_citas.py --centro Sabadell,Barcelona
```

### 4. Modo vigilancia (bucle continuo)

Repite la comprobación cada X minutos hasta que detecte **posible disponibilidad**. Se detiene automáticamente al encontrar hueco.

```bash
python dgt_citas.py --centro Sabadell --vigilar --cada 10
```

- `--cada` : minutos entre rondas. Mínimo 5 minutos (por defecto 10).
- Entre cada oficina se hace una pausa de **5 segundos** para no saturar la web.

### 5. Comprobación con "latido"

Envía un aviso informando que **se ha comprobado correctamente** pero **no hay citas**. Útil para saber que el script sigue funcionando.

```bash
python dgt_citas.py --centro-id 546 --latido
```

> **Importante:** El latido **se omite automáticamente** entre las **02:00 y 06:00** (hora de España, `Europe/Madrid`) para evitar notificaciones innecesarias por la noche.

### 6. Probar notificaciones

Envía un aviso de prueba por los canales configurados.

```bash
python dgt_citas.py --probar-aviso
```

### 7. Depurar / guardar HTML para diagnóstico

Si sospechas que la web ha cambiado su estructura, puedes ver qué campos se envían o guardar las respuestas.

```bash
python dgt_citas.py --centro-id 546 --depurar
python dgt_citas.py --centro-id 546 --guardar-error
```

Cuando hay **POSIBLE_DISPONIBILIDAD**, el script guarda automáticamente:

```text
resultado_<centro-id>.html
```

Cuando hay **ERROR** y se usa `--guardar-error`, guarda:

```text
error_<centro-id>.html
```

## Notificaciones

Si **no configuras ningún canal**, el script solo mostrará el resultado por pantalla. Para recibir avisos en móvil/correo, define las variables de entorno correspondientes.

### A) ntfy.sh (recomendado, push al móvil)

1. Instala la app [ntfy.sh](https://ntfy.sh/) en tu móvil
2. Elige un **topic** privado (ej. `dgt-citas-miusuario-abc123`)
3. Exporta la variable:

```bash
export NTFY_TOPIC="dgt-citas-tu-topic-secreto"
```

Al detectar **posible disponibilidad**, recibirás una notificación con enlace directo a `https://sedeclave.dgt.gob.es/WEB_CITE_CONSULTA/paginas/inicio.faces`

### B) Correo electrónico (SMTP)

Define estas variables:

| Variable | Descripción | Por defecto |
|---|---|---|
| `SMTP_HOST` | Servidor SMTP | (obligatorio) |
| `SMTP_PORT` | Puerto SMTP | `587` |
| `SMTP_USER` | Usuario/Remitente | (opcional) |
| `SMTP_PASS` | Contraseña/Token SMTP | (opcional) |
| `MAIL_TO` | Destinatario | (obligatorio) |

Ejemplo con Gmail/SMTP genérico:

```bash
export SMTP_HOST="smtp.tudominio.es"
export SMTP_PORT="587"
export SMTP_USER="tucorreo@tudominio.es"
export SMTP_PASS="tu-contraseña-app"
export MAIL_TO="destino@tudominio.es"
```

> Usa **contraseñas de aplicación** si tu proveedor lo requiere (Gmail, Outlook, etc.).

## Estados que devuelve

| Estado | Significado |
|---|---|
| `SIN_CITAS` | Se ha detectado el aviso típico *"No hay citas disponibles"* / *"sin capacidad"*. **No hay huecos aparentes**. |
| `POSIBLE_DISPONIBILIDAD` | **NO** se ha detectado ese aviso tras pulsar *"Pedir cita"*. Esto suele indicar que **hay citas libres** o que ha cambiado el mensaje. **Conviene entrar inmediatamente en la web para comprobarlo.** |
| `ERROR` | No se ha podido llegar al catálogo de trámites. Suele deberse a cambios en la web, red, cookies o estructura HTML. |

## Ejecución con GitHub Actions

El repositorio incluye un workflow para ejecutarlo automáticamente desde GitHub.

**Archivo:** `.github/workflows/vigilar-citas-dgt.yml`

### ¿Cuándo se ejecuta?

- **Manual (`workflow_dispatch`)**: Puedes lanzarlo desde la pestaña *Actions* → *Vigilar citas DGT* → *Run workflow*.
- **Con disparador externo**: El comentario `# lo lanza cron-job.org por API` indica que se puede invocar vía API/cron externo (p. ej. [cron-job.org](https://cron-job.org/)) para tener un horario fijo.

### Configuración

1. Ve a **Settings → Secrets and variables → Actions → Secrets** en tu repositorio.
2. Añade los secretos necesarios según tu canal de avisos:

| Secreto | Requerido | Uso |
|---|---|---|
| `NTFY_TOPIC` | Solo si usas ntfy | Tema privado de ntfy.sh |
| `SMTP_HOST` | Solo si usas email | Servidor SMTP |
| `SMTP_USER` | Opcional | Usuario SMTP |
| `SMTP_PASS` | Opcional | Contraseña/Token |
| `MAIL_TO` | Solo si usas email | Destinatario |

### Comportamiento del workflow

- **Instala dependencias**: `requests`, `beautifulsoup4`
- **Modo prueba**: Si marcas `probar_aviso = true`, ejecuta `--probar-aviso` y termina.
- **Modo normal**: Comprueba los centros **546** y **545** (`--centro-id 546,545`)
- **Latido automático**: Si el minuto actual (`TZ=Europe/Madrid`) es **< 10** y la hora es múltiplo de 8 (**00:00, 08:00 y 16:00**), añade `--latido` **solo si no se ha enviado ya en esa ventana**. El estado se guarda en la caché de Actions (`.latido_enviado`), así que aunque cron-job.org ejecute el workflow cada minuto, recibes **un único aviso cada 8 horas**.
- **Timeout**: 5 minutos máximo por ejecución.
- **Concurrencia**: `group: vigilar-citas-dgt` con `cancel-in-progress: false`. Evita lanzar dos ejecuciones simultáneas sin cancelar la que ya está corriendo.

> **Tip:** Si quieres vigilar otros centros, edita la línea `python dgt_citas.py --centro-id 546,545 $LATIDO` por tus propios IDs.

## Recomendaciones de uso responsable

- **No abuses del servidor**: Usa intervalos >= 10 minutos para vigilancia no crítica. El script ya respeta pausas internas.
- **Prioriza `--centro-id`**: Es más directo y evita búsquedas por nombre.
- **Usa GitHub Actions con criterio**: Aunque es cómodo, respeta los [términos de uso](https://sedeclave.dgt.gob.es/) de la Sede Clave de la DGT.
- **Actúa rápido**: Si recibes `POSIBLE_DISPONIBILIDAD`, **accede cuanto antes** con tu navegador y completa la reserva. Los huecos suelen desaparecer en cuestión de minutos.

## Diagnóstico rápido

| Síntoma | Solución |
|---|---|
| `No encuentro el formulario de selección de centro` | La web puede haber cambiado. Ejecuta con `--depurar` y guarda HTML para revisar. |
| `No encuentro el botón 'Pedir cita' en el catálogo` | Estructura JSF distinta. Usa `--depurar` y revisa `error_<id>.html`. |
| Muchos falsos `POSIBLE_DISPONIBILIDAD` | Raro, pero posible si cambian los diálogos. Revisa `resultado_<id>.html` para ver qué muestra realmente la web. |
| No encuentra centro por nombre | Usa `--listar` para obtener el ID exacto y luego `--centro-id`. |

## Licencia

Uso **personal y educativo**. El autor no se responsabiliza del uso que se le dé ni de posibles bloqueos derivados de un uso excesivo.

**Recomendado:** Ejecuta **únicamente** cuando realmente necesites encontrar cita. Usa con moderación.
