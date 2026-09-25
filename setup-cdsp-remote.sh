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

    set_config_param() {
        local param="$1"
        if sudo grep -q "^#*${param%%=*}=" "$CONFIG_FILE"; then
            sudo sed -i "s|^#*${param%%=*}=.*|${param}|" "$CONFIG_FILE"
        else
            echo "$param" | sudo tee -a "$CONFIG_FILE" > /dev/null
        fi
    }

    set_config_param "dtoverlay=gpio-poweroff,gpiopin=21,active_low=1"
    set_config_param "dtoverlay=gpio-shutdown,gpio_pin=20,active_low=0,gpio_pull=down"
    set_config_param "enable_uart=1"
    set_config_param "gpio=12=ip,pu"

    echo "✅ /boot/firmware/config.txt mis à jour !"
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

# Function to install Bluetooth Remote Script
install_bluetooth_remote() {
    echo "🎮 Installing Bluetooth Remote Control..."
    sudo apt install -y python3-evdev python3-websocket

    echo "📥 Downloading remote.py from GitHub..."
    wget -q https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/remote.py -O ~/remote.py

    cat > ~/remote.service <<EOL
[Unit]
Description=Bluetooth Remote Control for CamillaDSP
After=bluetooth.target network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=/home/$USER
ExecStart=/usr/bin/python3 /home/$USER/remote.py
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOL

    sudo mv ~/remote.service /lib/systemd/system/remote.service
    sudo systemctl daemon-reload
    sudo systemctl enable remote.service
    sudo systemctl start remote.service
}


# Function to pair Bluetooth Remote
pair_bluetooth_remote() {
    echo "🔗 Pairing Bluetooth Remote..."
    
    # S'assurer que le Bluetooth n'est pas bloqué et relancer le service
    sudo rfkill unblock bluetooth
    sudo systemctl restart bluetooth
    sleep 2

    # Allumer le Bluetooth
    bluetoothctl power on
    
    echo "Please set your Bluetooth remote in pairing mode now."
    read -p "Press Enter to start scanning for Bluetooth devices..."
    
    echo "🔍 Scanning for Bluetooth devices for 10 seconds (Look for your remote's MAC address)..."
    
    # Lancement du scan en direct dans le terminal
    (
        echo "scan on"
        sleep 10
        echo "scan off"
        echo "exit"
    ) | bluetoothctl

    echo ""
    echo "--- Fin du scan ---"
    
    # Réinitialisation de la variable au cas où
    BT_MAC=""
    
    # Boucle pour s'assurer qu'une adresse MAC est saisie
    while [ -z "$BT_MAC" ]; do
        read -p "Enter the MAC address of your Bluetooth Remote (e.g. XX:XX:XX:XX:XX:XX): " BT_MAC
        if [ -z "$BT_MAC" ]; then
            echo "⚠️ Address cannot be empty. Please try again."
        fi
    done

    bluetoothctl pair $BT_MAC
    bluetoothctl trust $BT_MAC
    bluetoothctl connect $BT_MAC
    echo "✅ Bluetooth Remote paired and connected!"
}

# Function to mount USB drive
mount_usb_drive() {
    echo "💾 Setting up USB Drive auto-mount..."
    sudo mkdir -p /mnt/usb
    
    # Récupérer automatiquement l'UUID de la première partition de type vfat, ext4 ou ntfs sur /dev/sda
    USB_UUID=$(sudo blkid -s UUID -o value /dev/sda1 2>/dev/null || true)
    
    if [ -z "$USB_UUID" ]; then
        echo "⚠️ Aucune clé USB détectée automatiquement sur /dev/sda1."
        read -p "Entrez manuellement l'UUID de votre clé USB : " USB_UUID
    else
        echo "✅ Clé USB détectée automatiquement avec l'UUID : $USB_UUID"
    fi
    
    if [ -n "$USB_UUID" ]; then
        # Nettoyer l'ancienne entrée pour /mnt/usb si elle existe déjà pour éviter les doublons
        sudo sed -i 's|.* /mnt/usb .*||g' /etc/fstab
        sudo sed -i '/^$/d' /etc/fstab # Supprimer les lignes vides créées

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
    echo "🔊 Available Sound Cards:"
    aplay -l
    read -p "Enter your Sound Card device (e.g., hw:1,0 or hw:CARD=DAC,DEV=0): " SOUND_CARD
    
    sed -i "s/device: \"hw:0,0\"/device: \"$SOUND_CARD\"/" ~/camilladsp/configs/_Setup.yml
    sudo systemctl restart camilladsp
    echo "✅ Sound card set to $SOUND_CARD!"
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
    echo "9) Set Sound Card Output"
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
