#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyrusCrypt – GUI para reencriptar dispositivos LUKS y configurar sistema
---------------------------------------------------
• Requiere Linux, Python 3 y herramientas del sistema: lsblk, e2fsck, resize2fs, cryptsetup, grub-install, update-grub, update-initramfs
• Debe ejecutarse como root (sudo)

ADVERTENCIA: Reencriptar discos puede dejar el sistema inservible si se interrumpe
el proceso o se elige un dispositivo incorrecto. Úsalo bajo tu propia responsabilidad
y con copias de seguridad.
"""

import os
import sys
import json
import tempfile
import threading
import subprocess
import shutil
from datetime import datetime


import getpass

APP_TITLE = "PyrusCrypt – Reencriptador LUKS"

# --------------------------- Utilidades de sistema --------------------------- #

def require_root():
    if os.geteuid() != 0:
        print("ERROR: Esta aplicación debe ejecutarse como root.\nEjemplo: sudo python3 pyrus.py")
        sys.exit(1)


def cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_and_stream(cmd, log_cb, check=True):
    log_cb(f"\n$ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    for line in iter(proc.stdout.readline, ''):
        log_cb(line)
    proc.stdout.close()
    rc = proc.wait()
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)
    return rc


def list_block_devices():
    try:
        out = subprocess.check_output(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,PATH,MOUNTPOINT,FSTYPE"], text=True)
        data = json.loads(out)
    except Exception:
        return []

    devices = []
    def walk(node):
        ntype = node.get("type")
        if ntype in ("disk", "part"):
            size = node.get("size", "?")
            path = node.get("path") or f"/dev/{node.get('name')}"
            mpt = node.get("mountpoint")
            fstype = node.get("fstype")
            devices.append({
                "path": path,
                "type": ntype,
                "size": size,
                "mountpoint": mpt,
                "fstype": fstype,
            })
        for ch in node.get("children", []) or []:
            walk(ch)

    for block in data.get("blockdevices", []) or []:
        walk(block)

    devices.sort(key=lambda d: d["path"])
    return devices



def print_log(text: str):
    ts = datetime.now().strftime("[%H:%M:%S] ")
    if text.startswith("$") or text.startswith("\n$"):
        line = f"{text}"
    else:
        line = f"{ts}{text}"
    print(line, end='')

def main():
    print("""
            __====-_  _-====__
                  _--^^^#####//      \\#####^^^--_
              _-^##########// (    ) \\##########^-_
             -############//  |\^^/|  \\############-
          _/############//   (0::0)   \\############\_
         /#############((     \\//     ))#############\
        -###############\\    (oo)    //###############-
      -#################\\  / UUU \  //#################-
     -###################\\/  (v)  \\//###################-
    _#/|##########/\######(   /   \   )######/\##########|\#_
    |/ |#/#/#/\/  \#/#/##\  (     )  /##/#/  \/#/#/#/\#| \|
    `  |/  V  V  `   V  \#\|  (___)  |/#/  V   '  V  V  \|  '
        `   `  `      `   / |  (   )  | \   '      '  '   '
                              (   |  (___)  |   )
                              `uuu'         `uuu'
     """)
    print("PyrusCrypt – Reencriptador LUKS (terminal)")
    require_root()

    if not cmd_exists("cryptsetup"):
        print("ERROR: No se encontró 'cryptsetup'. Instálalo e inténtalo de nuevo.")
        sys.exit(1)
    for tool in ("lsblk", "e2fsck", "resize2fs"):
        if not cmd_exists(tool):
            print(f"ERROR: No se encontró '{tool}'.")
            sys.exit(1)

    # Listar dispositivos
    devices = list_block_devices()
    if not devices:
        print("No se detectaron dispositivos.")
        sys.exit(1)
    print("\nDispositivos detectados:")
    for idx, d in enumerate(devices):
        mount = f" (montado en {d['mountpoint']})" if d['mountpoint'] else ""
        print(f"  [{idx}] {d['path']} [{d['type']}] {d['size']}{mount}")
    while True:
        sel = input("\nElige el número del dispositivo a reencriptar: ").strip()
        if sel.isdigit() and int(sel) < len(devices):
            device = devices[int(sel)]['path']
            break
        print("Opción inválida. Intenta de nuevo.")

    # Contraseña
    while True:
        password = getpass.getpass("Contraseña LUKS: ")
        password2 = getpass.getpass("Confirmar contraseña: ")
        if not password:
            print("La contraseña no puede estar vacía.")
        elif password != password2:
            print("Las contraseñas no coinciden.")
        else:
            break

    reduce_size = input("Reduce device size (cryptsetup) [32M]: ").strip() or "32M"

    # Opciones
    def ask_bool(msg, default=True):
        s = input(f"{msg} [{'S/n' if default else 's/N'}]: ").strip().lower()
        if s == '':
            return default
        return s in ['s', 'si', 'y', 'yes']

    run_fsck = ask_bool("¿Ejecutar e2fsck -f -y (recomendado)?", True)
    run_minimize = ask_bool("¿resize2fs -M antes del reencriptado?", True)
    run_chroot = ask_bool("¿Montar y configurar sistema (chroot + GRUB)?", False)

    print(f"\nVas a reencriptar: {device}\nEsto puede tardar mucho tiempo y es arriesgado.")
    confirm = ask_bool("¿Continuar?", False)
    if not confirm:
        print("Cancelado por el usuario.")
        sys.exit(0)

    keyfile = None
    try:
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(password.encode())
        tf.flush()
        tf.close()
        keyfile = tf.name

        if run_fsck:
            print_log("\n== Paso 1/4: Comprobando sistema de archivos (e2fsck -f -y)… ==\n")
            run_and_stream(["e2fsck", "-f", "-y", device], print_log)

        if run_minimize:
            print_log("\n== Paso 2/4: Redimensionando al mínimo (resize2fs -M)… ==\n")
            run_and_stream(["resize2fs", "-M", device], print_log)

        print_log("\n== Paso 3/4: Reencriptando con cryptsetup reencrypt (LUKS2)… ==\n")
        cmd = [
            "cryptsetup", "reencrypt",
            "--batch-mode",
            "--encrypt", "--type", "luks2",
            "--hash", "sha256", "--pbkdf", "pbkdf2",
            "--reduce-device-size", reduce_size,
            "--key-file", keyfile,
            device,
        ]
        run_and_stream(cmd, print_log)

        if run_chroot:
            print_log("\n== Paso 4/4: Montaje, chroot y configuración ==\n")
            uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", device], text=True).strip()
            cryptdev = "/dev/mapper/cryptroot"
            run_and_stream(["cryptsetup", "open", device, "cryptroot", "--key-file", keyfile], print_log)
            run_and_stream(["e2fsck", "-f", cryptdev], print_log, check=False)
            run_and_stream(["resize2fs", cryptdev], print_log, check=False)
            run_and_stream(["mkdir", "-p", "/mnt/root"], print_log, check=False)
            run_and_stream(["mount", cryptdev, "/mnt/root"], print_log, check=False)

            parts = list_block_devices()
            boot_uuid = None
            efi_uuid = None
            for p in parts:
                if p["type"] == "part" and p["fstype"] in ("vfat", "efi"):
                    efi_uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p["path"]], text=True).strip()
                    run_and_stream(["mkdir", "-p", "/mnt/root/boot/efi"], print_log, check=False)
                    run_and_stream(["mount", f"UUID={efi_uuid}", "/mnt/root/boot/efi"], print_log, check=False)
                elif p["type"] == "part" and p["fstype"] in ("ext2", "ext3", "ext4"):
                    boot_uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p["path"]], text=True).strip()
                    run_and_stream(["mkdir", "-p", "/mnt/root/boot"], print_log, check=False)
                    run_and_stream(["mount", f"UUID={boot_uuid}", "/mnt/root/boot"], print_log, check=False)

            run_and_stream(["mount", "--bind", "/dev", "/mnt/root/dev"], print_log, check=False)
            run_and_stream(["mount", "--bind", "/proc", "/mnt/root/proc"], print_log, check=False)
            run_and_stream(["mount", "--bind", "/sys", "/mnt/root/sys"], print_log, check=False)
            run_and_stream(["mount", "--bind", "/run", "/mnt/root/run"], print_log, check=False)

            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c",
                            f"echo 'cryptroot UUID={uuid} none luks' > /etc/crypttab"],
                           print_log, check=False)
            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c",
                            f"echo '/dev/mapper/cryptroot / ext4 defaults 0 1' > /etc/fstab"],
                           print_log, check=False)

            print_log("\n-- Regenerando initramfs (update-initramfs -u -k all) --\n")
            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", "update-initramfs -u -k all"], print_log)

            grub_line = f'GRUB_CMDLINE_LINUX="cryptdevice=UUID={uuid}:cryptroot root=/dev/mapper/cryptroot"'
            chroot_cmd = f"grep -q '^GRUB_CMDLINE_LINUX' /etc/default/grub && sed -i 's|^GRUB_CMDLINE_LINUX.*|{grub_line}|' /etc/default/grub || echo '{grub_line}' >> /etc/default/grub"
            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", chroot_cmd], print_log)

            base_disk = device.rstrip('0123456789')
            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", f"grub-install {base_disk}"], print_log)
            run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", "update-grub"], print_log)

        print_log("\n✔ Proceso completado correctamente.\n")
    except subprocess.CalledProcessError as e:
        print_log(f"\n✖ Error (rc={e.returncode}) en: {' '.join(e.cmd)}\n")
        print(f"Fallo al ejecutar: {' '.join(e.cmd)}\nCódigo: {e.returncode}")
        sys.exit(1)
    except Exception as ex:
        print_log(f"\n✖ Error inesperado: {ex}\n")
        print(f"Error inesperado: {ex}")
        sys.exit(1)
    finally:
        if keyfile and os.path.exists(keyfile):
            os.remove(keyfile)

if __name__ == "__main__":
    main()