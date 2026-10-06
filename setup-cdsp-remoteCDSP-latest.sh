#!/bin/bash

# Exit on error
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Function to update and upgrade the system
upgrade_system() {
    echo "🔄 Updating and upgrading system..."
    sudo apt update && sudo apt upgrade -y
    sudo apt install -y git wget curl python3-pip systemd
}

# Function to configure /boot/firmware/config.txt
configure_boot_config() {
    echo "⚙️ Configuring /boot/firmware/config.txt..."
    
    CONFIG_FILE="/boot/firmware/config.txt"
    
    # Backup before modifying
    sudo cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"

    # 1. Nettoyer les doublons potentiels ou anciennes lignes de configuration
    sudo sed -i '/dtparam=audio=/d' "$CONFIG_FILE"
    sudo sed -i '/dtoverlay=gpio-poweroff/d' "$CONFIG_FILE"
    sudo sed -i '/dtoverlay=gpio-shutdown/d' "$CONFIG_FILE"
    sudo sed -i '/enable_uart=/d' "$CONFIG_FILE"
    sudo sed -i '/gpio=12=ip,pu/d' "$CONFIG_FILE"
    sudo sed -i 's/^camera_auto_detect=/#camera_auto_detect=/' "$CONFIG_FILE"
    sudo sed -i 's/^display_auto_detect=/#display_auto_detect=/' "$CONFIG_FILE"
    sudo sed -i 's/^dtoverlay=vc4-kms-v3d/#dtoverlay=vc4-kms-v3d/' "$CONFIG_FILE"

    # 2. S'assurer qu'il y a bien une section [all] propre à la fin, et y injecter les paramètres une seule fois
    if ! sudo grep -q "\[all\]" "$CONFIG_FILE"; then
        echo -e "\n[all]" | sudo tee -a "$CONFIG_FILE" > /dev/null
    fi

    # Ajout propre des paramètres requis sous [all]
    sudo sed -i '/\[all\]/a dtparam=audio=off\ndtoverlay=gpio-poweroff,gpiopin=21,active_low=1\ndtoverlay=gpio-shutdown,gpio_pin=20,active_low=0,gpio_pull=down\nenable_uart=1\ngpio=12=ip,pu' "$CONFIG_FILE"

    # 3. Nettoyage de la console série dans cmdline.txt
    CMDLINE_FILE="/boot/firmware/cmdline.txt"
    if [ -f "$CMDLINE_FILE" ]; then
        sudo cp "$CMDLINE_FILE" "${CMDLINE_FILE}.bak"
        sudo sed -i 's/console=serial0,[0-9]* //' "$CMDLINE_FILE"
    fi

    echo "✅ /boot/firmware/config.txt et /boot/firmware/cmdline.txt nettoyés et mis à jour proprement !"
}

