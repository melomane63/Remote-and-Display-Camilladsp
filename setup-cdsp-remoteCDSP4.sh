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

    # 1. Clean up potential duplicates or old configuration lines
    sudo sed -i '/dtparam=audio=/d' "$CONFIG_FILE"
    sudo sed -i '/dtoverlay=gpio-poweroff/d' "$CONFIG_FILE"
    sudo sed -i '/dtoverlay=gpio-shutdown/d' "$CONFIG_FILE"
    sudo sed -i '/enable_uart=/d' "$CONFIG_FILE"
    sudo sed -i '/gpio=12=ip,pu/d' "$CONFIG_FILE"
    sudo sed -i 's/^camera_auto_detect=/#camera_auto_detect=/' "$CONFIG_FILE"
    sudo sed -i 's/^display_auto_detect=/#display_auto_detect=/' "$CONFIG_FILE"
    sudo sed -i 's/^dtoverlay=vc4-kms-v3d/#dtoverlay=vc4-kms-v3d/' "$CONFIG_FILE"

    # 2. Make sure a clean [all] section exists at the end, and inject the parameters only once
    if ! sudo grep -q "\[all\]" "$CONFIG_FILE"; then
        echo -e "\n[all]" | sudo tee -a "$CONFIG_FILE" > /dev/null
    fi

    # Cleanly add the required parameters under [all]
    # Note: the GPIO 12 (TV_GPIO) pull-up is now handled directly in remote.py
    # via lgpio.gpio_claim_input(h, TV_GPIO, lgpio.SET_PULL_UP), so gpio=12=ip,pu is no longer needed here.
    sudo sed -i '/\[all\]/a dtparam=audio=off\ndtoverlay=gpio-poweroff,gpiopin=21,active_low=1\ndtoverlay=gpio-shutdown,gpio_pin=20,active_low=0,gpio_pull=down\nenable_uart=1' "$CONFIG_FILE"

    # 3. Clean up the serial console in cmdline.txt
    CMDLINE_FILE="/boot/firmware/cmdline.txt"
    CMDLINE_MODIFIED=false
    if [ -f "$CMDLINE_FILE" ]; then
        sudo cp "$CMDLINE_FILE" "${CMDLINE_FILE}.bak"
        if grep -q 'console=serial0,[0-9]*' "$CMDLINE_FILE"; then
            CMDLINE_MODIFIED=true
        fi
        sudo sed -i 's/console=serial0,[0-9]* //' "$CMDLINE_FILE"
    fi

    echo "✅ /boot/firmware/config.txt and /boot/firmware/cmdline.txt cleaned up and updated!"
    echo ""
    echo "📋 Summary of changes made:"
    echo "  - Backups created     : ${CONFIG_FILE}.bak"
    [ -f "$CMDLINE_FILE" ] && echo "                          ${CMDLINE_FILE}.bak"
    echo "  - camera_auto_detect      : disabled (commented out)"
    echo "  - display_auto_detect     : disabled (commented out)"
    echo "  - dtoverlay=vc4-kms-v3d   : disabled (commented out)"
    echo "  - dtparam=audio           : disabled (audio=off)"
    echo "  - dtoverlay=gpio-poweroff : added (GPIO 21, active_low=1)"
    echo "  - dtoverlay=gpio-shutdown : added (GPIO 20, active_low=0, pull=down)"
    echo "  - enable_uart             : enabled (enable_uart=1)"
    echo "  - gpio=12=ip,pu           : removed (pull-up now handled in remote.py)"
    if [ "$CMDLINE_MODIFIED" = true ]; then
        echo "  - cmdline.txt             : serial console (console=serial0,...) removed"
    else
        echo "  - cmdline.txt             : no serial console found (nothing to change)"
    fi
    echo ""
}

