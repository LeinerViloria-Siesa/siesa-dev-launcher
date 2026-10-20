#!/usr/bin/env bash
# Levanta (o reinicia) todos los backends/frontends .NET + Vite de business-financiero-*
# bajo Services/, apuntando al túnel de Cloud SQL dev (business-dev.sh) ya levantado.
#
# Requisito previo: el túnel debe estar arriba (puerto local 5434). Si no lo está,
# este script NO lo levanta solo (modifica Master Authorized Networks de un cluster
# GKE compartido y crea un pod efímero — eso lo corres tú a propósito):
#   source ${TUNNEL_SCRIPT}
#
# Uso:
#   ./start-all-services.sh                          # levanta lo que esté caído (no toca lo que ya corre), los 5 servicios
#   ./start-all-services.sh accounting               # levanta solo accounting (back y front) si están caídos
#   ./start-all-services.sh accounting --reload      # reinicia accounting: back y front
#   ./start-all-services.sh accounting --reload --backend    # reinicia solo el backend de accounting
#   ./start-all-services.sh accounting --reload --frontend   # reinicia solo el frontend de accounting
#   ./start-all-services.sh --reload                 # reinicia los 5 servicios
#   ./start-all-services.sh --stop                   # baja TODO (backends + frontends) y ya
#   ./start-all-services.sh accounting --stop        # baja solo accounting (admite --backend / --frontend)
#   ./start-all-services.sh --status                 # muestra qué puertos están arriba
#
# --backend / --frontend filtran QUÉ lado se toca; sin ninguno de los dos, se tocan ambos.
# Los nombres de servicio filtran CUÁLES; sin nombres, son todos.
#
# Logs: ~/siesa-dev-logs/<servicio>-{backend,frontend}.log

set -uo pipefail

SERVICES_ROOT="${SERVICES_ROOT:?define SERVICES_ROOT (carpeta con los repos business-financiero-*)}"
LOG_DIR="${HOME}/siesa-dev-logs"
TUNNEL_PORT=5434
TUNNEL_SCRIPT="${TUNNEL_SCRIPT:-business-dev.sh}"

mkdir -p "$LOG_DIR"

# name | repo dir | backend csproj (relative) | backend port | frontend extra env (optional)
# Frontend siempre vive en src/frontend, puerto = backend port + 1000 (convención confirmada
# en los 5 repos), arrancado con `npx vite --force`.
SERVICES_DEFS=(
  "third-party|business-financiero-third-party-service|src/backend/ThirdPartyService.API/ThirdPartyService.API.csproj|7016|"
  "accounting|business-financiero-accounting-service|src/backend/AccountingService.API/AccountingService.API.csproj|7018|"
  "liquid-tax|business-financiero-liquid-tax-service|src/backend/LiquidTaxService.API/LiquidTaxService.API.csproj|7020|"
  "base-config|business-financiero-base-config|src/backend/BaseConfiguration.API/BaseConfiguration.API.csproj|7014|BUILD_MODE=standalone"
  "segments|business-financiero-segments-service|src/backend/SegmentsService.API/SegmentsService.API.csproj|7012|"
)

color() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info()  { echo "$(color 36 "[info]") $*"; }
ok()    { echo "$(color 32 "[ok]")   $*"; }
warn()  { echo "$(color 33 "[warn]") $*"; }
err()   { echo "$(color 31 "[err]")  $*"; }

check_tunnel() {
  if timeout 2 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/${TUNNEL_PORT}" 2>/dev/null; then
    ok "Túnel a Cloud SQL activo en 127.0.0.1:${TUNNEL_PORT}"
    return 0
  fi
  err "No hay túnel activo en 127.0.0.1:${TUNNEL_PORT}."
  echo "       Levántalo primero en OTRA terminal (tiene efectos en infra compartida, no lo hace este script):"
  echo "         source ${TUNNEL_SCRIPT}"
  return 1
}

