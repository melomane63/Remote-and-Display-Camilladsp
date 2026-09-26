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

    echo "1✅ /boot/firmware/config.txt et /boot/firmware/cmdline.txt nettoyés et mis à jour proprement !"
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
  samplerate: 44100
  chunksize: 1024
  queuelimit: 4
  capture:
    type: Alsa
    channels: 2
    device: "hw:Loopback,1,0"
    format: S32LE
  playback:
    type: Alsa
    channels: 2
    device: "hw:0,0"
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
    if grep -q "^SL_OPTIONS=" /etc/default/squeezelite; then
        sudo sed -i 's/^SL_OPTIONS=.*/SL_OPTIONS="-o hw:Loopback,0,0"/' /etc/default/squeezelite
    else
        echo 'SL_OPTIONS="-o hw:Loopback,0,0"' | sudo tee -a /etc/default/squeezelite
    fi

    sudo systemctl restart squeezelite
}

# Function to install Bluetooth Remote Script & LED Display module with venv
# Reconstruit d'apres l'audit reel du disque (pip freeze, chemins, service) : le venv doit
# heriter des paquets systeme (python-apt, distro, ssh-import-id ne s'installent pas via
# pip seul), pyserial + pycamilladsp (via Git) sont necessaires a remote.py et etaient
# absents de l'ancienne version de cette fonction.
install_bluetooth_remote() {
    echo "🎮 Installing Remote Control & LED module in venv..."

    # Groupes necessaires : serie (dialout), GPIO (gpio), lecture manette/telecommande (input),
    # SPI/I2C au cas ou d'autres peripheriques en dependent
    sudo usermod -aG dialout,gpio,input,spi,i2c "$USER"

    # Venv avec heritage des paquets systeme (gpiozero, numpy, scipy, python-apt, distro,
    # ssh-import-id, etc. fournis par l'image Raspberry Pi OS)
    if [ ! -d "/opt/venv" ]; then
        sudo mkdir -p /opt/venv
        sudo python3 -m venv --system-site-packages /opt/venv
    fi
    sudo /opt/venv/bin/pip install --upgrade pip

    # Dependances STRICTEMENT necessaires a remote.py (portables, testees Bookworm + Trixie)
    sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5

    # Paquets herites du systeme (gpiozero, numpy, scipy, python-apt, distro, ssh-import-id...)
    # via --system-site-packages : on ne les pin PAS ici, leurs versions dependent de la
    # distro (Bookworm vs Trixie) et python-apt/distro/ssh-import-id ne sont pas de vrais
    # paquets PyPI portables (lies a libapt-pkg du systeme) - forcer leur version echoue
    # souvent a la compilation sur une distro differente de celle ou la version a ete figee.

    # pycamilladsp (client Python du demon CamillaDSP) - installe via Git, commit precis
    # trouve dans l'audit. C'est le paquet manquant de l'ancienne version de cette fonction.
    sudo /opt/venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840"

    echo "📥 Downloading remote.py and tm1637_lgpio.py from GitHub..."
    wget -q https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/remote.py -O ~/remote.py
    # tm1637_lgpio.py place directement dans le venv - chemin calcule dynamiquement
    # (python3.11 sur Bookworm, python3.13 sur Trixie, etc.)
    # /opt/venv appartient a root -> telechargement dans /tmp puis copie avec sudo
    PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    wget -q https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O /tmp/tm1637_lgpio.py
    sudo cp /tmp/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"
    rm -f /tmp/tm1637_lgpio.py

    # Création du fichier de service systemd exact que vous souhaitez
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


# Function to pair Bluetooth Remote using bluetuith
pair_bluetooth_remote() {
    echo "🔗 Preparing Bluetooth Remote pairing interface..."
    
    # S'assurer que le Bluetooth n'est pas bloqué et relancer le service
    sudo rfkill unblock bluetooth
    sudo systemctl restart bluetooth
    sleep 2

    # Allumer le Bluetooth
    bluetoothctl power on

    # Vérifier si bluetuith est installé, sinon l'installer
    if ! command -v bluetuith &> /dev/null; then
        echo "📦 Installing bluetuith for visual Bluetooth management..."
        sudo apt update
        sudo apt install -y golang-go git
        go install github.com/darkhz/bluetuith@latest
        sudo ln -sf ~/go/bin/bluetuith /usr/bin/bluetuith
    fi

    echo ""
    echo "💡 Instructions :"
    echo "   1. L'interface bluetuith va s'ouvrir."
    echo "   2. Mettez votre télécommande en mode appairage (LED clignotante)."
    echo "   3. Utilisez les flèches pour trouver votre télécommande, appuyez sur Entrée pour la Pairer, puis la Connecter."
    echo "   4. Appuyez sur 'q' pour quitter l'interface une fois terminé."
    echo ""
    read -p "Appuyez sur Entrée pour lancer bluetuith..."

    # Lancer l'interface visuelle
    bluetuith
    
    echo "✅ Bluetooth setup interface closed."
}

# Function to mount USB drive
mount_usb_drive() {
    echo "💾 Setting up USB Drive auto-mount..."
    sudo mkdir -p /mnt/usb
    
    # Récupérer automatiquement l'UUID de la première partition sur /dev/sda1
    USB_UUID=$(sudo blkid -s UUID -o value /dev/sda1 2>/dev/null || true)
    
    if [ -z "$USB_UUID" ]; then
        echo "⚠️ Aucune clé USB détectée automatiquement sur /dev/sda1."
        read -p "Entrez manuellement l'UUID de votre clé USB : " USB_UUID
    else
        echo "✅ Clé USB détectée automatiquement avec l'UUID : $USB_UUID"
    fi
    
    if [ -n "$USB_UUID" ]; then
        # Nettoyer l'ancienne entrée pour /mnt/usb si elle existe déjà
        sudo sed -i 's|.* /mnt/usb .*||g' /etc/fstab
        sudo sed -i '/^$/d' /etc/fstab

        # Ajouter la nouvelle configuration
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
    install_bluetooth_remote
    mount_usb_drive
    pair_bluetooth_remote
    set_sound_card
    reboot_now
    echo "✅ All installations completed!"
}

# Main Menu
while true; do
    echo "=================================="
    echo "  Raspberry Pi Audio Setup Menu   "
    echo "=================================="
    echo "1) Install Everything (Automated)"
    echo "2) Upgrade System"
    echo "3) Configure /boot/firmware/config.txt"
    echo "4) Install CamillaDSP & GUI"
    echo "5) Install Lyrion Media Server & Squeezelite"
    echo "6) Install Bluetooth Remote Script"
    echo "7) Pair Bluetooth Remote"
    echo "8) Mount USB Drive"
    echo "9) Configure Sound Levels (alsamixer)"
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
        6) install_bluetooth_remote ;;
        7) pair_bluetooth_remote ;;
        8) mount_usb_drive ;;
        9) set_sound_card ;;
        10) reboot_now ;;
        11) echo "Exiting..."; exit 0 ;;
        *) echo "Invalid option. Please try again." ;;
    esac
done
