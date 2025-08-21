#!/usr/bin/env bash
# crear-/boot-separado.sh
# Crea una partición /boot separada reduciendo la raíz y ajustando fstab/GRUB (UEFI).
# **USO RIESGOSO**: ¡haz backup! Pensado para raíz ext4 en /dev/sdX.

set -euo pipefail

###########################
# 🔧 PARÁMETROS A EDITAR
###########################
DISK="/dev/sda"            # Disco que contiene tu sistema
ROOT_PART="/dev/sda2"      # Partición raíz actual
EFI_PART="/dev/sda1"       # Partición EFI existente (FAT32)
TARGET_ROOT_SIZE_GIB=20     # Tamaño final que quieres para la raíz (GiB)
BOOT_SIZE_GIB=1             # Tamaño de la nueva partición /boot (GiB)
BOOT_FS_TYPE="ext4"         # Sistema de archivos para /boot
GRUB_TARGET="x86_64-efi"    # Objetivo de grub-install (UEFI x86_64)
GRUB_ID="GRUB"              # Nombre del bootloader en la ESP

# Modo simulación (dry run): poner a 1 para solo imprimir comandos críticos
DRY_RUN=${DRY_RUN:-0}

###########################
# 🧠 COMPROBACIONES
###########################
if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] Debes ejecutar como root." >&2
  exit 1
fi

if ! lsblk -no TYPE "${DISK}" | grep -q disk; then
  echo "[ERROR] Disco no válido: ${DISK}" >&2
  exit 1
fi

if [[ ! -b "${ROOT_PART}" ]]; then
  echo "[ERROR] Partición raíz no encontrada: ${ROOT_PART}" >&2
  exit 1
fi

if [[ ! -b "${EFI_PART}" ]]; then
  echo "[ERROR] Partición EFI no encontrada: ${EFI_PART}" >&2
  exit 1
fi

ROOT_FSTYPE=$(blkid -o value -s TYPE "${ROOT_PART}" || true)
if [[ "${ROOT_FSTYPE}" != "ext4" ]]; then
  echo "[ERROR] Solo se soporta raíz ext4 en este script. Encontrado: ${ROOT_FSTYPE}" >&2
  exit 1
fi

ESP_FSTYPE=$(blkid -o value -s TYPE "${EFI_PART}" || true)
if [[ "${ESP_FSTYPE}" != "vfat" && "${ESP_FSTYPE}" != "fat32" && "${ESP_FSTYPE}" != "msdos" ]]; then
  echo "[WARN] La ESP debería ser vfat/fat32. Detectado: ${ESP_FSTYPE}. Continúo…" >&2
fi

cat <<EOF
⚠️  ADVERTENCIA GRANDE
Este script va a:
  1) Reducir el sistema de archivos de ${ROOT_PART} a ${TARGET_ROOT_SIZE_GIB}GiB
  2) Reducir la partición correspondiente en ${DISK}
  3) Crear una nueva partición de ${BOOT_SIZE_GIB}GiB para /boot
  4) Copiar /boot, actualizar fstab y reinstalar GRUB en UEFI

Asegúrate de estar en un LiveUSB (la raíz NO debe estar montada) y de tener backup.
EOF

read -rp "Escribe EXACTAMENTE 'SI' para continuar: " CONFIRM
if [[ "${CONFIRM}" != "SI" ]]; then
  echo "Abortado por el usuario."; exit 1
fi

run(){
  echo "+ $*"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    eval "$@"
  fi
}

###########################
# 1) CHEQUEAR Y REDUCIR FS
###########################
echo "\n==> Comprobando sistema de archivos en ${ROOT_PART}"
run e2fsck -f "${ROOT_PART}"

echo "\n==> Redimensionando ext4 a ${TARGET_ROOT_SIZE_GIB}G"
run resize2fs "${ROOT_PART}" "${TARGET_ROOT_SIZE_GIB}G"

###########################
# 2) REDUCIR PARTICIÓN RAÍZ
###########################
echo "\n==> Calculando nueva posición de fin de la partición raíz"
PART_NUM=$(lsblk -no PARTTYPE,NAME "${ROOT_PART}" | awk '{print $2}' | sed 's/[^0-9]//g' || true)
if [[ -z "${PART_NUM}" ]]; then
  # fallback: extraer dígitos al final del nombre
  PART_NUM=$(basename "${ROOT_PART}" | grep -o '[0-9]\+$' || true)
fi
if [[ -z "${PART_NUM}" ]]; then
  echo "[ERROR] No pude determinar el número de partición para ${ROOT_PART}" >&2
  exit 1
fi

echo "Partición raíz detectada: número ${PART_NUM} en ${DISK}"

# Obtener inicio en GiB con parted -m (machine readable)
PART_INFO=$(parted -m -s "${DISK}" unit GiB print | awk -F: -v pn="${PART_NUM}" '$1==pn {print $2" "$3}')
ROOT_START_GIB=$(awk '{print $1}' <<<"${PART_INFO}" | sed 's/GiB//')
ROOT_END_GIB=$(awk '{print $2}' <<<"${PART_INFO}" | sed 's/GiB//')

if [[ -z "${ROOT_START_GIB}" || -z "${ROOT_END_GIB}" ]]; then
  echo "[ERROR] No pude leer inicio/fin de la partición raíz." >&2
  exit 1
fi

NEW_ROOT_END_GIB=$(python3 - <<PY
start=${ROOT_START_GIB}
size=${TARGET_ROOT_SIZE_GIB}
print(f"{start+size:.3f}")
PY
)

if (( $(echo "${NEW_ROOT_END_GIB} > ${ROOT_END_GIB}" | bc -l) )); then
  echo "[ERROR] El tamaño objetivo (${TARGET_ROOT_SIZE_GIB}GiB) es MAYOR que la partición actual. Aborta." >&2
  exit 1