port_pid() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti ":${port}" 2>/dev/null
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1
  fi
}

stop_port() {
  local port="$1" label="$2"
  local pid
  pid="$(port_pid "$port")"
  if [ -n "${pid:-}" ]; then
    info "Deteniendo ${label} (puerto ${port}, pid ${pid})"
    kill -TERM $pid 2>/dev/null
    sleep 1
  fi
}

start_backend() {
  local name="$1" repo="$2" csproj="$3" port="$4"
  local dir="${SERVICES_ROOT}/${repo}"
  if [ ! -f "${dir}/${csproj}" ]; then
    err "${name}: no encuentro ${dir}/${csproj}, me lo salto"
    return
  fi
  stop_port "$port" "${name} backend"
  # Un `dotnet run` que aún COMPILA no escucha en el puerto, así que stop_port no lo ve. Si se deja vivo, el nuevo
  # compila el mismo proyecto a la vez (se pisan bin/obj) y el servicio no levanta. Se mata por su csproj.
  if pgrep -f "dotnet run --project ${csproj}" >/dev/null 2>&1; then
    info "Deteniendo ${name} backend que seguía compilando/arrancando"
    pkill -TERM -f "dotnet run --project ${csproj}" 2>/dev/null
    sleep 2
  fi
  info "Arrancando backend ${name} en :${port}"
  (
    cd "$dir" || exit 1
    ASPNETCORE_ENVIRONMENT=Development \
    ASPNETCORE_URLS="http://localhost:${port}" \
    SKIP_MIGRATIONS=true \
    nohup dotnet run --project "$csproj" --no-launch-profile \
      > "${LOG_DIR}/${name}-backend.log" 2>&1 &
    disown
  )
}

start_frontend() {
  local name="$1" repo="$2" fe_port="$3" extra_env="$4"
  local dir="${SERVICES_ROOT}/${repo}/src/frontend"
  if [ ! -d "$dir" ]; then
    warn "${name}: no tiene src/frontend, me lo salto"
    return
  fi
  stop_port "$fe_port" "${name} frontend"
  info "Arrancando frontend ${name} en :${fe_port}"
  (
    cd "$dir" || exit 1
    env ${extra_env} nohup npx vite --force \
      > "${LOG_DIR}/${name}-frontend.log" 2>&1 &
    disown
  )
}

wait_for_port() {
  # `dotnet run` compila primero (accounting tarda más de 40s en frío): se espera hasta 240s.
  local port="$1" name="$2" timeout="${3:-240}"
  local waited=0
  while ! timeout 1 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/${port}" 2>/dev/null; do
    sleep 2
    waited=$((waited + 2))
    if [ "$waited" -ge "$timeout" ]; then
      warn "${name} (:${port}) no respondió en ${timeout}s — revisa ${LOG_DIR}/${name}*.log"
      return 1
    fi
  done
  ok "${name} (:${port}) arriba (${waited}s)"
}

print_status() {
  echo
  printf "%-14s %-10s %-10s\n" "servicio" "backend" "frontend"
  for def in "${SERVICES_DEFS[@]}"; do
    IFS='|' read -r name repo csproj port fe_extra <<< "$def"
    local fe_port=$((port + 1000))
    local b_state="DOWN" f_state="DOWN"
    [ -n "$(port_pid "$port")" ] && b_state="UP (:$port)"
    [ -n "$(port_pid "$fe_port")" ] && f_state="UP (:$fe_port)"
    printf "%-14s %-10s %-10s\n" "$name" "$b_state" "$f_state"
  done
  echo
}

backend_up()  { [ -n "$(port_pid "$1")" ]; }
frontend_up() { [ -n "$(port_pid "$1")" ]; }