# Function to install CamillaDSP & CamillaGUI
install_camilladsp() {
    echo "🎛️ Installing CamillaDSP..."
    mkdir -p ~/camilladsp ~/camilladsp/coeffs ~/camilladsp/configs
    wget https://github.com/HEnquist/camilladsp/releases/download/v4.1.3/camilladsp-linux-aarch64.tar.gz -O ~/camilladsp/camilladsp-linux-aarch64.tar.gz
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
    wget https://github.com/HEnquist/camillagui-backend/releases/download/v4.1.0/bundle_linux_aarch64.tar.gz -O ~/camilladsp/bundle_linux_aarch64.tar.gz
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
# Rebuilt from the actual disk audit (pip freeze, paths, service): the venv must
# inherit system packages (python-apt, distro, ssh-import-id can't be installed via
# pip alone), pyserial + pycamilladsp (via Git) are required by remote.py and were
# missing from the previous version of this function.
install_bluetooth_remote() {
    echo "🎮 Installing Remote Control & LED module in venv..."

    # Groups required: serial (dialout), GPIO (gpio), reading the remote/gamepad (input),
    # SPI/I2C in case other peripherals depend on them
    sudo usermod -aG dialout,gpio,input,spi,i2c "$USER"

    # Venv inheriting system packages (gpiozero, numpy, scipy, python-apt, distro,
    # ssh-import-id, etc. provided by the Raspberry Pi OS image)
    if [ ! -d "/opt/venv" ]; then
        sudo mkdir -p /opt/venv
        sudo python3 -m venv --system-site-packages /opt/venv
    fi
    sudo /opt/venv/bin/pip install --upgrade pip

    # Dependencies STRICTLY required by remote.py (portable, tested on Bookworm + Trixie)
    sudo /opt/venv/bin/pip install evdev==1.6.1 lgpio==0.2.2.0 pyserial==3.5

    # Packages inherited from the system (gpiozero, numpy, scipy, python-apt, distro,
    # ssh-import-id...) via --system-site-packages: their versions are NOT pinned here,
    # as they depend on the distro (Bookworm vs Trixie) and python-apt/distro/ssh-import-id
    # are not true portable PyPI packages (they're tied to the system's libapt-pkg) - forcing
    # their version often fails to build on a distro different from the one it was pinned on.

    # pycamilladsp (Python client for the CamillaDSP daemon) - installed via Git, at the
    # exact commit found during the audit. This is the package missing from the previous
    # version of this function.
    sudo /opt/venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git"
    
    echo "📥 Downloading remote.py and tm1637_lgpio.py from GitHub..."
    wget -q https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/remote.py -O ~/remote.py
    # tm1637_lgpio.py placed directly in the venv - path computed dynamically
    # (python3.11 on Bookworm, python3.13 on Trixie, etc.)
    # /opt/venv is owned by root -> download to /tmp then copy with sudo
    PYVER=$(/opt/venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    wget -q https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O /tmp/tm1637_lgpio.py
    sudo cp /tmp/tm1637_lgpio.py "/opt/venv/lib/python${PYVER}/site-packages/tm1637_lgpio.py"
    rm -f /tmp/tm1637_lgpio.py

    # Create the exact systemd service file you want
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
    
    # Make sure Bluetooth isn't blocked and restart the service
    sudo rfkill unblock bluetooth
    sudo systemctl restart bluetooth
    sleep 2

    # Turn Bluetooth on
    bluetoothctl power on

    # Check whether bluetuith is installed, otherwise install it
    if ! command -v bluetuith &> /dev/null; then
        echo "📦 Installing bluetuith for visual Bluetooth management..."
        sudo apt update
        sudo apt install -y golang-go git
        go install github.com/darkhz/bluetuith@latest
        sudo ln -sf ~/go/bin/bluetuith /usr/bin/bluetuith
    fi

    echo ""
    echo "💡 Instructions:"
    echo "   1. The bluetuith interface will open."
    echo "   2. Put your remote in pairing mode (blinking LED)."
    echo "   3. Use the arrow keys to find your remote, press Enter to Pair it, then Connect it."
    echo "   4. Press 'q' to quit the interface once done."
    echo ""
    read -p "Press Enter to launch bluetuith..."

    # Launch the visual interface
    bluetuith
    
    echo "✅ Bluetooth setup interface closed."
}

# Function to mount USB drive
mount_usb_drive() {
    echo "111💾 Setting up USB Drive auto-mount..."
    sudo mkdir -p /mnt/usb
 
    # Discover every removable block device and list its partitions.
    # We look at /sys/block/*/removable instead of hardcoding /dev/sda1,
    # so this works correctly even with several USB drives plugged in,
    # or if the OS names the drive /dev/sdb, /dev/sdc, etc.
    CANDIDATES=()
    for dev_path in /sys/block/*/; do
        dev_name=$(basename "$dev_path")
        removable_flag="${dev_path}removable"
        if [ -f "$removable_flag" ] && [ "$(cat "$removable_flag")" = "1" ]; then
            for part in /dev/${dev_name}*[0-9]; do
                [ -b "$part" ] && CANDIDATES+=("$part")
            done
        fi
    done
 
    USB_UUID=""
 
    if [ ${#CANDIDATES[@]} -eq 0 ]; then
        # Nothing detected automatically: fall back to manual entry
        echo "⚠️ No removable USB drive automatically detected."
        read -p "Manually enter your USB drive's UUID: " USB_UUID
 
    elif [ ${#CANDIDATES[@]} -eq 1 ]; then
        # Exactly one candidate: use it directly
        PART="${CANDIDATES[0]}"
        USB_UUID=$(sudo blkid -s UUID -o value "$PART" 2>/dev/null || true)
        if [ -n "$USB_UUID" ]; then
            echo "✅ USB drive automatically detected: $PART (UUID=$USB_UUID)"
        else
            echo "⚠️ Found $PART but could not read its UUID."
            read -p "Manually enter your USB drive's UUID: " USB_UUID
        fi
 
    else
        # Several candidates: show a menu and let the user pick
        echo "🔎 Multiple USB drives detected:"
        echo ""
        UUID_LIST=()
        i=1
        for part in "${CANDIDATES[@]}"; do
            uuid=$(sudo blkid -s UUID -o value "$part" 2>/dev/null || echo "")
            label=$(sudo blkid -s LABEL -o value "$part" 2>/dev/null || echo "")
            size=$(lsblk -no SIZE "$part" 2>/dev/null | xargs || echo "?")
            UUID_LIST+=("$uuid")
            printf "  %d) %-12s size=%-8s label=%-15s uuid=%s\n" "$i" "$part" "$size" "${label:-<none>}" "${uuid:-<none>}"
            i=$((i+1))
        done
        echo ""
        read -p "Choose the drive to mount [1-${#CANDIDATES[@]}]: " choice
 
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#CANDIDATES[@]}" ]; then
            USB_UUID="${UUID_LIST[$((choice-1))]}"
            echo "✅ Selected: ${CANDIDATES[$((choice-1))]} (UUID=$USB_UUID)"
        else
            echo "⚠️ Invalid choice."
            read -p "Manually enter your USB drive's UUID: " USB_UUID
        fi
    fi
 
    if [ -n "$USB_UUID" ]; then
        # Backup fstab before touching it
        sudo cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
 
        # Clean up the old entry for /mnt/usb if it already exists
        sudo sed -i 's|.* /mnt/usb .*||g' /etc/fstab
        sudo sed -i '/^$/d' /etc/fstab
 
        # Add the new configuration
        echo "UUID=$USB_UUID /mnt/usb auto defaults,nofail,x-systemd.device-timeout=1,noatime 0 0" | sudo tee -a /etc/fstab
 
        sudo mount -a
        echo "✅ USB Drive mounted at /mnt/usb with UUID $USB_UUID!"
    else
        echo "❌ Error: No valid UUID could be configured."
    fi
}

# Function to set sound card output
set_sound_card() {
    echo "🔊 Launching alsamixer to configure the sound card..."
    echo "   (Esc or 'q' to quit once the settings are done)"
    alsamixer
    echo "💾 Saving ALSA settings..."
    sudo alsactl store
    echo "✅ ALSA settings saved!"
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
