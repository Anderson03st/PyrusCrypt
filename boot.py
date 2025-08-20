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

import tkinter as tk
from tkinter import ttk, messagebox
import re

APP_TITLE = "PyrusCrypt"

# --------------------------- Utilidades de sistema --------------------------- #

def require_root():
    if os.geteuid() != 0:
        messagebox.showerror(
            "Permisos insuficientes",
            "Esta aplicación debe ejecutarse como root.\n\nEjemplos:\n- sudo python3 pyrus.py\n- sudo ./PyrusCrypt",
        )
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


def _get_base_disk_from_device(device_path: str) -> str:
    """Devuelve el disco base para una ruta de partición (/dev/sda1 -> /dev/sda, /dev/nvme0n1p3 -> /dev/nvme0n1)."""
    if device_path.startswith("/dev/nvme") or device_path.startswith("/dev/mmcblk"):
        return re.sub(r"p\d+$", "", device_path)
    return re.sub(r"\d+$", "", device_path)


def create_boot_partition_if_missing(device: str, append_log):
    """
    Fuerza la creación de una partición /boot ext4 (~1GiB):
    1) Intenta liberar ~1GiB al final del disco reduciendo la partición objetivo
       (solo ext2/3/4, no LUKS directo), y 2) crea la nueva partición, la formatea,
       copia /boot y añade la entrada a /etc/fstab.
    """
    parts_before = list_block_devices()
    append_log("Liberando ~1GiB al final del disco y creando partición /boot (~1GiB)…\n")

    base_disk = _get_base_disk_from_device(device)
    # Detectar tipo de la partición seleccionada
    dev_info = next((p for p in parts_before if p.get("path") == device), None)
    dev_fstype = (dev_info or {}).get("fstype")
    is_luks = str(dev_fstype).lower() in ("crypto_luks", "luks")
    if is_luks:
        append_log("[INFO] El dispositivo seleccionado parece ser LUKS (crypto_LUKS). No se intentará reducir automáticamente la partición, ya que el FS está dentro del contenedor.\n")
    # Crear partición al final del disco si hay hueco (aprox 1GiB)
    # Usamos rangos negativos para apuntar al final del disco.
    def _get_partition_number(dev_path: str) -> str:
        m = re.search(r"(\d+)$", dev_path)
        return m.group(1) if m else ""

    def _get_partition_end_mib(disk: str, part_num: str) -> float:
        out = subprocess.check_output(["parted", "-m", disk, "unit", "MiB", "print", "free"], text=True)
        for line in out.splitlines():
            if not line or line.startswith("BYT;") or line.startswith(disk):
                continue
            fields = line.split(":")
            if fields[0] == part_num:
                # fields: nr:start:end:size:fs:name:flags
                end = fields[2]
                if end.endswith("MiB"):
                    return float(end[:-3])
        raise RuntimeError("No se pudo obtener el fin de la partición")

    def _get_table_type_and_last_part(disk: str):
        out = subprocess.check_output(["parted", "-m", disk, "unit", "MiB", "print", "free"], text=True)
        table_type = None
        max_part = None
        for line in out.splitlines():
            if line.startswith(disk):
                # Ej: /dev/sda:...:scsi:512:512:gpt:...
                parts = line.split(":")
                if len(parts) >= 6:
                    table_type = parts[5]
            elif line and line[0].isdigit():
                num = line.split(":", 1)[0]
                try:
                    val = int(num)
                    if max_part is None or val > max_part:
                        max_part = val
                except ValueError:
                    pass
        return table_type or "unknown", max_part

    def _shrink_partition_free_space(dev_path: str, current_mountpoint: str | None) -> bool:
        try:
            append_log("Intentando liberar ~1GiB reduciendo la partición raíz…\n")
            # Si está montada, intentar desmontar salvo que sea '/'
            remount_after = None
            if current_mountpoint:
                if current_mountpoint == "/":
                    append_log("[ERROR] La partición objetivo está montada como '/'. Ejecuta desde un entorno live/rescue para poder reducirla.\n")
                    return False
                append_log(f"Desmontando {dev_path} de {current_mountpoint}…\n")
                run_and_stream(["umount", dev_path], append_log)
                remount_after = current_mountpoint
            # Asegurar integridad y minimizar FS
            run_and_stream(["e2fsck", "-f", "-y", dev_path], append_log)
            run_and_stream(["resize2fs", "-M", dev_path], append_log)

            part_num = _get_partition_number(dev_path)
            if not part_num:
                append_log("[ERROR] No se pudo determinar el número de partición.\n")
                return False
            table_type, last_part = _get_table_type_and_last_part(base_disk)
            if str(part_num) != str(last_part):
                append_log(f"[ERROR] La partición seleccionada {part_num} no es la última del disco (última: {last_part}). No se puede reducir para liberar cola.\n")
                return False
            current_end = _get_partition_end_mib(base_disk, part_num)
            new_end = max(1.0, current_end - 1050.0)
            # Reducir partición
            run_and_stream(["parted", base_disk, "--script", "unit", "MiB", "resizepart", part_num, str(int(new_end)) + "MiB"], append_log)
            run_and_stream(["partprobe", base_disk], append_log, check=False)
            run_and_stream(["udevadm", "settle"], append_log, check=False)
            # Si se desmontó, re-montar para dejar el sistema como estaba
            if remount_after:
                append_log(f"Remontando {dev_path} en {remount_after}…\n")
                run_and_stream(["mkdir", "-p", remount_after], append_log, check=False)
                run_and_stream(["mount", dev_path, remount_after], append_log, check=False)
            return True
        except Exception as ex:
            append_log(f"[ERROR] Falló la reducción de la partición: {ex}\n")
            return False

    # Determinar mejor objetivo de reducción: la partición montada en '/'
    shrink_target = device
    shrink_mountpoint = None
    try:
        current = list_block_devices()
        # Preferir la partición montada en '/'
        root_parts = [p for p in current if p.get("mountpoint") == "/" and p.get("type") == "part"]
        if root_parts:
            shrink_target = root_parts[0]["path"]
            shrink_mountpoint = "/"
            if shrink_target != device:
                append_log(f"Usando {shrink_target} como partición raíz para liberar espacio (seleccionado: {device}).\n")
        else:
            # Si no hay '/', mirar si el propio device está montado en algún sitio
            mp = next((p.get("mountpoint") for p in current if p.get("path") == device and p.get("mountpoint")), None)
            if mp:
                shrink_mountpoint = mp
    except Exception:
        pass

    # Log de tabla y última partición
    table_type, last_part = _get_table_type_and_last_part(base_disk)
    append_log(f"Tabla de particiones detectada: {table_type}. Última partición: {last_part}\n")

    # 1) Intentar liberar espacio primero (si no es LUKS)
    if not is_luks:
        _shrink_partition_free_space(shrink_target, shrink_mountpoint)
    else:
        append_log("[INFO] Omite reducción automática por ser LUKS directo.\n")

    # 2) Crear la partición de /boot
    try:
        run_and_stream(["parted", base_disk, "--script", "mkpart", "primary", "ext4", "-1050MiB", "-1MiB"], append_log)
    except subprocess.CalledProcessError:
        if table_type.lower() == "msdos":
            append_log("[PISTA] Si el disco usa tabla MSDOS y ya hay 4 primarias, crea/usa una extendida o migra a GPT.\n")
        if is_luks:
            append_log("[ERROR] No fue posible crear /boot: no hay espacio al final y no podemos reducir una partición LUKS sin abrir y encoger el FS interno.\n")
        else:
            append_log("[ERROR] No fue posible crear /boot. Verifica que la partición objetivo sea la última y que se pudo liberar espacio.\n")
        return

    # Notificar al kernel y esperar a que aparezca la nueva partición
    run_and_stream(["partprobe", base_disk], append_log, check=False)
    run_and_stream(["udevadm", "settle"], append_log, check=False)

    # Detectar nueva partición
    after = list_block_devices()
    before_set = {p["path"] for p in parts_before}
    after_set = {p["path"] for p in after}
    new_parts = sorted(list(after_set - before_set))
    if not new_parts:
        append_log("[ERROR] No se pudo detectar la nueva partición creada.\n")
        return

    new_boot = new_parts[-1]
    append_log(f"Nueva partición detectada: {new_boot}\n")

    # Formatear como ext4 y copiar contenido de /boot
    run_and_stream(["mkfs.ext4", "-L", "BOOT", new_boot], append_log)
    run_and_stream(["mkdir", "-p", "/mnt/tmpboot"], append_log)
    run_and_stream(["mount", new_boot, "/mnt/tmpboot"], append_log)
    # Copia robusta (incluye archivos ocultos)
    run_and_stream(["bash", "-c", "cp -a /boot/. /mnt/tmpboot/"], append_log)
    run_and_stream(["umount", "/mnt/tmpboot"], append_log)

    # Añadir a fstab del sistema actual
    try:
        boot_uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", new_boot], text=True).strip()
    except Exception as ex:
        append_log(f"[ERROR] No se pudo obtener UUID de {new_boot}: {ex}\n")
        return
    run_and_stream(["bash", "-c", f"echo 'UUID={boot_uuid} /boot ext4 defaults 0 2' >> /etc/fstab"], append_log)
    append_log("Partición /boot creada y configurada correctamente (ext4).\n")