# Baja un servicio. $4 = backend|frontend|both.
stop_service() {
  local name="$1" port="$2" side="$3" csproj="$4"
  if [ "$side" != "frontend" ]; then
    stop_port "$port" "${name} backend"
    # Un `dotnet run` que aún compila no escucha en el puerto: se mata por su csproj.
    if pgrep -f "dotnet run --project ${csproj}" >/dev/null 2>&1; then
      pkill -TERM -f "dotnet run --project ${csproj}" 2>/dev/null
      sleep 1
    fi
  fi
  if [ "$side" != "backend" ]; then
    stop_port "$((port + 1000))" "${name} frontend"
  fi
}

usage_error() { err "$*"; echo "       Uso: $0 [servicio ...] [--reload|--stop|--status] [--backend] [--frontend]"; exit 2; }

main() {
  local mode="start" want_back=0 want_front=0
  local filter=()
  local known=" "
  for def in "${SERVICES_DEFS[@]}"; do known+="${def%%|*} "; done

  for arg in "$@"; do
    case "$arg" in
      --reload)   mode="reload" ;;
      --stop)     mode="stop" ;;
      --status)   mode="status" ;;
      --backend)  want_back=1 ;;
      --frontend) want_front=1 ;;
      -*)         usage_error "Opción desconocida: ${arg}" ;;
      *)
        case "$known" in
          *" ${arg} "*) filter+=("$arg") ;;
          *) usage_error "Servicio desconocido: ${arg} (válidos:${known% })" ;;
        esac
        ;;
    esac
  done

  # Sin --backend ni --frontend se tocan ambos lados.
  local side="both"
  if [ "$want_back" -eq 1 ] && [ "$want_front" -eq 0 ]; then side="backend"; fi
  if [ "$want_front" -eq 1 ] && [ "$want_back" -eq 0 ]; then side="frontend"; fi

  if [ "$mode" = "status" ]; then
    print_status
    exit 0
  fi

  selected() {
    [ "${#filter[@]}" -eq 0 ] && return 0
    local f
    for f in "${filter[@]}"; do [ "$f" = "$1" ] && return 0; done
    return 1
  }

  if [ "$mode" = "stop" ]; then
    for def in "${SERVICES_DEFS[@]}"; do
      IFS='|' read -r name repo csproj port fe_extra <<< "$def"
      selected "$name" || continue
      stop_service "$name" "$port" "$side" "$csproj"
    done
    ok "Listo, detenido."
    print_status
    exit 0
  fi

  check_tunnel || exit 1

  local started=()
  for def in "${SERVICES_DEFS[@]}"; do
    IFS='|' read -r name repo csproj port fe_extra <<< "$def"
    selected "$name" || continue
    local fe_port=$((port + 1000))

    if [ "$mode" = "reload" ]; then
      stop_service "$name" "$port" "$side" "$csproj"
    fi

    if [ "$side" != "frontend" ]; then
      if [ "$mode" = "start" ] && backend_up "$port"; then
        info "${name} backend ya está arriba (:${port}); usa --reload para reiniciarlo"
      else
        start_backend "$name" "$repo" "$csproj" "$port"
        started+=("$name")
      fi
    fi
    if [ "$side" != "backend" ]; then
      if [ "$mode" = "start" ] && frontend_up "$fe_port"; then
        info "${name} frontend ya está arriba (:${fe_port}); usa --reload para reiniciarlo"
      else
        start_frontend "$name" "$repo" "$fe_port" "$fe_extra"
      fi
    fi
  done

  if [ "${#started[@]}" -gt 0 ]; then
    echo
    info "Esperando a que los backends respondan (hasta 240s cada uno, incluye la compilación)..."
    for def in "${SERVICES_DEFS[@]}"; do
      IFS='|' read -r name repo csproj port fe_extra <<< "$def"
      local s
      for s in "${started[@]}"; do
        [ "$s" = "$name" ] && wait_for_port "$port" "$name"
      done
    done
  fi

  print_status
  info "Logs en ${LOG_DIR}/"
}

main "$@"