fi

echo "Inicio raíz: ${ROOT_START_GIB}GiB  | Fin actual: ${ROOT_END_GIB}GiB  | Fin nuevo: ${NEW_ROOT_END_GIB}GiB"

echo "\n==> Redimensionando partición ${DISK}${PART_NUM}"
run parted -s "${DISK}" unit GiB resizepart "${PART_NUM}" "${NEW_ROOT_END_GIB}GiB"

###########################
# 3) CREAR NUEVA /boot
###########################
NEW_BOOT_START_GIB=${NEW_ROOT_END_GIB}
NEW_BOOT_END_GIB=$(python3 - <<PY
start=${NEW_BOOT_START_GIB}
size=${BOOT_SIZE_GIB}
print(f"{start+size:.3f}")
PY
)

echo "\n==> Creando nueva partición para /boot: ${BOOT_SIZE_GIB}GiB"
run parted -s "${DISK}" unit GiB mkpart primary "${BOOT_FS_TYPE}" "${NEW_BOOT_START_GIB}GiB" "${NEW_BOOT_END_GIB}GiB"

# Detectar el nombre de la nueva partición (la de mayor número)
NEW_BOOT_PART=$(lsblk -nrpo NAME "${DISK}" | tail -n1)
if [[ -z "${NEW_BOOT_PART}" || "${NEW_BOOT_PART}" == "${DISK}" ]]; then
  echo "[ERROR] No se pudo detectar la nueva partición de /boot." >&2
  exit 1
fi

echo "Nueva partición /boot: ${NEW_BOOT_PART}"

echo "\n==> Formateando ${NEW_BOOT_PART} en ${BOOT_FS_TYPE}"
case "${BOOT_FS_TYPE}" in
  ext4) run mkfs.ext4 -F "${NEW_BOOT_PART}" ;;
  xfs)  run mkfs.xfs -f "${NEW_BOOT_PART}" ;;
  *) echo "[ERROR] FS no soportado en script: ${BOOT_FS_TYPE}" >&2; exit 1 ;;
esac

###########################
# 4) MONTAR Y COPIAR /boot
###########################
echo "\n==> Montando sistema en /mnt"
run mkdir -p /mnt
run mount "${ROOT_PART}" /mnt
run mkdir -p /mnt/boot.old /mnt/boot.new /mnt/boot/efi
run mount "${NEW_BOOT_PART}" /mnt/boot.new
run mount "${EFI_PART}" /mnt/boot/efi

echo "\n==> Copiando contenido actual de /boot a la nueva partición"
run rsync -aHAXx /mnt/boot/ /mnt/boot.new/

# Intercambiar
run mv /mnt/boot /mnt/boot.old
run mkdir -p /mnt/boot
run mount "${NEW_BOOT_PART}" /mnt/boot
run rsync -aHAXx /mnt/boot.new/ /mnt/boot/

###########################
# 5) CHROOT + fstab + GRUB
###########################
BOOT_UUID=$(blkid -s UUID -o value "${NEW_BOOT_PART}")
if [[ -z "${BOOT_UUID}" ]]; then
  echo "[ERROR] No se pudo obtener UUID de ${NEW_BOOT_PART}" >&2
  exit 1
fi

echo "\n==> Preparando chroot"
run mount --bind /dev /mnt/dev
run mount --bind /proc /mnt/proc
run mount --bind /sys /mnt/sys

CHROOT_CMDS=$(cat <<CH
set -euo pipefail
# Añadir /boot a fstab si no existe
if ! grep -qE "^UUID=${BOOT_UUID} .* /boot " /etc/fstab; then
  echo "UUID=${BOOT_UUID}  /boot  ${BOOT_FS_TYPE}  defaults  0 2" >> /etc/fstab
fi

# Asegurar que /boot/efi está en fstab (no tocamos si ya existe)
ESP_UUID=$(blkid -s UUID -o value ${EFI_PART} || true)
if [[ -n "${ESP_UUID}" ]] && ! grep -q "/boot/efi" /etc/fstab; then
  echo "UUID=${ESP_UUID}  /boot/efi  vfat  umask=0077  0 1" >> /etc/fstab
fi

# Reinstalar GRUB (UEFI)
if command -v grub-install >/dev/null 2>&1; then
  grub-install --target=${GRUB_TARGET} --efi-directory=/boot/efi --bootloader-id=${GRUB_ID}
  if command -v update-grub >/dev/null 2>&1; then
    update-grub
  elif command -v grub-mkconfig >/dev/null 2>&1; then
    grub-mkconfig -o /boot/grub/grub.cfg
  fi
fi
CH
)

echo "\n==> Ejecutando acciones en chroot"
if [[ "${DRY_RUN}" -eq 0 ]]; then
  chroot /mnt /bin/bash -c "${CHROOT_CMDS}"
else
  echo "[DRY-RUN] chroot /mnt …"
  echo "${CHROOT_CMDS}"
fi

###########################
# 6) LIMPIEZA Y RESUMEN
###########################
echo "\n==> Limpiando montajes temporales"
run umount -R /mnt/boot.new || true
run umount -R /mnt/dev || true
run umount -R /mnt/proc || true
run umount -R /mnt/sys || true
run umount -R /mnt/boot || true
run umount -R /mnt || true

echo "\n✅ Terminado. Reinicia el sistema y verifica el arranque."
echo "   - Nueva /boot: ${NEW_BOOT_PART} (UUID=${BOOT_UUID})"
echo "   - Revisa /etc/fstab y /boot/grub/grub.cfg si algo no cuadra."