# ------------------------------ Interfaz GUI -------------------------------- #

class PyrusCryptGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🐉 " + APP_TITLE + " 🔒")
        self.geometry("820x600")
        self.minsize(780, 560)
        
        self._configure_styles()
        self._build_ui()
        self.refresh_devices()

    def _configure_styles(self):
        self.configure(bg="#0F1C17")  # Fondo principal verde muy oscuro
        style = ttk.Style(self)
        style.theme_use('clam')

        # Paleta de colores de fantasía
        BG_COLOR = "#0F1C17"      # Verde muy oscuro (fondo principal)
        FG_COLOR = "#D2B26E"      # Dorado (texto y acentos)
        ALT_BG_COLOR = "#162720"  # Verde alterno más claro
        SELECT_BG = "#1E342B"     # Verde más intenso (hover/selección)
        SELECT_FG = "#E8D6A2"     # Dorado claro (estados activos)
        BORDER_COLOR = "#2A3B33"  # Verde grisáceo oscuro

        style.configure('.',
                        background=BG_COLOR,
                        foreground=FG_COLOR,
                        fieldbackground=ALT_BG_COLOR,
                        troughcolor=ALT_BG_COLOR,
                        darkcolor=BORDER_COLOR,
                        lightcolor=BORDER_COLOR,
                        bordercolor=BORDER_COLOR)

        style.map('.',
                  background=[('active', SELECT_BG), ('disabled', ALT_BG_COLOR)],
                  foreground=[('active', SELECT_FG), ('disabled', '#8B7355')])

        style.configure('TLabel',
                        background=BG_COLOR,
                        foreground=FG_COLOR)

        style.configure('TButton',
                        background=ALT_BG_COLOR,
                        foreground=FG_COLOR,
                        relief=tk.FLAT,
                        borderwidth=1)
        style.map('TButton',
                  background=[('active', SELECT_BG), ('pressed', SELECT_BG)],
                  foreground=[('active', SELECT_FG), ('pressed', SELECT_FG)])

        style.configure('TCombobox',
                        fieldbackground=ALT_BG_COLOR,
                        background=ALT_BG_COLOR,
                        foreground=FG_COLOR,
                        arrowcolor=FG_COLOR,
                        selectbackground=SELECT_BG,
                        selectforeground=SELECT_FG)
        
        style.configure('TEntry',
                        fieldbackground=ALT_BG_COLOR,
                        foreground=FG_COLOR,
                        insertcolor=FG_COLOR)

        style.configure('Horizontal.TProgressbar',
                        background=FG_COLOR,      # Barra dorada
                        troughcolor=ALT_BG_COLOR) # Fondo verde oscuro
                        
        style.configure('TCheckbutton',
                        background=BG_COLOR,
                        foreground=FG_COLOR)
        style.map('TCheckbutton',
                  background=[('active', BG_COLOR)],
                  indicatorcolor=[('selected', FG_COLOR), ('!selected', ALT_BG_COLOR)])

    def on_device_selected(self, event=None):
        self.dev_combo.master.focus()
        style = ttk.Style(self)
        style.map('TCombobox',
                  fieldbackground=[('readonly', '#1A1A1A')],
                  foreground=[('readonly', '#00FF00')])

    def _build_ui(self):
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)
        
        # Configurar estilo para el Combobox antes de crearlo
        style = ttk.Style(self)
        style.map('TCombobox',
                  fieldbackground=[('readonly', '#1A1A1A')],
                  foreground=[('readonly', '#555555')]) # Color inicial atenuado

        row0 = ttk.Frame(frm)
        row0.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(row0, text="Dispositivo a reencriptar:").pack(side=tk.LEFT)
        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(row0, textvariable=self.dev_var, width=60, state="readonly")
        self.dev_combo.pack(side=tk.LEFT, padx=8)
        self.dev_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        ttk.Button(row0, text="Actualizar", command=self.refresh_devices).pack(side=tk.LEFT)

        self.mount_warn = ttk.Label(frm, text="", foreground="#00FF44")
        self.mount_warn.pack(anchor="w")

        sep1 = ttk.Separator(frm)
        sep1.pack(fill=tk.X, pady=8)

        grid = ttk.Frame(frm)
        grid.pack(fill=tk.X)

        ttk.Label(grid, text="Contraseña LUKS:").grid(row=0, column=0, sticky="w")
        self.pass1 = ttk.Entry(grid, show="*")
        self.pass1.grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Label(grid, text="Confirmar contraseña:").grid(row=1, column=0, sticky="w")
        self.pass2 = ttk.Entry(grid, show="*")
        self.pass2.grid(row=1, column=1, sticky="ew", padx=8)

        ttk.Label(grid, text="Reduce device size (cryptsetup)").grid(row=2, column=0, sticky="w")
        self.reduce_sz = ttk.Entry(grid)
        self.reduce_sz.insert(0, "32M")
        self.reduce_sz.grid(row=2, column=1, sticky="w", padx=8)

        self.do_fsck = tk.BooleanVar(value=True)
        self.do_minimize = tk.BooleanVar(value=True)
        self.do_chroot = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="Ejecutar e2fsck -f -y (recomendado)", variable=self.do_fsck).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(grid, text="resize2fs -M antes del reencriptado", variable=self.do_minimize).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(grid, text="Montar y configurar sistema (chroot + GRUB)", variable=self.do_chroot).grid(row=5, column=0, columnspan=2, sticky="w")
        self.do_boot = tk.BooleanVar(value=True)
        ttk.Checkbutton(grid, text="Crear partición /boot si falta (experimental)", variable=self.do_boot).grid(row=6, column=0, columnspan=2, sticky="w")

        grid.columnconfigure(1, weight=1)

        sep2 = ttk.Separator(frm)
        sep2.pack(fill=tk.X, pady=8)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X)
        self.start_btn = ttk.Button(btns, text="Iniciar proceso", command=self.start)
        self.start_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="Salir", command=self.destroy).pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(6, 2))
        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor="w")

        sep3 = ttk.Separator(frm)
        sep3.pack(fill=tk.X, pady=8)
        ttk.Label(frm, text="Registro del proceso:").pack(anchor="w")
        self.log = tk.Text(frm, height=20, wrap=tk.NONE, background="#0F1C17", foreground="#D2B26E", insertbackground="#D2B26E", relief=tk.FLAT)
        self.log.pack(fill=tk.BOTH, expand=True)
        yscroll = ttk.Scrollbar(self.log, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def append_log(self, text: str):
        ts = datetime.now().strftime("[%H:%M:%S] ")
        if text.startswith("$") or text.startswith("\n$"):
            line = f"{text}"
        else:
            line = f"{ts}{text}"
        self.log.insert(tk.END, line)
        self.log.see(tk.END)
        self.update_idletasks()

    def refresh_devices(self):
        devices = list_block_devices()
        entries = []
        warn_mounts = []
        for d in devices:
            label = f"{d['path']}  [{d['type']}]  {d['size']}"
            if d.get('mountpoint'):
                label += f"  (montado en {d['mountpoint']})"
                warn_mounts.append(label)
            entries.append(label)
        self.dev_combo['values'] = entries
        if entries:
            self.dev_combo.current(0)
        self.mount_warn.configure(text=("⚠️ Estas son las particiones montadas. Revisa antes de continuar:\n- " + "\n- ".join(warn_mounts)) if warn_mounts else "")

    def start(self):
        if os.geteuid() != 0:
            messagebox.showerror("Permisos insuficientes", "Debes ejecutar como root (sudo).")
            return

        sel = self.dev_var.get().strip()
        if not sel:
            messagebox.showwarning("Falta dispositivo", "Selecciona un dispositivo o partición.")
            return
        device = sel.split()[0]

        p1 = self.pass1.get()
        p2 = self.pass2.get()
        if not p1:
            messagebox.showwarning("Contraseña vacía", "Introduce una contraseña.")
            return
        if p1 != p2:
            messagebox.showwarning("No coinciden", "Las contraseñas no coinciden.")
            return

        rsize = self.reduce_sz.get().strip() or "32M"

        if self.mount_warn.cget("text"):
            if not messagebox.askyesno("Advertencia", "Se detectan puntos de montaje.\n¿Estás seguro de continuar?"):
                return

        if not cmd_exists("cryptsetup"):
            messagebox.showerror("Falta dependencia", "No se encontró 'cryptsetup'. Instálalo e inténtalo de nuevo.")
            return
        for tool in ("lsblk", "e2fsck", "resize2fs"):
            if not cmd_exists(tool):
                messagebox.showerror("Falta dependencia", f"No se encontró '{tool}'.")
                return

        if not messagebox.askyesno("Confirmar", f"⚠️ Vas a reencriptar: {device}\n\nEsto puede tardar mucho tiempo y es arriesgado. ¿Continuar?"):
            return

        self.start_btn.configure(state=tk.DISABLED)
        self.progress['value'] = 0
        self.status_var.set("Ejecutando…")

        t = threading.Thread(target=self._worker, args=(device, p1, rsize, self.do_fsck.get(), self.do_minimize.get(), self.do_boot.get(), self.do_chroot.get()), daemon=True)
        t.start()

    def _worker(self, device, password, reduce_size, run_fsck, run_minimize, run_boot, run_chroot):
        keyfile = None
        try:
            steps = []
            if run_fsck: steps.append("fsck")
            if run_minimize: steps.append("minimize")
            if run_boot: steps.append("boot")
            steps.append("reencrypt")
            if run_chroot: steps.append("chroot")
            
            num_steps = len(steps)
            progress_increment = 100 / num_steps
            current_progress = 0
            self.progress['value'] = 0
            step_no = 1

            tf = tempfile.NamedTemporaryFile(delete=False)
            tf.write(password.encode())
            tf.flush()
            tf.close()
            keyfile = tf.name

            if run_fsck:
                self.append_log(f"\n== Paso {step_no}/{num_steps}: Comprobando sistema de archivos (e2fsck -f -y)… ==\n")
                run_and_stream(["e2fsck", "-f", "-y", device], self.append_log)
                current_progress += progress_increment
                self.progress['value'] = current_progress
                step_no += 1

            if run_minimize:
                self.append_log(f"\n== Paso {step_no}/{num_steps}: Redimensionando al mínimo (resize2fs -M)… ==\n")
                run_and_stream(["resize2fs", "-M", device], self.append_log)
                current_progress += progress_increment
                self.progress['value'] = current_progress
                step_no += 1

            if run_boot:
                self.append_log(f"\n== Paso {step_no}/{num_steps}: Creando partición /boot si falta… ==\n")
                try:
                    create_boot_partition_if_missing(device, self.append_log)
                except Exception as ex:
                    self.append_log(f"[ADVERTENCIA] No se pudo crear /boot automáticamente: {ex}\n")
                current_progress += progress_increment
                self.progress['value'] = current_progress
                step_no += 1

            self.append_log(f"\n== Paso {step_no}/{num_steps}: Reencriptando con cryptsetup reencrypt (LUKS2)… ==\n")
            cmd = [
                "cryptsetup", "reencrypt",
                "--batch-mode",
                "--encrypt", "--type", "luks2",
                "--hash", "sha256", "--pbkdf", "pbkdf2",
                "--reduce-device-size", reduce_size,
                "--key-file", keyfile,
                device,
            ]
            run_and_stream(cmd, self.append_log)
            current_progress += progress_increment
            self.progress['value'] = current_progress
            step_no += 1

            if run_chroot:
                self.append_log(f"\n== Paso {step_no}/{num_steps}: Montaje, chroot y configuración ==\n")
                uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", device], text=True).strip()
                cryptdev = "/dev/mapper/cryptroot"
                run_and_stream(["cryptsetup", "open", device, "cryptroot", "--key-file", keyfile], self.append_log)
                run_and_stream(["e2fsck", "-f", cryptdev], self.append_log, check=False)
                run_and_stream(["resize2fs", cryptdev], self.append_log, check=False)
                run_and_stream(["mkdir", "-p", "/mnt/root"], self.append_log, check=False)
                run_and_stream(["mount", cryptdev, "/mnt/root"], self.append_log, check=False)

                parts = list_block_devices()
                boot_uuid = None
                efi_uuid = None
                for p in parts:
                    if p["type"] == "part" and p["fstype"] in ("vfat", "efi"):
                        efi_uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p["path"]], text=True).strip()
                        run_and_stream(["mkdir", "-p", "/mnt/root/boot/efi"], self.append_log, check=False)
                        run_and_stream(["mount", f"UUID={efi_uuid}", "/mnt/root/boot/efi"], self.append_log, check=False)
                    elif p["type"] == "part" and p["fstype"] in ("ext2", "ext3", "ext4"):
                        boot_uuid = subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", p["path"]], text=True).strip()
                        run_and_stream(["mkdir", "-p", "/mnt/root/boot"], self.append_log, check=False)
                        run_and_stream(["mount", f"UUID={boot_uuid}", "/mnt/root/boot"], self.append_log, check=False)

                run_and_stream(["mount", "--bind", "/dev", "/mnt/root/dev"], self.append_log, check=False)
                run_and_stream(["mount", "--bind", "/proc", "/mnt/root/proc"], self.append_log, check=False)
                run_and_stream(["mount", "--bind", "/sys", "/mnt/root/sys"], self.append_log, check=False)
                run_and_stream(["mount", "--bind", "/run", "/mnt/root/run"], self.append_log, check=False)

                run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c",
                                f"echo 'cryptroot UUID={uuid} none luks' > /etc/crypttab"],
                               self.append_log, check=False)
                run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c",
                                f"echo '/dev/mapper/cryptroot / ext4 defaults 0 1' > /etc/fstab"],
                               self.append_log, check=False)
                if boot_uuid:
                    run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c",
                                    f"echo 'UUID={boot_uuid} /boot ext4 defaults 0 2' >> /etc/fstab"],
                                   self.append_log, check=False)

                self.append_log("\n-- Regenerando initramfs (update-initramfs -u -k all) --\n")
                run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", "update-initramfs -u -k all"], self.append_log)

                grub_line = f'GRUB_CMDLINE_LINUX="cryptdevice=UUID={uuid}:cryptroot root=/dev/mapper/cryptroot"'
                chroot_cmd = f"grep -q '^GRUB_CMDLINE_LINUX' /etc/default/grub && sed -i 's|^GRUB_CMDLINE_LINUX.*|{grub_line}|' /etc/default/grub || echo '{grub_line}' >> /etc/default/grub"
                run_and_stream(["chroot", "/mnt/root", "/bin/bash", "-c", chroot_cmd], self.append_log)

                current_progress += progress_increment
                self.progress['value'] = current_progress

            self.progress['value'] = 100
            self.append_log("\n✔ Proceso completado correctamente.\n")
            self.status_var.set("Completado.")
            messagebox.showinfo("Éxito", "El proceso terminó correctamente.")
        except subprocess.CalledProcessError as e:
            self.append_log(f"\n✖ Error (rc={e.returncode}) en: {' '.join(e.cmd)}\n")
            self.status_var.set("Falló.")
            messagebox.showerror("Error", f"Fallo al ejecutar: {' '.join(e.cmd)}\nCódigo: {e.returncode}")
        except Exception as ex:
            self.append_log(f"\n✖ Error inesperado: {ex}\n")
            self.status_var.set("Falló.")
            messagebox.showerror("Error inesperado", str(ex))
        finally:
            if keyfile and os.path.exists(keyfile):
                os.remove(keyfile)
            self.progress['value'] = 0
            self.start_btn.configure(state=tk.NORMAL)

if __name__ == "__main__":
    require_root()
    app = PyrusCryptGUI()
    app.mainloop()