# Function to install CamillaDSP & CamillaGUI
install_camilladsp() {
    echo "🎛️ Installing CamillaDSP..."
    mkdir -p ~/camilladsp ~/camilladsp/coeffs ~/camilladsp/configs
    wget https://github.com/HEnquist/camilladsp/releases/download/v3.0.1/camilladsp-linux-aarch64.tar.gz -O ~/camilladsp/camilladsp-linux-aarch64.tar.gz
    sudo tar -xvf ~/camilladsp/camilladsp-linux-aarch64.tar.gz -C /usr/local/bin/

    # Set up the systemd service for CamillaDSP
    cat > ~/camilladsp.service <<EOL
[Unit]
Description=CamillaDSP
After=default.target
StartLimitIntervalSec=10
StartLimitBurst=10

[Service]
Type=simple
User=$USER
WorkingDirectory=~
ExecStart=camilladsp -s camilladsp/statefile.yml -w -g-40 -o camilladsp/camilladsp.log -p 1234
Restart=always
RestartSec=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=camilladsp
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=10

[Install]
WantedBy=default.target
EOL

    sudo mv ~/camilladsp.service /lib/systemd/system/camilladsp.service
    sudo systemctl enable camilladsp
    sudo systemctl start camilladsp

    # Install CamillaGUI
    echo "🖥️ Installing CamillaGUI..."
    wget https://github.com/HEnquist/camillagui-backend/releases/download/v3.0.3/bundle_linux_aarch64.tar.gz -O ~/camilladsp/bundle_linux_aarch64.tar.gz
    tar -xvf ~/camilladsp/bundle_linux_aarch64.tar.gz -C ~/camilladsp/

    # Set up CamillaGUI systemd service
    cat > ~/camillagui.service <<EOL
[Unit]
Description=CamillaDSP Backend and GUI
After=default.target

[Service]
Type=idle
User=$USER
WorkingDirectory=~
ExecStart=/home/$USER/camilladsp/camillagui_backend/camillagui_backend

[Install]
WantedBy=default.target
EOL

    sudo mv ~/camillagui.service /lib/systemd/system/camillagui.service
    sudo systemctl enable camillagui
    sudo systemctl start camillagui

    # Create CamillaDSP state file
    cat > ~/camilladsp/statefile.yml <<EOL
config_path: "/home/$USER/camilladsp/configs/_Setup.yml"
mute: 0
volume: 0.0
EOL

    # Create default config
    cat > ~/camilladsp/configs/_Setup.yml <<EOL
devices:
  samplerate: 48000
  chunksize: 512
  queuelimit: 4
  capture:
    type: Alsa
    channels: 2
    device: "plughw:Loopback,0,0"
    format: S32LE
  playback:
    type: Alsa
    channels: 2
    device: "plughw:0,0"
    format: S32LE
EOL

    # Load ALSA loopback module
    if ! grep -q "^snd-aloop" /etc/modules-load.d/snd-aloop.conf 2>/dev/null; then
        echo "snd-aloop" | sudo tee -a /etc/modules-load.d/snd-aloop.conf
    fi
    sudo modprobe snd-aloop
}

# Function to install Lyrion Media Server & Squeezelite
install_lyrion_and_squeezelite() {
    echo "🎵 Installing Lyrion Media Server (v9.1.1)..."
    wget https://downloads.lms-community.org/LyrionMusicServer_v9.1.1/lyrionmusicserver_9.1.1_arm.deb -O ~/lyrionmusicserver_arm.deb
    sudo dpkg -i ~/lyrionmusicserver_arm.deb || sudo apt --fix-broken install -y

    echo "🔊 Installing Squeezelite..."
    sudo apt install -y squeezelite

   # Safely update Squeezelite output device
    sudo sed -i '/^SL_OPTIONS=/d;/^SL_SOUNDCARD=/d;/^SB_EXTRA_ARGS=/d' /etc/default/squeezelite
    echo -e 'SL_SOUNDCARD="hw:Loopback,1"\nSB_EXTRA_ARGS="-W -C 5 -r 48000-48000 -R hLE"' | sudo tee -a /etc/default/squeezelite > /dev/null

    sudo systemctl restart squeezelite
}

# Function to mount USB drive
mount_usb_drive() {
    echo "💾 Setting up USB Drive auto-mount..."
    sudo mkdir -p /mnt/usb
    
    USB_UUID=$(sudo blkid -s UUID -o value /dev/sda1 2>/dev/null || true)
    
    if [ -z "$USB_UUID" ]; then
        echo "⚠️ Aucune clé USB détectée automatiquement sur /dev/sda1."
        read -p "Entrez manuellement l'UUID de votre clé USB : " USB_UUID
    else
        echo "✅ Clé USB détectée automatiquement avec l'UUID : $USB_UUID"
    fi
    
    if [ -n "$USB_UUID" ]; then
        sudo sed -i 's|.* /mnt/usb .*||g' /etc/fstab
        sudo sed -i '/^$/d' /etc/fstab

        echo "UUID=$USB_UUID /mnt/usb auto defaults,nofail,x-systemd.device-timeout=1,noatime 0 0" | sudo tee -a /etc/fstab
        
        sudo mount -a
        echo "✅ USB Drive mounted at /mnt/usb with UUID $USB_UUID!"
    else
        echo "❌ Erreur : Aucun UUID valide n'a pu être configuré."
    fi
}

# Function to set sound card output
set_sound_card() {
    echo "🔊 Lancement d'alsamixer pour configurer la carte son..."
    echo "   (Echap ou 'q' pour quitter une fois les reglages faits)"
    alsamixer
    echo "💾 Sauvegarde des reglages ALSA..."
    sudo alsactl store
    echo "✅ Reglages ALSA sauvegardes !"
}

# Function to install Bluetooth Remote Script & LED Display module with venv
install_bluetooth_remote() {
    echo "🎮 Installing Remote Control & LED module in venv..."

    sudo usermod -aG dialout,gpio,input,spi,i2c "$USER"

    if [ ! -d "/opt/venv" ]; then
        sudo mkdir -p /opt/venv
        sudo python3 -m venv --system-site-packages /opt/venv
    fi
    sudo /opt/venv/bin/pip install --upgrade pip

    sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5
    sudo /opt/venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840"

    echo "📥 Downloading remote.py and tm1637_lgpio.py from GitHub..."
    wget -q https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/remote.py -O ~/remote.py
    
    PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    wget -q https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O /tmp/tm1637_lgpio.py
    sudo cp /tmp/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"
    rm -f /tmp/tm1637_lgpio.py

    sudo tee /etc/systemd/system/remote.service > /dev/null <<EOL
[Unit]
Description=CamillaDSP Remote control, led display & power trigger
After=default.target

[Service]
User=$USER
Type=simple
WorkingDirectory=~
ExecStart=/opt/venv/bin/python3 remote.py
Restart=on-failure
RestartSec=5
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=remote

[Install]
WantedBy=default.target
EOL

    sudo systemctl daemon-reload
    sudo systemctl enable remote.service
    sudo systemctl start remote.service
    echo "✅ Remote service configured and started with /opt/venv/bin/python3 !"
}

# Function to pair Bluetooth Remote using bluetuith, select device, and write to remote.py
pair_bluetooth_remote() {
    REMOTE_SCRIPT="${REMOTE_SCRIPT:-$HOME/remote.py}"
    VENV_PYTHON="${VENV_PYTHON:-/opt/venv/bin/python3}"

    echo "🔗 Preparing Bluetooth Remote pairing interface..."
    
    sudo rfkill unblock bluetooth
    sudo systemctl restart bluetooth
    sleep 2

    bluetoothctl power on

if ! command -v bluetuith &> /dev/null; then
    echo "📦 Installation de bluetuith..."
    wget https://github.com/bluetuith-org/bluetuith/releases/download/v0.2.7/bluetuith_0.2.7_Linux_arm64.tar.gz -O ~/bluetuith.tar.gz
    tar -xzf ~/bluetuith.tar.gz -C ~/ bluetuith
    sudo mv ~/bluetuith /usr/bin/bluetuith
    sudo chmod +x /usr/bin/bluetuith
    rm -f ~/bluetuith.tar.gz
fi

    echo ""
    echo "💡 Instructions :"
    echo "   1. L'interface bluetuith va s'ouvrir."
    echo "   2. Mettez votre télécommande en mode appairage (LED clignotante)."
    echo "   3. Utilisez les flèches pour trouver votre télécommande, appuyez sur Entrée pour la Pairer, puis la Connecter."
    echo "   4. Appuyez sur 'q' pour quitter l'interface une fois terminé."
    echo ""
    read -p "Appuyez sur Entrée pour lancer bluetuith..."

    bluetuith
    
    echo "✅ Bluetooth setup interface closed."

    echo ""
    echo "🔍 Detecting input devices..."
    echo "   The remote should now be connected. Press a key on it if needed."
    read -rp "Press Enter to scan..."

    local -a lines
    mapfile -t lines < <("$VENV_PYTHON" - <<'EOF'
import evdev
from evdev import ecodes as e

names = ['KEY_VOLUMEDOWN', 'KEY_VOLUMEUP', 'KEY_MUTE', 'KEY_PLAYPAUSE', 'KEY_PREVIOUSSONG',
         'KEY_NEXTSONG', 'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT', 'KEY_POWER',
         'KEY_ENTER', 'KEY_BACK', 'KEY_HOMEPAGE']
codes = {e.ecodes[n] for n in names}

rows = []
for p in evdev.list_devices():
    d = evdev.InputDevice(p)
    keys = d.capabilities().get(e.EV_KEY, [])
    rows.append((len(codes & set(keys)), p, d.name))

for score, p, name in sorted(rows, key=lambda r: (-r[0], r[1])):
    print(f"{p}\t{name}\t{score}/{len(codes)}")
EOF
)

    if [ "${#lines[@]}" -eq 0 ]; then
        echo "❌ No input device found (is the remote connected? is evdev installed in the venv?)"
        return 1
    fi

    echo ""
    echo "Input devices found (best match for the remote first):"
    local i dev_path dev_name dev_keys
    for i in "${!lines[@]}"; do
        IFS=$'\t' read -r dev_path dev_name dev_keys <<< "${lines[$i]}"
        printf "   %d) %-26s %-8s %s\n" "$((i + 1))" "$dev_name" "$dev_keys" "$dev_path"
    done
    echo ""

    local choice
    while true; do
        read -rp "Choose your remote [1-${#lines[@]}] (Enter = 1): " choice
        choice="${choice:-1}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#lines[@]} )); then
            break
        fi
        echo "Invalid choice."
    done

    IFS=$'\t' read -r dev_path dev_name dev_keys <<< "${lines[$((choice - 1))]}"
    echo "➡  Selected: $dev_name ($dev_keys keys)"
    if (( ${dev_keys%%/*} < 10 )); then
        echo "⚠️  This device supports few of the remote keys: check that it is really the remote."
    fi

    if [ ! -f "$REMOTE_SCRIPT" ]; then
        echo "⚠️  $REMOTE_SCRIPT not found. Set this line manually in remote.py:"
        echo "   REMOTE_NAME = \"$dev_name\""
        return 1
    fi

    if "$VENV_PYTHON" - "$REMOTE_SCRIPT" "$dev_name" <<'EOF'
import json
import re
import sys

script, name = sys.argv[1], sys.argv[2]
with open(script, encoding="utf-8") as f:
    text = f.read()

pattern = re.compile(
    r"""^(REMOTE_NAME\s*=\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""",
    re.MULTILINE,
)
new_text, count = pattern.subn(
    lambda m: m.group(1) + json.dumps(name, ensure_ascii=False), text, count=1
)
if count == 0:
    sys.exit(1)

with open(script, "w", encoding="utf-8") as f:
    f.write(new_text)
EOF
    then
        echo "✅ REMOTE_NAME set to \"$dev_name\" in $REMOTE_SCRIPT"
        echo "🔄 Restarting remote service..."
        sudo systemctl restart remote
        echo "✅ Remote service restarted successfully."
    else
        echo "⚠️  Could not update $REMOTE_SCRIPT (no 'REMOTE_NAME = \"...\"' line, or no write permission)."
        echo "    Set this line manually in remote.py:"
        echo "    REMOTE_NAME = \"$dev_name\""
        return 1
    fi
}

# Function to reboot
reboot_now() {
    read -p "Reboot now? (y/n): " choice
    if [[ "$choice" == "y" || "$choice" == "Y" ]]; then
        sudo reboot
    fi
}

# Function to install everything sequentially
install_everything() {
    upgrade_system
    configure_boot_config
    install_camilladsp
    install_lyrion_and_squeezelite
    mount_usb_drive
    set_sound_card
    install_bluetooth_remote
    pair_bluetooth_remote
    reboot_now
    echo "✅ All installations completed!"
}

# Main Menu
while true; do
    echo "=================================="
    echo "  Raspberry Pi Audio Setup Menu   "
    echo "=================================="
    echo "1) Install Everything"
    echo "2) Upgrade System"
    echo "3) Configure /boot/firmware/config.txt"
    echo "4) Install CamillaDSP & GUI"
    echo "5) Install Lyrion Media Server & Squeezelite"
    echo "6) Mount USB Drive"
    echo "7) Configure Sound Levels (alsamixer)"
    echo "8) Install Remote Script"
    echo "9) Pair Bluetooth Remote"
    echo "10) Reboot System"
    echo "11) Exit"
    echo "=================================="
    read -p "Choose an option [1-11]: " option

    case $option in
        1) install_everything ;;
        2) upgrade_system ;;
        3) configure_boot_config ;;
        4) install_camilladsp ;;
        5) install_lyrion_and_squeezelite ;;
        6) mount_usb_drive ;;
        7) set_sound_card ;;
        8) install_bluetooth_remote ;;
        9) pair_bluetooth_remote ;;
        10) reboot_now ;;
        11) echo "Exiting..."; exit 0 ;;
        *) echo "Invalid option. Please try again." ;;
    esac
done
